from __future__ import annotations

import json
import os
import platform
import re
import shutil
import ssl
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psutil
from mcp.server import FastMCP
from seller_client.windows_elevation import (
    is_windows_platform,
    wireguard_helper_query_task_command,
    wireguard_helper_request_path,
    wireguard_helper_result_path,
    wireguard_helper_root,
    wireguard_helper_run_task_command,
    wireguard_helper_task_name,
    windows_is_elevated,
)

DEFAULT_MANAGER_HOST = "pivotcompute.store"
DEFAULT_MANAGER_PORT = 2377
DEFAULT_REGISTRY = "pivotcompute.store"
DEFAULT_PORTAINER_URL = "https://pivotcompute.store:9443"
DEFAULT_WG_INTERFACE = "wg-seller"
DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
LEGACY_REGISTRY_HOSTS = {"81.70.52.75"}


def _default_state_dir() -> Path:
    override = os.environ.get("PIVOT_SELLER_CLIENT_HOME")
    if override:
        return Path(override).expanduser().resolve()

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path.home()
    return (base / "pivot-seller-client").resolve()


def _ensure_client_dirs(base_dir: Path | None = None) -> dict[str, str]:
    root = (base_dir or _default_state_dir()).resolve()
    paths = {
        "root": root,
        "config_dir": root / "config",
        "wireguard_dir": root / "wireguard",
        "logs_dir": root / "logs",
        "cache_dir": root / "cache",
        "images_dir": root / "images",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return {name: str(path) for name, path in paths.items()}


def _config_path(base_dir: Path | None = None) -> Path:
    root = Path(_ensure_client_dirs(base_dir)["config_dir"])
    return root / "client.json"


def _wireguard_config_path(interface_name: str, base_dir: Path | None = None) -> Path:
    wireguard_dir = Path(_ensure_client_dirs(base_dir)["wireguard_dir"])
    return wireguard_dir / f"{interface_name}.conf"


def _default_config() -> dict[str, Any]:
    return {
        "server": {
            "manager_host": DEFAULT_MANAGER_HOST,
            "manager_port": DEFAULT_MANAGER_PORT,
            "registry": DEFAULT_REGISTRY,
            "portainer_url": DEFAULT_PORTAINER_URL,
            "backend_url": DEFAULT_BACKEND_URL,
        },
        "wireguard": {
            "interface": DEFAULT_WG_INTERFACE,
            "config_path": "",
            "endpoint_host": "",
            "endpoint_port": 0,
            "client_address": "",
            "client_public_key": "",
            "allowed_ips": "",
            "dns": "",
        },
        "docker": {
            "last_pushed_image": "",
        },
        "runtime": {
            "codex_provider": "",
            "codex_model": "",
            "codex_runtime_ready": False,
            "codex_last_bootstrap_at": "",
            "wireguard_profile_status": "",
            "wireguard_last_bootstrap_at": "",
            "wireguard_activation_mode": "",
        },
        "auth": {
            "seller_email": "",
            "access_token": "",
            "node_registration_token": "",
            "device_fingerprint": "",
        },
    }


def _load_client_config(base_dir: Path | None = None) -> dict[str, Any]:
    path = _config_path(base_dir)
    if not path.exists():
        return _default_config()

    data = json.loads(path.read_text(encoding="utf-8"))
    merged = _default_config()
    for section, values in data.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


def _save_client_config(data: dict[str, Any], base_dir: Path | None = None) -> dict[str, Any]:
    path = _config_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return {"ok": True, "config_path": str(path), "data": data}


def _mask_config_secrets(data: dict[str, Any]) -> dict[str, Any]:
    rendered = json.loads(json.dumps(data))
    for section in rendered.values():
        if not isinstance(section, dict):
            continue
        for key in list(section.keys()):
            if "private" in key or "token" in key or "password" in key:
                value = section[key]
                section[key] = "***" if value else value
    return rendered


def _run_command(command: list[str], cwd: str | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "command": command,
        "cwd": cwd,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "ok": completed.returncode == 0,
    }


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _is_transient_registry_push_error(result: dict[str, Any]) -> bool:
    if result.get("ok"):
        return False
    combined = f"{result.get('stdout')}\n{result.get('stderr')}".lower()
    transient_markers = (
        "eof",
        "connection reset by peer",
        "broken pipe",
        "tls handshake timeout",
        "context deadline exceeded",
        "i/o timeout",
        "unexpected http status: 5",
    )
    return any(marker in combined for marker in transient_markers)


def _parse_json_lines(stdout: str) -> Any:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) == 1:
        try:
            return json.loads(lines[0])
        except json.JSONDecodeError:
            return lines[0]

    parsed: list[Any] = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append(line)
    return parsed


def _docker_json(command: list[str]) -> dict[str, Any]:
    result = _run_command(command)
    if not result["ok"]:
        return result
    result["data"] = _parse_json_lines(result["stdout"])
    return result


def _run_registry_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout_seconds: float = 20,
) -> dict[str, Any]:
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8", "replace")
            return {
                "ok": True,
                "status": response.status,
                "headers": dict(response.headers.items()),
                "body": payload,
                "url": url,
            }
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        return {
            "ok": False,
            "status": exc.code,
            "headers": dict(exc.headers.items()),
            "body": payload,
            "url": url,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "headers": {}, "body": str(exc), "url": url}


def _run_backend_request(
    method: str,
    path: str,
    *,
    backend_url: str,
    bearer_token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 20,
) -> dict[str, Any]:
    base_url = backend_url.rstrip("/")
    data: bytes | None = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    return _run_registry_request(
        method,
        f"{base_url}{path}",
        headers=headers,
        data=data,
        timeout_seconds=timeout_seconds,
    )


def _normalize_registry_reference(registry: str) -> str:
    normalized = registry.strip().rstrip("/")
    if not normalized:
        return DEFAULT_REGISTRY

    parsed = urlsplit(normalized if "://" in normalized else f"https://{normalized}")
    host = (parsed.hostname or "").lower()
    port = parsed.port
    path = (parsed.path or "").strip("/")

    if not path and host in {DEFAULT_REGISTRY, *LEGACY_REGISTRY_HOSTS} and port in {None, 443, 5000}:
        return DEFAULT_REGISTRY

    if parsed.hostname:
        if parsed.port is not None:
            return f"{host}:{parsed.port}"
        return host
    return normalized


def _registry_base_url(registry: str) -> str:
    normalized = _normalize_registry_reference(registry)
    _, port = _registry_host_port(normalized)
    scheme = "http" if port == 80 else "https"
    return f"{scheme}://{normalized}"


def _server_registry(registry: str | None = None, base_dir: Path | None = None) -> str:
    config = _load_client_config(base_dir)
    return _normalize_registry_reference(registry or config["server"]["registry"])


def _backend_url(backend_url: str | None = None, base_dir: Path | None = None) -> str:
    config = _load_client_config(base_dir)
    return backend_url or config["server"]["backend_url"]


def _build_remote_image_ref(
    repository: str,
    remote_tag: str = "latest",
    registry: str | None = None,
    base_dir: Path | None = None,
) -> str:
    registry_host = _server_registry(registry, base_dir)
    return f"{registry_host.rstrip('/')}/{repository}:{remote_tag}"


def _registry_host_port(registry: str) -> tuple[str, int]:
    normalized = _normalize_registry_reference(registry)
    if ":" in normalized:
        host, port = normalized.rsplit(":", maxsplit=1)
        return host, int(port)
    return normalized, 443


def _wireguard_windows_exe() -> str | None:
    candidates = [
        shutil.which("wireguard"),
        shutil.which("wireguard.exe"),
        r"C:\Program Files\WireGuard\wireguard.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def _wireguard_keygen_exe() -> str | None:
    direct = shutil.which("wg") or shutil.which("wg.exe")
    if direct:
        return direct

    wireguard_gui = _wireguard_windows_exe()
    if wireguard_gui:
        sibling = Path(wireguard_gui).with_name("wg.exe")
        if sibling.exists():
            return str(sibling)
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_secret_value(value: str) -> str:
    if not value:
        return value
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-4:]}"


def _windows_wireguard_helper_installed() -> bool:
    if not is_windows_platform():
        return False
    return bool(_run_command(wireguard_helper_query_task_command())["ok"])


def _run_windows_wireguard_helper(
    *,
    action: str,
    config_path: str | None = None,
    interface_name: str | None = None,
    wireguard_exe: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    if not is_windows_platform():
        return {"ok": False, "error": "not_windows"}
    if not _windows_wireguard_helper_installed():
        return {
            "ok": False,
            "error": "wireguard_elevated_helper_not_installed",
            "task_name": wireguard_helper_task_name(),
        }

    request_path = wireguard_helper_request_path()
    result_path = wireguard_helper_result_path()
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    payload = {
        "request_id": request_id,
        "action": action,
        "config_path": config_path or "",
        "interface_name": interface_name or "",
        "wireguard_exe": wireguard_exe or "",
        "requested_at": _utc_now_iso(),
    }
    request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cleanup_warning: dict[str, Any] | None = None
    if result_path.exists():
        try:
            result_path.unlink()
        except OSError as exc:
            cleanup_warning = {
                "warning": "wireguard_helper_result_cleanup_failed",
                "result_path": str(result_path),
                "error": str(exc),
            }

    run_result = _run_command(wireguard_helper_run_task_command())
    if not run_result["ok"]:
        return {"ok": False, "error": "wireguard_elevated_helper_run_failed", "run_result": run_result}

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                time.sleep(1)
                continue
            if result.get("request_id") == request_id:
                response = {"ok": bool(result.get("ok")), "helper_result": result, "run_result": run_result}
                if cleanup_warning:
                    response["cleanup_warning"] = cleanup_warning
                return response
        time.sleep(1)

    return {
        "ok": False,
        "error": "wireguard_elevated_helper_timeout",
        "task_name": wireguard_helper_task_name(),
        "request_path": str(request_path),
        "result_path": str(result_path),
        "run_result": run_result,
        "cleanup_warning": cleanup_warning,
    }


def _wait_for_docker(timeout_seconds: int = 60) -> dict[str, Any]:
    last_result: dict[str, Any] = {"ok": False, "error": "docker_not_ready"}
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        last_result = _run_command(["docker", "info"])
        if last_result["ok"]:
            return last_result
        time.sleep(2)
    return last_result


def _ensure_device_fingerprint(base_dir: Path | None = None) -> str:
    config = _load_client_config(base_dir)
    fingerprint = config["auth"].get("device_fingerprint") or ""
    if fingerprint:
        return fingerprint

    fingerprint = uuid.uuid4().hex
    config["auth"]["device_fingerprint"] = fingerprint
    _save_client_config(config, base_dir)
    return fingerprint


def _default_node_key(base_dir: Path | None = None) -> str:
    fingerprint = _ensure_device_fingerprint(base_dir)
    return f"{socket.gethostname().lower()}-{fingerprint[:12]}"


def _extract_share_percent(intent: str) -> int:
    match = re.search(r"(\d{1,3})\s*%", intent)
    if match is None:
        return 10
    return max(1, min(int(match.group(1)), 100))


def _verify_server_certificate(registry_host: str, registry_port: int) -> dict[str, Any]:
    context = ssl.create_default_context()
    try:
        with socket.create_connection((registry_host, registry_port), timeout=10) as raw_socket:
            with context.wrap_socket(raw_socket, server_hostname=registry_host) as tls_socket:
                certificate = tls_socket.getpeercert()
        return {
            "ok": True,
            "trusted": True,
            "subject": certificate.get("subject", ()),
            "issuer": certificate.get("issuer", ()),
        }
    except ssl.SSLCertVerificationError as exc:
        return {
            "ok": False,
            "trusted": False,
            "error_type": "certificate_verification_failed",
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "trusted": False,
            "error_type": "tls_connection_failed",
            "error": str(exc),
        }


mcp = FastMCP(
    name="seller-node-agent",
    instructions=(
        "Local seller node client MCP server. "
        "Use it to configure the local seller environment, prepare or connect "
        "WireGuard, inspect and start Docker, join Swarm, build and push images, "
        "and manage images uploaded to the server registry."
    ),
)


@mcp.tool(description="Return a basic liveness payload for the local seller client agent.")
def ping() -> dict[str, Any]:
    return {"status": "ok", "agent": "seller-node-agent"}


@mcp.tool(description="Inspect local OS, CPU, memory, disk, hostname, and basic network facts.")
def host_summary() -> dict[str, Any]:
    boot_time = psutil.boot_time()
    net_if_addrs = psutil.net_if_addrs()
    interfaces: dict[str, list[dict[str, Any]]] = {}

    for interface_name, addresses in net_if_addrs.items():
        interface_rows: list[dict[str, Any]] = []
        for address in addresses:
            interface_rows.append(
                {
                    "family": str(address.family),
                    "address": address.address,
                    "netmask": address.netmask,
                }
            )
        interfaces[interface_name] = interface_rows

    disk_path = str(Path.cwd())
    try:
        disk_usage = psutil.disk_usage(disk_path)
    except Exception:
        disk_usage = shutil.disk_usage(disk_path)
    memory = psutil.virtual_memory()

    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "memory_total_mb": round(memory.total / 1024 / 1024, 2),
        "memory_available_mb": round(memory.available / 1024 / 1024, 2),
        "disk_total_gb": round(disk_usage.total / 1024 / 1024 / 1024, 2),
        "disk_free_gb": round(disk_usage.free / 1024 / 1024 / 1024, 2),
        "boot_time_epoch": boot_time,
        "interfaces": interfaces,
    }


@mcp.tool(description="Show whether common local prerequisites are present for the seller node client.")
def environment_check() -> dict[str, Any]:
    return {
        "docker_cli": shutil.which("docker"),
        "python": shutil.which("python"),
        "curl": shutil.which("curl"),
        "git": shutil.which("git"),
        "codex_cli": shutil.which("codex"),
        "wireguard_cli": shutil.which("wg"),
        "wireguard_quick": shutil.which("wg-quick"),
        "wireguard_windows_exe": _wireguard_windows_exe(),
        "current_workdir": os.getcwd(),
        "platform": platform.system(),
    }


@mcp.tool(description="Create the local seller client workspace and store the server endpoints.")
def configure_environment(
    manager_host: str = DEFAULT_MANAGER_HOST,
    manager_port: int = DEFAULT_MANAGER_PORT,
    registry: str = DEFAULT_REGISTRY,
    portainer_url: str = DEFAULT_PORTAINER_URL,
    backend_url: str = DEFAULT_BACKEND_URL,
    wireguard_interface: str = DEFAULT_WG_INTERFACE,
    wireguard_endpoint_host: str = "",
    wireguard_endpoint_port: int = 0,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    _ensure_client_dirs(base_dir)
    config = _load_client_config(base_dir)
    normalized_registry = _normalize_registry_reference(registry)
    config["server"].update(
        {
            "manager_host": manager_host,
            "manager_port": manager_port,
            "registry": normalized_registry,
            "portainer_url": portainer_url,
            "backend_url": backend_url,
        }
    )
    config["wireguard"].update(
        {
            "interface": wireguard_interface,
            "endpoint_host": wireguard_endpoint_host,
            "endpoint_port": wireguard_endpoint_port,
            "config_path": str(_wireguard_config_path(wireguard_interface, base_dir)),
        }
    )
    return _save_client_config(config, base_dir)


@mcp.tool(description="Read the local seller client configuration from disk.")
def get_client_config(mask_secrets: bool = True, state_dir: str | None = None) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    rendered = _mask_config_secrets(config) if mask_secrets else config
    return {
        "ok": True,
        "config_path": str(_config_path(base_dir)),
        "data": rendered,
        "dirs": _ensure_client_dirs(base_dir),
    }


@mcp.tool(description="Fetch the seller runtime CodeX bootstrap from the platform backend.")
def fetch_codex_runtime_bootstrap(
    backend_url: str | None = None,
    state_dir: str | None = None,
    mask_secret: bool = True,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    access_token = config["auth"].get("access_token") or ""
    if not access_token:
        return {"ok": False, "error": "missing_access_token"}

    response = _run_backend_request(
        "GET",
        "/api/v1/platform/runtime/codex",
        backend_url=_backend_url(backend_url, base_dir),
        bearer_token=access_token,
    )
    if not response["ok"]:
        return response

    parsed = json.loads(response["body"])
    config["runtime"]["codex_provider"] = parsed.get("provider", {}).get("name", "")
    config["runtime"]["codex_model"] = parsed.get("model", "")
    config["runtime"]["codex_runtime_ready"] = bool(parsed.get("auth", {}).get("OPENAI_API_KEY"))
    config["runtime"]["codex_last_bootstrap_at"] = _utc_now_iso()
    _save_client_config(config, base_dir)

    if mask_secret and parsed.get("auth", {}).get("OPENAI_API_KEY"):
        parsed["auth"]["OPENAI_API_KEY"] = _mask_secret_value(parsed["auth"]["OPENAI_API_KEY"])
        response["body"] = json.dumps(parsed, ensure_ascii=False)
    response["data"] = parsed
    return response


@mcp.tool(description="Generate a local WireGuard keypair using the installed wg CLI.")
def generate_wireguard_keypair() -> dict[str, Any]:
    wg_bin = _wireguard_keygen_exe()
    if not wg_bin:
        return {"ok": False, "error": "wireguard_keygen_not_found"}

    private_result = _run_command([wg_bin, "genkey"])
    if not private_result["ok"] or not private_result["stdout"]:
        return {"ok": False, "stage": "private_key", "private_result": private_result}

    private_key = private_result["stdout"].strip()
    public_process = subprocess.run(
        [wg_bin, "pubkey"],
        input=private_key + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    public_key = public_process.stdout.strip()
    public_result = {
        "command": [wg_bin, "pubkey"],
        "returncode": public_process.returncode,
        "stdout": public_key,
        "stderr": public_process.stderr.strip(),
        "ok": public_process.returncode == 0 and bool(public_key),
    }
    if not public_result["ok"]:
        return {"ok": False, "stage": "public_key", "private_result": {"ok": True}, "public_result": public_result}

    return {
        "ok": True,
        "private_key": private_key,
        "public_key": public_key,
        "wg_bin": wg_bin,
    }


@mcp.tool(description="Request a WireGuard bootstrap profile from the platform backend for the current node.")
def request_wireguard_bootstrap(
    client_public_key: str,
    backend_url: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    node_token = config["auth"].get("node_registration_token") or ""
    if not node_token:
        return {"ok": False, "error": "missing_node_registration_token"}

    response = _run_backend_request(
        "POST",
        "/api/v1/platform/nodes/wireguard/bootstrap",
        backend_url=_backend_url(backend_url, base_dir),
        bearer_token=node_token,
        payload={
            "node_id": _default_node_key(base_dir),
            "client_public_key": client_public_key,
        },
    )
    if response["ok"]:
        response["data"] = json.loads(response["body"])
    return response


@mcp.tool(description="Generate local WireGuard keys, fetch bootstrap settings from the platform backend, and write the local profile.")
def bootstrap_wireguard_from_platform(
    backend_url: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    keypair_result = generate_wireguard_keypair()
    if not keypair_result["ok"]:
        return {"ok": False, "stage": "keypair", "keypair_result": keypair_result}

    bootstrap_result = request_wireguard_bootstrap(
        client_public_key=keypair_result["public_key"],
        backend_url=backend_url,
        state_dir=state_dir,
    )
    if not bootstrap_result["ok"]:
        return {"ok": False, "stage": "backend_bootstrap", "bootstrap_result": bootstrap_result}

    bootstrap = bootstrap_result["data"]
    profile_result = prepare_wireguard_profile(
        server_public_key=bootstrap["server_public_key"],
        client_private_key=keypair_result["private_key"],
        client_address=bootstrap["client_address"],
        endpoint_host=bootstrap["server_endpoint_host"],
        endpoint_port=bootstrap["server_endpoint_port"],
        allowed_ips=bootstrap["allowed_ips"],
        interface_name=bootstrap["interface_name"],
        dns=bootstrap.get("dns") or "",
        persistent_keepalive=bootstrap["persistent_keepalive"],
        state_dir=state_dir,
    )
    if not profile_result["ok"]:
        return {
            "ok": False,
            "stage": "prepare_profile",
            "bootstrap_result": bootstrap_result,
            "profile_result": profile_result,
        }

    config = _load_client_config(base_dir)
    config["wireguard"]["client_public_key"] = keypair_result["public_key"]
    config["runtime"]["wireguard_profile_status"] = "prepared"
    config["runtime"]["wireguard_last_bootstrap_at"] = _utc_now_iso()
    config["runtime"]["wireguard_activation_mode"] = bootstrap.get("activation_mode", "")
    _save_client_config(config, base_dir)

    activation_result: dict[str, Any] | None = None
    if not bootstrap.get("server_peer_apply_required", True):
        activation_result = connect_server_vpn(interface_name=bootstrap["interface_name"], state_dir=state_dir)
        config = _load_client_config(base_dir)
        config["runtime"]["wireguard_profile_status"] = "active" if activation_result.get("ok") else "activation_failed"
        _save_client_config(config, base_dir)

    return {
        "ok": True,
        "activation_mode": bootstrap.get("activation_mode", ""),
        "server_peer_apply_required": bootstrap.get("server_peer_apply_required", True),
        "keypair_result": {
            "ok": True,
            "public_key": keypair_result["public_key"],
            "wg_bin": keypair_result["wg_bin"],
        },
        "bootstrap_result": bootstrap_result,
        "profile_result": profile_result,
        "activation_result": activation_result,
    }


@mcp.tool(description="Register a seller account on the platform backend.")
def register_seller_account(
    email: str,
    password: str,
    display_name: str | None = None,
    backend_url: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    response = _run_backend_request(
        "POST",
        "/api/v1/auth/register",
        backend_url=_backend_url(backend_url, base_dir),
        payload={
            "email": email,
            "password": password,
            "display_name": display_name,
        },
    )
    config = _load_client_config(base_dir)
    if response["ok"]:
        config["auth"]["seller_email"] = email
        _save_client_config(config, base_dir)
    return response


@mcp.tool(description="Login a seller account and persist the backend access token locally.")
def login_seller_account(
    email: str,
    password: str,
    backend_url: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    response = _run_backend_request(
        "POST",
        "/api/v1/auth/login",
        backend_url=_backend_url(backend_url, base_dir),
        payload={"email": email, "password": password},
    )
    config = _load_client_config(base_dir)
    if response["ok"]:
        parsed = json.loads(response["body"])
        config["auth"]["seller_email"] = email
        config["auth"]["access_token"] = parsed["access_token"]
        _save_client_config(config, base_dir)
        response["data"] = parsed
    return response


@mcp.tool(description="Fetch a Docker Swarm worker join token from the platform backend.")
def fetch_swarm_worker_join_token(
    backend_url: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    access_token = config["auth"].get("access_token") or ""
    if not access_token:
        return {"ok": False, "error": "missing_access_token"}

    response = _run_backend_request(
        "GET",
        "/api/v1/platform/swarm/worker-join-token",
        backend_url=_backend_url(backend_url, base_dir),
        bearer_token=access_token,
    )
    if response["ok"]:
        parsed = json.loads(response["body"])
        config["server"]["manager_host"] = parsed["manager_host"]
        config["server"]["manager_port"] = parsed["manager_port"]
        _save_client_config(config, base_dir)
        response["data"] = parsed
    return response


@mcp.tool(description="Issue a node registration token from the platform backend for this seller.")
def issue_node_registration_token(
    label: str | None = None,
    expires_hours: int = 72,
    backend_url: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    access_token = config["auth"].get("access_token") or ""
    if not access_token:
        return {"ok": False, "error": "missing_access_token"}

    response = _run_backend_request(
        "POST",
        "/api/v1/platform/node-registration-token",
        backend_url=_backend_url(backend_url, base_dir),
        bearer_token=access_token,
        payload={"label": label, "expires_hours": expires_hours},
    )
    if response["ok"]:
        parsed = json.loads(response["body"])
        config["auth"]["node_registration_token"] = parsed["node_registration_token"]
        _save_client_config(config, base_dir)
        response["data"] = parsed
    return response


@mcp.tool(description="Ensure the local Docker engine is joined to the platform Swarm as a worker.")
def ensure_joined_to_platform_swarm(
    backend_url: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    current_swarm = swarm_summary()
    current_state = current_swarm.get("info", {}).get("stdout", "")
    if current_swarm.get("info", {}).get("ok") and "state=active" in current_state:
        return {
            "ok": True,
            "action": "already_joined",
            "swarm_summary": current_swarm,
        }

    token_result = fetch_swarm_worker_join_token(backend_url=backend_url, state_dir=state_dir)
    if not token_result["ok"]:
        return {"ok": False, "stage": "fetch_join_token", "token_result": token_result}

    payload = token_result["data"]
    join_result = join_swarm_manager(
        join_token=payload["join_token"],
        manager_host=payload["manager_host"],
        manager_port=payload["manager_port"],
    )
    if not join_result["ok"]:
        return {"ok": False, "stage": "join_swarm", "token_result": token_result, "join_result": join_result}

    post_join_swarm = swarm_summary()
    return {
        "ok": bool(post_join_swarm.get("info", {}).get("ok")),
        "action": "joined",
        "token_result": token_result,
        "join_result": join_result,
        "swarm_summary": post_join_swarm,
    }


@mcp.tool(description="Register the current machine as a seller node on the platform backend.")
def register_node_with_platform(
    shared_percent_preference: int = 10,
    seller_intent: str | None = None,
    node_class: str | None = "cpu-basic",
    backend_url: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    node_token = config["auth"].get("node_registration_token") or ""
    if not node_token:
        return {"ok": False, "error": "missing_node_registration_token"}

    docker_status_payload = docker_summary()
    swarm_status_payload = swarm_summary()
    docker_status_text = (
        docker_status_payload["info"]["stdout"] if docker_status_payload.get("ok") else "unavailable"
    )
    swarm_status_text = (
        swarm_status_payload["info"]["stdout"]
        if swarm_status_payload.get("info", {}).get("ok")
        else "unknown"
    )
    payload = {
        "node_id": _default_node_key(base_dir),
        "device_fingerprint": _ensure_device_fingerprint(base_dir),
        "hostname": socket.gethostname(),
        "system": platform.system(),
        "machine": platform.machine(),
        "shared_percent_preference": shared_percent_preference,
        "capabilities": host_summary(),
        "seller_intent": seller_intent,
        "docker_status": docker_status_text,
        "swarm_state": swarm_status_text,
        "node_class": node_class,
    }
    response = _run_backend_request(
        "POST",
        "/api/v1/platform/nodes/register",
        backend_url=_backend_url(backend_url, base_dir),
        bearer_token=node_token,
        payload=payload,
    )
    if response["ok"]:
        response["data"] = json.loads(response["body"])
    return response


@mcp.tool(description="Send a node heartbeat to the platform backend.")
def send_node_heartbeat(
    status: str = "available",
    backend_url: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    node_token = config["auth"].get("node_registration_token") or ""
    if not node_token:
        return {"ok": False, "error": "missing_node_registration_token"}

    docker_status_payload = docker_summary()
    swarm_status_payload = swarm_summary()
    docker_status_text = (
        docker_status_payload["info"]["stdout"] if docker_status_payload.get("ok") else "unavailable"
    )
    swarm_status_text = (
        swarm_status_payload["info"]["stdout"]
        if swarm_status_payload.get("info", {}).get("ok")
        else "unknown"
    )
    response = _run_backend_request(
        "POST",
        "/api/v1/platform/nodes/heartbeat",
        backend_url=_backend_url(backend_url, base_dir),
        bearer_token=node_token,
        payload={
            "node_id": _default_node_key(base_dir),
            "status": status,
            "docker_status": docker_status_text,
            "swarm_state": swarm_status_text,
            "capabilities": host_summary(),
        },
    )
    if response["ok"]:
        response["data"] = json.loads(response["body"])
    return response


@mcp.tool(description="Report an uploaded image artifact to the platform backend.")
def report_image_to_platform(
    repository: str,
    tag: str,
    registry: str,
    digest: str | None = None,
    source_image: str | None = None,
    status: str = "uploaded",
    backend_url: str | None = None,
    state_dir: str | None = None,
    timeout_seconds: float = 240,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    node_token = config["auth"].get("node_registration_token") or ""
    if not node_token:
        return {"ok": False, "error": "missing_node_registration_token"}

    response = _run_backend_request(
        "POST",
        "/api/v1/platform/images/report",
        backend_url=_backend_url(backend_url, base_dir),
        bearer_token=node_token,
        payload={
            "node_id": _default_node_key(base_dir),
            "repository": repository,
            "tag": tag,
            "digest": digest,
            "registry": _normalize_registry_reference(registry),
            "source_image": source_image,
            "status": status,
        },
        timeout_seconds=timeout_seconds,
    )
    if response["ok"]:
        response["data"] = json.loads(response["body"])
    return response


@mcp.tool(description="Explain a seller onboarding intent in simple language and derive the target shared percentage.")
def explain_seller_intent(intent: str) -> dict[str, Any]:
    share_percent = _extract_share_percent(intent)
    return {
        "ok": True,
        "intent": intent,
        "share_percent_preference": share_percent,
        "explanation": (
            f"你的意思可以理解为：想把电脑大约 {share_percent}% 的可用能力接入平台，"
            "让平台把这台机器登记成可出租节点，并在接入成功后开始统计可出租状态。"
            "第一阶段不会真的做硬件 10% 的精确切片，而是把它记录为卖家的共享意向和节点偏好。"
        ),
    }


@mcp.tool(description="Run the minimal seller onboarding flow from a natural-language intent.")
def onboard_seller_from_intent(
    intent: str,
    email: str,
    password: str,
    display_name: str | None = None,
    backend_url: str = DEFAULT_BACKEND_URL,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    explanation = explain_seller_intent(intent)
    shared_percent = explanation["share_percent_preference"]

    configure_result = configure_environment(backend_url=backend_url, state_dir=state_dir)
    env_result = environment_check()
    docker_result = ensure_docker_engine()

    register_result = register_seller_account(
        email=email,
        password=password,
        display_name=display_name,
        backend_url=backend_url,
        state_dir=state_dir,
    )
    if not register_result["ok"] and register_result.get("status") != 409:
        return {
            "ok": False,
            "stage": "register",
            "explanation": explanation,
            "configure_result": configure_result,
            "environment": env_result,
            "docker": docker_result,
            "register_result": register_result,
        }

    login_result = login_seller_account(
        email=email,
        password=password,
        backend_url=backend_url,
        state_dir=state_dir,
    )
    if not login_result["ok"]:
        return {
            "ok": False,
            "stage": "login",
            "explanation": explanation,
            "configure_result": configure_result,
            "environment": env_result,
            "docker": docker_result,
            "login_result": login_result,
        }

    node_token_result = issue_node_registration_token(
        label="seller-onboarding",
        backend_url=backend_url,
        state_dir=state_dir,
    )
    if not node_token_result["ok"]:
        return {
            "ok": False,
            "stage": "issue_node_token",
            "login_result": login_result,
            "node_token_result": node_token_result,
        }

    codex_runtime_result = fetch_codex_runtime_bootstrap(
        backend_url=backend_url,
        state_dir=state_dir,
        mask_secret=True,
    )

    register_node_result = register_node_with_platform(
        shared_percent_preference=shared_percent,
        seller_intent=intent,
        backend_url=backend_url,
        state_dir=state_dir,
    )
    if not register_node_result["ok"]:
        return {
            "ok": False,
            "stage": "register_node",
            "register_node_result": register_node_result,
        }

    wireguard_result = bootstrap_wireguard_from_platform(
        backend_url=backend_url,
        state_dir=state_dir,
    )
    if not wireguard_result["ok"]:
        return {
            "ok": False,
            "stage": "wireguard_bootstrap",
            "login_result": login_result,
            "node_token_result": node_token_result,
            "codex_runtime_result": codex_runtime_result,
            "register_node_result": register_node_result,
            "wireguard_result": wireguard_result,
        }

    swarm_join_result = ensure_joined_to_platform_swarm(
        backend_url=backend_url,
        state_dir=state_dir,
    )
    if not swarm_join_result["ok"]:
        return {
            "ok": False,
            "stage": "swarm_join",
            "login_result": login_result,
            "node_token_result": node_token_result,
            "codex_runtime_result": codex_runtime_result,
            "register_node_result": register_node_result,
            "wireguard_result": wireguard_result,
            "swarm_join_result": swarm_join_result,
        }

    heartbeat_result = send_node_heartbeat(
        status="available",
        backend_url=backend_url,
        state_dir=state_dir,
    )
    return {
        "ok": heartbeat_result["ok"],
        "message": "卖家节点接入流程已执行，节点信息、CodeX runtime 和 WireGuard profile 已从平台侧完成第一轮准备。",
        "explanation": explanation,
        "configure_result": configure_result,
        "environment": env_result,
        "docker": docker_result,
        "login_result": login_result,
        "node_token_result": node_token_result,
        "codex_runtime_result": codex_runtime_result,
        "register_node_result": register_node_result,
        "wireguard_result": wireguard_result,
        "swarm_join_result": swarm_join_result,
        "heartbeat_result": heartbeat_result,
        "device_fingerprint": _ensure_device_fingerprint(base_dir),
        "node_id": _default_node_key(base_dir),
    }


@mcp.tool(description="Render and persist a WireGuard client profile for the seller node.")
def prepare_wireguard_profile(
    server_public_key: str,
    client_private_key: str,
    client_address: str,
    endpoint_host: str,
    endpoint_port: int,
    allowed_ips: str,
    interface_name: str = DEFAULT_WG_INTERFACE,
    dns: str = "",
    persistent_keepalive: int = 25,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config_path = _wireguard_config_path(interface_name, base_dir)

    lines = [
        "[Interface]",
        f"PrivateKey = {client_private_key}",
        f"Address = {client_address}",
    ]
    if dns:
        lines.append(f"DNS = {dns}")

    lines.extend(
        [
            "",
            "[Peer]",
            f"PublicKey = {server_public_key}",
            f"AllowedIPs = {allowed_ips}",
            f"Endpoint = {endpoint_host}:{endpoint_port}",
            f"PersistentKeepalive = {persistent_keepalive}",
            "",
        ]
    )
    config_path.write_text("\n".join(lines), encoding="utf-8")

    config = _load_client_config(base_dir)
    config["wireguard"].update(
        {
            "interface": interface_name,
            "config_path": str(config_path),
            "endpoint_host": endpoint_host,
            "endpoint_port": endpoint_port,
            "client_address": client_address,
            "allowed_ips": allowed_ips,
            "dns": dns,
        }
    )
    _save_client_config(config, base_dir)
    return {"ok": True, "config_path": str(config_path)}


@mcp.tool(description="Inspect local WireGuard prerequisites and interface state.")
def wireguard_summary(interface_name: str = DEFAULT_WG_INTERFACE, state_dir: str | None = None) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    resolved_interface = interface_name or config["wireguard"]["interface"]
    result = {
        "platform": platform.system(),
        "wg_cli": shutil.which("wg"),
        "wg_quick": shutil.which("wg-quick"),
        "wireguard_windows_exe": _wireguard_windows_exe(),
        "wireguard_elevated_helper_task": wireguard_helper_task_name() if is_windows_platform() else "",
        "wireguard_elevated_helper_installed": _windows_wireguard_helper_installed() if is_windows_platform() else False,
        "config_path": config["wireguard"]["config_path"] or str(_wireguard_config_path(resolved_interface, base_dir)),
        "client_address": config["wireguard"].get("client_address") or "",
        "client_public_key": config["wireguard"].get("client_public_key") or "",
        "profile_status": config.get("runtime", {}).get("wireguard_profile_status") or "",
    }

    if shutil.which("wg"):
        result["show"] = _run_command(["wg", "show"])
    else:
        result["show"] = {"ok": False, "error": "wg_cli_not_found"}
    return result


@mcp.tool(description="Bring up the local WireGuard tunnel that should connect the seller node to the server.")
def connect_server_vpn(interface_name: str = DEFAULT_WG_INTERFACE, state_dir: str | None = None) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    resolved_interface = interface_name or config["wireguard"]["interface"]
    config_path = Path(config["wireguard"]["config_path"] or _wireguard_config_path(resolved_interface, base_dir))

    if not config_path.exists():
        return {"ok": False, "error": "wireguard_config_missing", "config_path": str(config_path)}

    if platform.system() == "Windows":
        wireguard_exe = _wireguard_windows_exe()
        if wireguard_exe:
            if windows_is_elevated():
                return _run_command([wireguard_exe, "/installtunnelservice", str(config_path)])
            if _windows_wireguard_helper_installed():
                helper_result = _run_windows_wireguard_helper(
                    action="install_tunnel_service",
                    config_path=str(config_path),
                    interface_name=resolved_interface,
                    wireguard_exe=wireguard_exe,
                )
                return {
                    "ok": bool(helper_result.get("ok")),
                    "mode": "elevated_helper" if helper_result.get("ok") else "elevated_helper_failed",
                    **helper_result,
                }
            return {
                "ok": False,
                "error": "wireguard_elevated_helper_not_installed",
                "task_name": wireguard_helper_task_name(),
                "install_hint": "Run environment_check/install_windows.ps1 -Apply once as administrator.",
            }
        if shutil.which("wg-quick"):
            return _run_command(["wg-quick", "up", str(config_path)])
        return {"ok": False, "error": "wireguard_runtime_not_found"}

    if shutil.which("wg-quick"):
        return _run_command(["wg-quick", "up", str(config_path)])
    return {"ok": False, "error": "wg_quick_not_found"}


@mcp.tool(description="Tear down the local WireGuard tunnel used by the seller node.")
def disconnect_server_vpn(interface_name: str = DEFAULT_WG_INTERFACE, state_dir: str | None = None) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    config = _load_client_config(base_dir)
    resolved_interface = interface_name or config["wireguard"]["interface"]
    config_path = Path(config["wireguard"]["config_path"] or _wireguard_config_path(resolved_interface, base_dir))

    if platform.system() == "Windows":
        wireguard_exe = _wireguard_windows_exe()
        if wireguard_exe:
            if windows_is_elevated():
                return _run_command([wireguard_exe, "/uninstalltunnelservice", resolved_interface])
            if _windows_wireguard_helper_installed():
                helper_result = _run_windows_wireguard_helper(
                    action="uninstall_tunnel_service",
                    interface_name=resolved_interface,
                    wireguard_exe=wireguard_exe,
                )
                return {
                    "ok": bool(helper_result.get("ok")),
                    "mode": "elevated_helper" if helper_result.get("ok") else "elevated_helper_failed",
                    **helper_result,
                }
            return {
                "ok": False,
                "error": "wireguard_elevated_helper_not_installed",
                "task_name": wireguard_helper_task_name(),
                "install_hint": "Run environment_check/install_windows.ps1 -Apply once as administrator.",
            }
        if shutil.which("wg-quick"):
            return _run_command(["wg-quick", "down", str(config_path)])
        return {"ok": False, "error": "wireguard_runtime_not_found"}

    if shutil.which("wg-quick"):
        return _run_command(["wg-quick", "down", str(config_path)])
    return {"ok": False, "error": "wg_quick_not_found"}


@mcp.tool(description="Check whether Docker CLI is available and inspect daemon and swarm status.")
def docker_summary() -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}

    version = _run_command(["docker", "version", "--format", "client={{.Client.Version}} server={{.Server.Version}}"])
    info = _run_command(
        [
            "docker",
            "info",
            "--format",
            (
                "swarm_state={{.Swarm.LocalNodeState}} "
                "control={{.Swarm.ControlAvailable}} "
                "node_id={{.Swarm.NodeID}} "
                "node_addr={{.Swarm.NodeAddr}} "
                "cpus={{.NCPU}} mem={{.MemTotal}}"
            ),
        ]
    )
    return {"ok": version["ok"] and info["ok"], "version": version, "info": info}


@mcp.tool(description="Attempt to start the local Docker engine if it is installed but not running.")
def ensure_docker_engine(timeout_seconds: int = 60) -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}

    preflight = _run_command(["docker", "info"])
    if preflight["ok"]:
        return {"ok": True, "action": "already_running", "docker_info": preflight}

    attempts: list[dict[str, Any]] = []
    if platform.system() == "Windows":
        attempts.append(
            _run_command(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "try { Start-Service com.docker.service -ErrorAction Stop; 'STARTED_SERVICE' } catch { $_.Exception.Message }",
                ]
            )
        )
        docker_desktop = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
        if docker_desktop.exists():
            attempts.append(
                _run_command(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        f"Start-Process '{docker_desktop}'",
                    ]
                )
            )
    else:
        if shutil.which("systemctl"):
            attempts.append(_run_command(["systemctl", "start", "docker"]))
        elif shutil.which("service"):
            attempts.append(_run_command(["service", "docker", "start"]))

    waited = _wait_for_docker(timeout_seconds)
    return {"ok": waited["ok"], "attempts": attempts, "docker_info": waited}


@mcp.tool(description="Join the local Docker engine to the remote Swarm manager.")
def join_swarm_manager(
    join_token: str,
    manager_host: str = DEFAULT_MANAGER_HOST,
    manager_port: int = DEFAULT_MANAGER_PORT,
) -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}

    return _run_command(
        [
            "docker",
            "swarm",
            "join",
            "--token",
            join_token,
            f"{manager_host}:{manager_port}",
        ]
    )


@mcp.tool(description="Leave the current Swarm as a worker node.")
def leave_swarm(force: bool = True) -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}
    command = ["docker", "swarm", "leave"]
    if force:
        command.append("--force")
    return _run_command(command)


@mcp.tool(description="Return local swarm status and node metadata if Docker is available.")
def swarm_summary() -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}

    return {
        "info": _run_command(
            [
                "docker",
                "info",
                "--format",
                (
                    "state={{.Swarm.LocalNodeState}} "
                    "node_id={{.Swarm.NodeID}} "
                    "node_addr={{.Swarm.NodeAddr}} "
                    "control={{.Swarm.ControlAvailable}}"
                ),
            ]
        ),
        "nodes": _run_command(["docker", "node", "ls"]),
    }


@mcp.tool(description="List local Docker images with repository, tag, id, size, and creation time.")
def list_docker_images() -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}
    return _docker_json(["docker", "images", "--format", "{{json .}}"])


@mcp.tool(description="List local Docker containers with status, image, names, and ports.")
def list_docker_containers(all_containers: bool = True) -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}
    command = ["docker", "ps"]
    if all_containers:
        command.append("-a")
    command.extend(["--format", "{{json .}}"])
    return _docker_json(command)


@mcp.tool(description="Create and start a Docker container from a local image.")
def create_docker_container(
    image: str,
    name: str | None = None,
    command: list[str] | None = None,
    environment: dict[str, str] | None = None,
    ports: dict[str, str] | None = None,
    volumes: dict[str, str] | None = None,
    detach: bool = True,
) -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}

    docker_command = ["docker", "run"]
    if detach:
        docker_command.append("-d")
    if name:
        docker_command.extend(["--name", name])
    if environment:
        for key, value in environment.items():
            docker_command.extend(["-e", f"{key}={value}"])
    if ports:
        for host_port, container_port in ports.items():
            docker_command.extend(["-p", f"{host_port}:{container_port}"])
    if volumes:
        for host_path, container_path in volumes.items():
            docker_command.extend(["-v", f"{host_path}:{container_path}"])
    docker_command.append(image)
    if command:
        docker_command.extend(command)
    return _run_command(docker_command)


@mcp.tool(description="Inspect a single Docker container by id or name.")
def inspect_container(container_id_or_name: str) -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}
    return _docker_json(["docker", "inspect", container_id_or_name])


@mcp.tool(description="Collect a no-stream Docker stats snapshot for a single container.")
def measure_container(container_id_or_name: str) -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}
    return _run_command(
        [
            "docker",
            "stats",
            "--no-stream",
            container_id_or_name,
            "--format",
            (
                "container={{.Container}} "
                "name={{.Name}} "
                "cpu={{.CPUPerc}} "
                "mem={{.MemUsage}} "
                "mem_perc={{.MemPerc}} "
                "net={{.NetIO}} "
                "block={{.BlockIO}} "
                "pids={{.PIDs}}"
            ),
        ]
    )


@mcp.tool(description="Build a Docker image from a local build context and tag it.")
def build_image(context_path: str, tag: str, dockerfile: str = "Dockerfile") -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}
    resolved_context = Path(context_path).expanduser().resolve()
    return _run_command(["docker", "build", "-t", tag, "-f", dockerfile, str(resolved_context)], cwd=str(resolved_context))


@mcp.tool(description="Tag a local image for the configured server registry.")
def tag_image_for_server(
    local_tag: str,
    repository: str,
    remote_tag: str = "latest",
    registry: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}

    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    remote_ref = _build_remote_image_ref(repository, remote_tag, registry, base_dir)
    result = _run_command(["docker", "tag", local_tag, remote_ref])
    result["remote_ref"] = remote_ref
    return result


@mcp.tool(description="Push a tagged Docker image to the configured server registry.")
def push_image(tag: str, retries: int = 2, retry_delay_seconds: float = 2.0) -> dict[str, Any]:
    if not _docker_available():
        return {"ok": False, "error": "docker_cli_not_found"}

    attempts: list[dict[str, Any]] = []
    total_attempts = max(1, retries + 1)
    for attempt in range(1, total_attempts + 1):
        result = _run_command(["docker", "push", tag])
        result["attempt"] = attempt
        attempts.append(result)
        if result["ok"] or attempt == total_attempts or not _is_transient_registry_push_error(result):
            if len(attempts) > 1:
                result["attempts"] = attempts
            return result
        time.sleep(retry_delay_seconds * attempt)

    return attempts[-1]


@mcp.tool(description="Tag a local image for the server registry and push it in one step.")
def push_image_to_server(
    local_tag: str,
    repository: str,
    remote_tag: str = "latest",
    registry: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    tag_result = tag_image_for_server(local_tag, repository, remote_tag, registry, state_dir)
    if not tag_result["ok"]:
        return {"ok": False, "tag_result": tag_result}

    push_result = push_image(tag_result["remote_ref"])
    config = _load_client_config(base_dir)
    if push_result["ok"]:
        config["docker"]["last_pushed_image"] = tag_result["remote_ref"]
        _save_client_config(config, base_dir)
    return {
        "ok": push_result["ok"],
        "remote_ref": tag_result["remote_ref"],
        "tag_result": tag_result,
        "push_result": push_result,
    }


@mcp.tool(description="Push a local image to the server registry and then report it to the platform backend.")
def push_and_report_image(
    local_tag: str,
    repository: str,
    remote_tag: str = "latest",
    registry: str | None = None,
    backend_url: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    push_result = push_image_to_server(
        local_tag=local_tag,
        repository=repository,
        remote_tag=remote_tag,
        registry=registry,
        state_dir=state_dir,
    )
    if not push_result["ok"]:
        return {"ok": False, "stage": "push", "push_result": push_result}

    remote_ref = push_result["remote_ref"]
    inspect_result = _run_command(
        ["docker", "image", "inspect", remote_ref, "--format", "{{index .RepoDigests 0}}"]
    )
    digest: str | None = None
    if inspect_result["ok"] and "@sha256:" in inspect_result["stdout"]:
        digest = inspect_result["stdout"].split("@", maxsplit=1)[1]

    report_result = report_image_to_platform(
        repository=repository,
        tag=remote_tag,
        digest=digest,
        registry=registry or _server_registry(None, Path(state_dir).expanduser().resolve() if state_dir else None),
        source_image=local_tag,
        backend_url=backend_url,
        state_dir=state_dir,
    )
    if not report_result["ok"]:
        return {"ok": False, "stage": "report", "push_result": push_result, "report_result": report_result}

    return {
        "ok": True,
        "remote_ref": remote_ref,
        "digest": digest,
        "push_result": push_result,
        "report_result": report_result,
    }


@mcp.tool(description="Probe a registry v2 HTTPS endpoint.")
def probe_registry(registry_url: str) -> dict[str, Any]:
    base_url = _registry_base_url(registry_url)
    return _run_registry_request("GET", f"{base_url}/v2/")


@mcp.tool(description="Check whether the remote registry is serving a publicly trusted HTTPS certificate.")
def fetch_registry_certificate(registry: str = DEFAULT_REGISTRY) -> dict[str, Any]:
    normalized_registry = _normalize_registry_reference(registry)
    host, port = _registry_host_port(normalized_registry)
    trust_probe = _verify_server_certificate(host, port)
    if not trust_probe.get("ok"):
        return {
            "ok": False,
            "registry": normalized_registry,
            "trust_probe": trust_probe,
            "error": trust_probe.get("error") or "registry_tls_check_failed",
        }
    return {
        "ok": True,
        "registry": normalized_registry,
        "trust_probe": trust_probe,
        "publicly_trusted": True,
    }


@mcp.tool(description="Check registry HTTPS access on the public pivotcompute.store endpoint.")
def configure_registry_trust(
    registry: str = DEFAULT_REGISTRY,
    restart_docker: bool = True,
) -> dict[str, Any]:
    normalized_registry = _normalize_registry_reference(registry)
    fetch_result = fetch_registry_certificate(normalized_registry)
    if not fetch_result["ok"]:
        return {"ok": False, "stage": "fetch_certificate", "fetch_result": fetch_result}

    probe_result = probe_registry(normalized_registry)
    if not probe_result["ok"]:
        return {"ok": False, "stage": "probe_registry", "fetch_result": fetch_result, "probe_result": probe_result}

    return {
        "ok": True,
        "registry": normalized_registry,
        "trust_mode": "public_https",
        "input_registry": registry,
        "legacy_input_upgraded": normalized_registry != registry.strip().rstrip("/"),
        "fetch_result": fetch_result,
        "probe_result": probe_result,
        "restart_docker_requested": restart_docker,
    }

    if fetch_result.get("publicly_trusted"):
        return {
            "ok": True,
            "registry": registry,
            "trust_mode": "public_https",
            "fetch_result": fetch_result,
            "install_result": {
                "ok": True,
                "action": "https_only",
                "detail": "Registry 证书已经由系统 CA 信任，无需额外安装本地 CA 或重启 Docker。",
            },
            "restart_result": None,
        }



@mcp.tool(description="List repositories currently uploaded to the server registry.")
def list_uploaded_images(registry: str | None = None, state_dir: str | None = None) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    base_url = _registry_base_url(_server_registry(registry, base_dir))
    catalog = _run_registry_request("GET", f"{base_url}/v2/_catalog")
    if not catalog["ok"]:
        return {"ok": False, "catalog": catalog}

    parsed = json.loads(catalog["body"] or "{}")
    repositories = parsed.get("repositories", [])
    return {"ok": True, "registry": base_url, "repositories": repositories}


@mcp.tool(description="List tags for a repository uploaded to the server registry.")
def list_uploaded_image_tags(
    repository: str,
    registry: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    base_url = _registry_base_url(_server_registry(registry, base_dir))
    response = _run_registry_request("GET", f"{base_url}/v2/{repository}/tags/list")
    if not response["ok"]:
        return {"ok": False, "response": response}
    return {"ok": True, "registry": base_url, "data": json.loads(response["body"] or "{}")}


@mcp.tool(description="Delete an uploaded image manifest from the server registry if deletion is enabled.")
def delete_uploaded_image(
    repository: str,
    reference: str,
    registry: str | None = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    base_dir = Path(state_dir).expanduser().resolve() if state_dir else None
    base_url = _registry_base_url(_server_registry(registry, base_dir))
    accept = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}

    head = _run_registry_request("HEAD", f"{base_url}/v2/{repository}/manifests/{reference}", headers=accept)
    if not head["ok"]:
        return {"ok": False, "stage": "head_manifest", "response": head}

    digest = head["headers"].get("Docker-Content-Digest") or head["headers"].get("docker-content-digest")
    if not digest:
        return {"ok": False, "stage": "digest_lookup", "response": head}

    delete = _run_registry_request("DELETE", f"{base_url}/v2/{repository}/manifests/{digest}")
    return {"ok": delete["ok"], "digest": digest, "response": delete}


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
