from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import os
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from buyer_client.agent_cli import (
    bootstrap_runtime_session_wireguard,
    create_runtime_session,
    disconnect_runtime_session_wireguard,
    handshake_runtime_gateway,
    read_runtime_session,
    renew_runtime_session as renew_backend_runtime_session,
    run_archive,
    run_github_repo,
    start_licensed_shell_session,
    start_shell_session,
    stop_runtime_session,
)
from buyer_client.codex_orchestrator import cancel_codex_job, codex_status, create_codex_job, get_codex_job
from buyer_client.runtime.gateway import (
    gateway_download_archive,
    gateway_exec_command,
    gateway_read_logs,
    gateway_upload_archive,
    request_gateway_json,
    gateway_shell_websocket_url,
)
from seller_client.agent_mcp import wireguard_summary
from seller_client.installer import bootstrap_client

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = str(REPO_ROOT / ".cache" / "buyer-web")
INDEX_HTML = REPO_ROOT / "buyer_client" / "web" / "index.html"

SESSION_STORE: dict[str, dict[str, Any]] = {}
ACTIVE_TERMINALS: dict[str, list[WebSocket]] = {}
TERMINAL_SESSION_STATES = {"completed", "failed", "stopped", "expired"}


class RunCodeRequest(BaseModel):
    backend_url: str = "http://127.0.0.1:8000"
    email: str
    password: str
    display_name: str | None = None
    seller_node_key: str
    runtime_image: str = "python:3.12-alpine"
    code_filename: str = "main.py"
    code_content: str = Field(min_length=1, max_length=200_000)
    requested_duration_minutes: int = Field(default=30, ge=1, le=720)
    state_dir: str | None = None


class RunArchiveRequest(BaseModel):
    backend_url: str = "http://127.0.0.1:8000"
    email: str
    password: str
    display_name: str | None = None
    seller_node_key: str
    source_path: str
    runtime_image: str = "python:3.12-alpine"
    working_dir: str | None = None
    run_command: str = ""
    requested_duration_minutes: int = Field(default=30, ge=1, le=720)
    state_dir: str | None = None


class RunGitHubRequest(BaseModel):
    backend_url: str = "http://127.0.0.1:8000"
    email: str
    password: str
    display_name: str | None = None
    seller_node_key: str
    repo_url: str
    repo_ref: str = "main"
    runtime_image: str = "python:3.12-alpine"
    working_dir: str | None = None
    run_command: str = ""
    requested_duration_minutes: int = Field(default=30, ge=1, le=720)
    state_dir: str | None = None


class StartShellRequest(BaseModel):
    backend_url: str = "http://127.0.0.1:8000"
    email: str
    password: str
    display_name: str | None = None
    seller_node_key: str
    runtime_image: str = "python:3.12-alpine"
    requested_duration_minutes: int = Field(default=30, ge=1, le=720)
    state_dir: str | None = None


class StartLicensedShellRequest(BaseModel):
    backend_url: str = "http://127.0.0.1:8000"
    email: str
    password: str
    display_name: str | None = None
    license_token: str = Field(min_length=8, max_length=512)
    state_dir: str | None = None


class ExecRequest(BaseModel):
    command: str = Field(min_length=1, max_length=2000)
    state_dir: str | None = None


class SessionUploadRequest(BaseModel):
    local_path: str = Field(min_length=1, max_length=2000)
    remote_path: str = Field(default="/workspace", min_length=1, max_length=2000)
    state_dir: str | None = None


class SessionDownloadRequest(BaseModel):
    remote_path: str = Field(min_length=1, max_length=2000)
    local_path: str = Field(min_length=1, max_length=2000)
    state_dir: str | None = None


class StopSessionRequest(BaseModel):
    state_dir: str | None = None


class RenewSessionRequest(BaseModel):
    additional_minutes: int = Field(default=30, ge=1, le=720)
    state_dir: str | None = None


class WireGuardRequest(BaseModel):
    state_dir: str | None = None


class ConnectRequest(BaseModel):
    state_dir: str | None = None
    activate_wireguard: bool = True


class WireGuardHelperInstallRequest(BaseModel):
    state_dir: str | None = None
    attempt_launch: bool = True


class CodexJobRequest(BaseModel):
    local_id: str = Field(min_length=1, max_length=128)
    workspace_path: str = Field(min_length=1, max_length=2000)
    prompt: str = Field(min_length=1, max_length=100_000)
    state_dir: str | None = None
    model: str = Field(default="", max_length=200)


class CodexJobCancelRequest(BaseModel):
    state_dir: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir(provided: str | None) -> Path:
    return Path(provided or DEFAULT_STATE_DIR).expanduser().resolve()


def _activity_path(state_dir: str | None) -> Path:
    root = _state_dir(state_dir)
    path = root / "logs" / "buyer-web-actions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_activity(state_dir: str | None, entry: dict[str, Any]) -> dict[str, Any]:
    payload = dict(entry)
    payload["timestamp"] = _utc_now_iso()
    path = _activity_path(state_dir)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return payload


def _read_activity(state_dir: str | None, limit: int = 20) -> list[dict[str, Any]]:
    path = _activity_path(state_dir)
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(items) >= limit:
            break
    return items


def _session_state_dir(record: dict[str, Any]) -> str:
    return str(_state_dir(record.get("state_dir")))


def _wireguard_helper_apply_command() -> str:
    return f'powershell -ExecutionPolicy Bypass -File "{REPO_ROOT / "environment_check" / "install_windows.ps1"}" -Apply'


def _wireguard_helper_status(state_dir: str | None) -> dict[str, Any]:
    resolved_state_dir = str(_state_dir(state_dir))
    install_command = _wireguard_helper_apply_command()
    try:
        summary = wireguard_summary(interface_name="wg-buyer", state_dir=resolved_state_dir)
    except Exception as exc:  # noqa: BLE001
        return {
            "wireguard_helper_ready": False,
            "wireguard_helper_error": str(exc),
            "wireguard_helper_install_hint": "Unable to inspect local WireGuard helper status.",
            "wireguard_helper_apply_command": install_command,
        }

    platform_name = str(summary.get("platform") or "")
    is_windows = platform_name.lower().startswith("win")
    helper_installed = bool(summary.get("wireguard_elevated_helper_installed"))
    runtime_available = bool(summary.get("wireguard_windows_exe") or summary.get("wg_quick") or summary.get("wg_cli"))
    if not is_windows:
        return {
            "wireguard_helper_ready": True,
            "wireguard_helper_error": "",
            "wireguard_helper_install_hint": "",
            "wireguard_helper_apply_command": "",
        }
    if helper_installed:
        return {
            "wireguard_helper_ready": True,
            "wireguard_helper_error": "",
            "wireguard_helper_install_hint": "",
            "wireguard_helper_apply_command": install_command,
        }
    if not runtime_available:
        return {
            "wireguard_helper_ready": False,
            "wireguard_helper_error": "wireguard_runtime_not_found",
            "wireguard_helper_install_hint": "Install WireGuard for Windows before connecting buyer sessions.",
            "wireguard_helper_apply_command": install_command,
        }
    return {
        "wireguard_helper_ready": False,
        "wireguard_helper_error": "wireguard_elevated_helper_not_installed",
        "wireguard_helper_install_hint": "Run the install command once as administrator, then retry Connect Gateway.",
        "wireguard_helper_apply_command": install_command,
    }


def _wireguard_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "wireguard_status": record.get("wireguard_status", ""),
        "wireguard_interface": record.get("wireguard_interface", ""),
        "wireguard_client_address": record.get("wireguard_client_address", ""),
        "seller_wireguard_target": record.get("seller_wireguard_target", ""),
        "wireguard_last_bootstrap_at": record.get("wireguard_last_bootstrap_at", ""),
        "wireguard_activation_mode": record.get("wireguard_activation_mode", ""),
    }


def _gateway_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "gateway_required": bool(record.get("gateway_required")),
        "gateway_protocol": record.get("gateway_protocol", ""),
        "gateway_port": record.get("gateway_port"),
        "gateway_service_name": record.get("gateway_service_name", ""),
        "gateway_status": record.get("gateway_status", ""),
        "gateway_last_seen_at": record.get("gateway_last_seen_at"),
        "gateway_host": record.get("gateway_host", ""),
        "gateway_access_host": record.get("gateway_local_override_host", "") or record.get("gateway_host", ""),
        "gateway_handshake_mode": record.get("gateway_handshake_mode", ""),
        "gateway_supported_features": list(record.get("gateway_supported_features") or []),
        "gateway_connected_at": record.get("gateway_connected_at"),
        "connection_status": record.get("connection_status", ""),
        "connection_mode": record.get("connection_mode", ""),
        "connect_source": record.get("connect_source", ""),
        "exec_mode": record.get("exec_mode", "gateway"),
    }


def _apply_gateway_handshake(record: dict[str, Any], payload: dict[str, Any]) -> None:
    record["gateway_service_name"] = payload.get("gateway_service_name", record.get("gateway_service_name", ""))
    record["gateway_protocol"] = payload.get("gateway_protocol", record.get("gateway_protocol", ""))
    record["gateway_host"] = payload.get("gateway_host", record.get("gateway_host", ""))
    record["gateway_port"] = payload.get("gateway_port", record.get("gateway_port"))
    record["gateway_handshake_mode"] = payload.get("handshake_mode", record.get("gateway_handshake_mode", ""))
    record["gateway_supported_features"] = [
        str(item) for item in (payload.get("supported_features") or record.get("gateway_supported_features") or [])
    ]
    record["gateway_connected_at"] = _utc_now_iso()
    record["gateway_status"] = payload.get("gateway_status") or record.get("gateway_status") or "running"
    record["gateway_last_seen_at"] = _utc_now_iso()
    record["connect_source"] = payload.get("connect_source", record.get("connect_source", "gateway_handshake"))
    record["connection_mode"] = "wireguard_gateway" if record.get("network_mode") == "wireguard" else "gateway_only"
    record["exec_mode"] = "gateway"
    record["gateway_local_override_host"] = ""
    if payload.get("seller_wireguard_target"):
        record["seller_wireguard_target"] = payload["seller_wireguard_target"]
    if record.get("network_mode") == "wireguard":
        record["connection_status"] = "connected" if record.get("wireguard_status") == "active" else "handshaken"
    else:
        record["connection_status"] = "connected"


def _compose_session_logs(record: dict[str, Any], remote_logs: str | None) -> str:
    parts = []
    remote = (remote_logs or "").strip()
    local_exec = (record.get("local_exec_history") or "").strip()
    if remote:
        parts.append(remote)
    if local_exec:
        parts.append(local_exec)
    return "\n\n".join(parts)


def _masked_session(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "local_id": record["local_id"],
        "backend_url": record["backend_url"],
        "buyer_email": record["buyer_email"],
        "session_id": record["session_id"],
        "order_id": record.get("order_id"),
        "license_token": record.get("license_token", ""),
        "seller_node_key": record["seller_node_key"],
        "runtime_image": record["runtime_image"],
        "code_filename": record["code_filename"],
        "session_mode": record.get("session_mode", "code_run"),
        "source_type": record.get("source_type", ""),
        "network_mode": record.get("network_mode", "wireguard"),
        "status": record.get("status", ""),
        "service_name": record.get("service_name", ""),
        "relay_endpoint": record.get("relay_endpoint", ""),
        "session_token": record.get("session_token", ""),
        "logs": record.get("logs", ""),
        "connect_code": record.get("connect_code", ""),
        "created_at": record.get("created_at", ""),
        "expires_at": record.get("expires_at"),
        "ended_at": record.get("ended_at"),
        **_wireguard_fields(record),
        **_gateway_fields(record),
        **_wireguard_helper_status(record.get("state_dir")),
    }


def _record_from_created_session(
    *,
    local_id: str,
    payload: BaseModel,
    session: dict[str, Any],
    code_filename: str,
    session_mode: str,
) -> dict[str, Any]:
    create_data = session.get("create_result", {}).get("data", {}) or session.get("start_result", {}).get("data", {})
    source_type = (
        str(create_data.get("source_type") or session.get("source_type") or getattr(payload, "source_type", "") or "")
        or ("licensed_order" if session.get("order_id") else "inline_code")
    )
    redeem_data = session.get("redeem_result", {}).get("data", {})
    return {
        "local_id": local_id,
        "state_dir": str(_state_dir(getattr(payload, "state_dir", None))),
        "backend_url": getattr(payload, "backend_url", session.get("backend_url", "")),
        "buyer_email": getattr(payload, "email", session.get("buyer_email", "")),
        "buyer_token": session["buyer_token"],
        "session_id": session["session_id"],
        "order_id": create_data.get("order_id") or session.get("order_id"),
        "license_token": str(session.get("license_token") or ""),
        "seller_node_key": getattr(payload, "seller_node_key", session.get("seller_node_key", "")),
        "runtime_image": getattr(payload, "runtime_image", session.get("runtime_image", "")),
        "code_filename": code_filename,
        "session_mode": session_mode,
        "source_type": source_type,
        "network_mode": redeem_data.get("network_mode", "wireguard"),
        "status": redeem_data.get("status", "created"),
        "service_name": "",
        "session_token": session.get("session_token", ""),
        "logs": "",
        "relay_endpoint": session["relay_endpoint"],
        "connect_code": session["connect_code"],
        "created_at": _utc_now_iso(),
        "expires_at": create_data.get("expires_at"),
        "ended_at": None,
        "remote_logs": "",
        "local_exec_history": "",
        "wireguard_status": "",
        "wireguard_interface": "wg-buyer",
        "wireguard_client_address": "",
        "seller_wireguard_target": str(redeem_data.get("seller_wireguard_target") or ""),
        "wireguard_last_bootstrap_at": "",
        "wireguard_activation_mode": "",
        "gateway_required": bool(session.get("gateway_required") or redeem_data.get("gateway_required")),
        "gateway_protocol": session.get("gateway_protocol") or redeem_data.get("gateway_protocol") or "",
        "gateway_port": session.get("gateway_port") or redeem_data.get("gateway_port"),
        "gateway_service_name": "",
        "gateway_status": "",
        "gateway_last_seen_at": None,
        "gateway_host": "",
        "gateway_local_override_host": "",
        "gateway_handshake_mode": "",
        "gateway_supported_features": [str(item) for item in (session.get("supported_features") or redeem_data.get("supported_features") or [])],
        "gateway_connected_at": None,
        "connection_status": "pending",
        "connection_mode": "",
        "connect_source": "",
        "exec_mode": "gateway",
    }


def _deactivate_local_wireguard(record: dict[str, Any]) -> dict[str, Any]:
    interface_name = record.get("wireguard_interface") or "wg-buyer"
    result = disconnect_runtime_session_wireguard(
        state_dir=_session_state_dir(record),
        interface_name=interface_name,
    )
    if result.get("ok"):
        record["wireguard_status"] = "disconnected"
        record["wireguard_activation_mode"] = "disconnected"
        if record.get("connection_status") == "connected" and record.get("network_mode") == "wireguard":
            record["connection_status"] = "handshaken" if record.get("gateway_host") else "pending"
    return result


def _deactivate_other_wireguard_sessions(target_local_id: str, state_dir: str) -> None:
    for local_id, record in SESSION_STORE.items():
        if local_id == target_local_id:
            continue
        if _session_state_dir(record) != state_dir:
            continue
        if record.get("wireguard_status") != "active":
            continue
        _deactivate_local_wireguard(record)


def _refresh_session(local_id: str) -> dict[str, Any]:
    record = SESSION_STORE.get(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")

    payload = read_runtime_session(
        backend_url=record["backend_url"],
        buyer_token=record["buyer_token"],
        session_id=record["session_id"],
    )
    remote_logs = payload.get("logs", "")
    record["status"] = payload.get("status", "")
    record["service_name"] = payload.get("service_name", "")
    record["gateway_service_name"] = payload.get("gateway_service_name", record.get("gateway_service_name", ""))
    record["gateway_protocol"] = payload.get("gateway_protocol", record.get("gateway_protocol", ""))
    record["gateway_port"] = payload.get("gateway_port", record.get("gateway_port"))
    record["gateway_status"] = payload.get("gateway_status", record.get("gateway_status", ""))
    record["gateway_last_seen_at"] = payload.get("gateway_last_seen_at", record.get("gateway_last_seen_at"))
    record["gateway_supported_features"] = [
        str(item) for item in (payload.get("supported_features") or record.get("gateway_supported_features") or [])
    ]
    record["connect_source"] = payload.get("connect_source", record.get("connect_source", ""))
    record["remote_logs"] = remote_logs
    record["logs"] = _compose_session_logs(record, remote_logs)
    record["ended_at"] = payload.get("ended_at")
    record["expires_at"] = payload.get("expires_at")
    record["network_mode"] = payload.get("network_mode", record.get("network_mode", "wireguard"))
    record["wireguard_client_address"] = payload.get("buyer_wireguard_client_address") or record.get(
        "wireguard_client_address", ""
    )
    record["seller_wireguard_target"] = payload.get("seller_wireguard_target") or record.get(
        "seller_wireguard_target", ""
    )
    if record.get("gateway_required"):
        record["exec_mode"] = "gateway"
    if record.get("network_mode") == "wireguard" and record.get("wireguard_status") != "active":
        if record.get("connection_status") == "connected":
            record["connection_status"] = "handshaken"

    if record["status"] in {"stopped", "expired"} and record.get("wireguard_status") == "active":
        _deactivate_local_wireguard(record)
    if record["status"] in TERMINAL_SESSION_STATES:
        record["connection_status"] = "stopped"
        record["gateway_status"] = record.get("gateway_status") or "stopped"

    return _masked_session(record)


def _slice_log_text(text: str, *, cursor: int, limit: int, tail: bool) -> dict[str, Any]:
    lines = (text or "").splitlines()
    if tail:
        start = max(len(lines) - limit, 0)
    else:
        start = min(max(cursor, 0), len(lines))
    end = min(start + limit, len(lines))
    excerpt = lines[start:end]
    return {
        "ok": True,
        "cursor": start,
        "next_cursor": end,
        "total_lines": len(lines),
        "logs": "\n".join(excerpt),
        "lines": excerpt,
    }


def _local_ipv4_addresses() -> set[str]:
    addresses = {"127.0.0.1"}
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(str(item[4][0]))
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["ipconfig"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            for line in result.stdout.splitlines():
                if "IPv4" not in line:
                    continue
                for match in re.findall(r"(?:\d{1,3}\.){3}\d{1,3}", line):
                    addresses.add(match)
        except Exception:
            pass
    return {item for item in addresses if item}


def _gateway_access_host(record: dict[str, Any]) -> str:
    configured = str(record.get("gateway_host") or "").strip()
    override = str(record.get("gateway_local_override_host") or "").strip()
    if override:
        return override
    if configured and configured in _local_ipv4_addresses():
        return "127.0.0.1"
    return configured


def _probe_gateway(record: dict[str, Any], *, retries: int = 10, delay_seconds: float = 1.0) -> dict[str, Any]:
    if not record.get("gateway_host") or not record.get("gateway_port") or not record.get("session_token"):
        raise RuntimeError("session_gateway_not_connected")
    access_host = _gateway_access_host(record)
    last_response: dict[str, Any] | None = None
    for attempt in range(max(1, retries)):
        response = request_gateway_json(
            "GET",
            gateway_host=access_host,
            gateway_port=record["gateway_port"],
            session_token=record["session_token"],
            path="/",
            gateway_protocol=record.get("gateway_protocol") or "http",
            timeout=5.0,
        )
        if response["ok"]:
            record["gateway_local_override_host"] = access_host if access_host != str(record.get("gateway_host") or "") else ""
            record["gateway_status"] = "online"
            return response["data"]
        last_response = response
        if attempt + 1 < retries:
            time.sleep(delay_seconds)
    raise RuntimeError(f"gateway_health_failed: {(last_response or {}).get('data')}")


def _require_gateway_session(record: dict[str, Any], feature: str, *, require_connected: bool = True) -> None:
    if record.get("status") in TERMINAL_SESSION_STATES:
        raise HTTPException(status_code=409, detail="runtime_session_not_active")
    if not record.get("gateway_host") or not record.get("gateway_port") or not record.get("session_token"):
        raise HTTPException(status_code=409, detail="session_gateway_not_connected")
    supported_features = list(record.get("gateway_supported_features") or [])
    if supported_features and feature not in supported_features:
        raise HTTPException(status_code=409, detail=f"gateway_feature_not_available:{feature}")
    if require_connected and record.get("network_mode") == "wireguard" and record.get("wireguard_status") != "active":
        raise HTTPException(status_code=409, detail="buyer_wireguard_not_active")
    if require_connected and record.get("connection_status") != "connected":
        raise HTTPException(status_code=409, detail="session_gateway_not_connected")


def _cache_exec_result(record: dict[str, Any], command: str, exec_result: dict[str, Any]) -> None:
    transcript = f"$ {command}\n{exec_result.get('stdout') or ''}{exec_result.get('stderr') or ''}".strip()
    existing_exec = (record.get("local_exec_history") or "").strip()
    record["local_exec_history"] = f"{existing_exec}\n\n{transcript}".strip() if existing_exec else transcript
    record["logs"] = _compose_session_logs(record, record.get("remote_logs") or "")


def _path_size_bytes(path: Path) -> int:
    if path.is_file():
        return int(path.stat().st_size)
    return sum(int(item.stat().st_size) for item in path.rglob("*") if item.is_file())


def _prepare_upload_archive(local_path: str) -> dict[str, Any]:
    source_path = Path(local_path).expanduser().resolve()
    if not source_path.exists():
        raise RuntimeError("local_path_not_found")
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(source_path, arcname=source_path.name)
    return {
        "local_path": str(source_path),
        "entry_name": source_path.name,
        "is_dir": source_path.is_dir(),
        "size_bytes": _path_size_bytes(source_path),
        "archive_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }


def _remove_local_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.exists():
        shutil.rmtree(path)


def _extract_download_archive(download_result: dict[str, Any], local_path: str) -> dict[str, Any]:
    archive_base64 = str(download_result.get("archive_base64") or "")
    if not archive_base64:
        raise RuntimeError("gateway_download_archive_missing")
    target_path = Path(local_path).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="pivot-buyer-download-") as temp_dir:
        temp_root = Path(temp_dir)
        raw_archive = base64.b64decode(archive_base64)
        with tarfile.open(fileobj=BytesIO(raw_archive), mode="r:*") as archive:
            members = archive.getmembers()
            if not members:
                raise RuntimeError("gateway_download_archive_empty")
            root_path = temp_root.resolve()
            for member in members:
                member_path = (temp_root / member.name).resolve()
                if root_path != member_path and root_path not in member_path.parents:
                    raise RuntimeError("gateway_download_archive_unsafe")
            archive.extractall(temp_root, filter="data")
        extracted_items = [item for item in temp_root.iterdir()]
        if len(extracted_items) != 1:
            raise RuntimeError("gateway_download_archive_invalid_layout")
        extracted_path = extracted_items[0]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            _remove_local_path(target_path)
        shutil.move(str(extracted_path), str(target_path))
    return {
        "local_path": str(target_path),
        "entry_name": target_path.name,
        "is_dir": target_path.is_dir(),
        "size_bytes": _path_size_bytes(target_path),
    }


def _gateway_exec(record: dict[str, Any], command: str) -> dict[str, Any]:
    _require_gateway_session(record, "exec")
    return gateway_exec_command(
        gateway_host=_gateway_access_host(record),
        gateway_port=record["gateway_port"],
        session_token=record["session_token"],
        command=command,
        gateway_protocol=record.get("gateway_protocol") or "http",
    )


def _gateway_logs(record: dict[str, Any], *, cursor: int, limit: int, tail: bool) -> dict[str, Any]:
    _require_gateway_session(record, "logs")
    access_host = _gateway_access_host(record)
    if tail:
        summary = gateway_read_logs(
            gateway_host=access_host,
            gateway_port=record["gateway_port"],
            session_token=record["session_token"],
            cursor=0,
            limit=1,
            gateway_protocol=record.get("gateway_protocol") or "http",
        )
        total_lines = int(summary.get("total_lines") or 0)
        cursor = max(total_lines - limit, 0)
    return gateway_read_logs(
        gateway_host=access_host,
        gateway_port=record["gateway_port"],
        session_token=record["session_token"],
        cursor=cursor,
        limit=limit,
        gateway_protocol=record.get("gateway_protocol") or "http",
    )


def _gateway_upload_files(record: dict[str, Any], *, local_path: str, remote_path: str) -> dict[str, Any]:
    _require_gateway_session(record, "files")
    archive_payload = _prepare_upload_archive(local_path)
    gateway_result = gateway_upload_archive(
        gateway_host=_gateway_access_host(record),
        gateway_port=record["gateway_port"],
        session_token=record["session_token"],
        remote_path=remote_path,
        archive_base64=str(archive_payload["archive_base64"]),
        gateway_protocol=record.get("gateway_protocol") or "http",
    )
    return {
        **gateway_result,
        "local_path": archive_payload["local_path"],
        "entry_name": archive_payload["entry_name"],
        "is_dir": archive_payload["is_dir"],
        "local_size_bytes": archive_payload["size_bytes"],
    }


def _gateway_download_files(record: dict[str, Any], *, remote_path: str, local_path: str) -> dict[str, Any]:
    _require_gateway_session(record, "files")
    gateway_result = gateway_download_archive(
        gateway_host=_gateway_access_host(record),
        gateway_port=record["gateway_port"],
        session_token=record["session_token"],
        remote_path=remote_path,
        gateway_protocol=record.get("gateway_protocol") or "http",
    )
    local_result = _extract_download_archive(gateway_result, local_path)
    return {
        **gateway_result,
        **local_result,
    }


def _register_terminal(local_id: str, websocket: WebSocket) -> None:
    ACTIVE_TERMINALS.setdefault(local_id, []).append(websocket)


def _unregister_terminal(local_id: str, websocket: WebSocket) -> None:
    sockets = ACTIVE_TERMINALS.get(local_id, [])
    if websocket in sockets:
        sockets.remove(websocket)
    if not sockets and local_id in ACTIVE_TERMINALS:
        ACTIVE_TERMINALS.pop(local_id, None)


async def _close_terminal_sockets(local_id: str, reason: str = "session_stopped") -> None:
    sockets = list(ACTIVE_TERMINALS.get(local_id, []))
    for websocket in sockets:
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": reason}))
        except Exception:
            pass
        try:
            await websocket.close(code=1012, reason=reason)
        except Exception:
            pass
        _unregister_terminal(local_id, websocket)


async def _bridge_runtime_terminal(
    websocket: WebSocket,
    record: dict[str, Any],
    *,
    rows: int,
    cols: int,
) -> None:
    remote_url = gateway_shell_websocket_url(
        gateway_host=_gateway_access_host(record),
        gateway_port=record["gateway_port"],
        gateway_protocol=record.get("gateway_protocol") or "http",
        rows=rows,
        cols=cols,
    )
    headers = {"Authorization": f"Bearer {record['session_token']}"}
    async with websockets.connect(
        remote_url,
        additional_headers=headers,
        open_timeout=20,
        max_size=None,
    ) as remote_socket:
        stop_event = asyncio.Event()

        async def local_to_remote() -> None:
            try:
                while not stop_event.is_set():
                    message = await websocket.receive_text()
                    await remote_socket.send(message)
            except WebSocketDisconnect:
                try:
                    await remote_socket.send(json.dumps({"type": "close"}))
                except Exception:
                    pass
            finally:
                stop_event.set()

        async def remote_to_local() -> None:
            try:
                async for message in remote_socket:
                    text = message.decode("utf-8", "replace") if isinstance(message, bytes) else message
                    await websocket.send_text(text)
            finally:
                stop_event.set()

        local_task = asyncio.create_task(local_to_remote())
        remote_task = asyncio.create_task(remote_to_local())
        await stop_event.wait()
        for task in (local_task, remote_task):
            task.cancel()
        await asyncio.gather(local_task, remote_task, return_exceptions=True)


def _launch_wireguard_helper_installer(state_dir: str | None, attempt_launch: bool) -> dict[str, Any]:
    command = _wireguard_helper_apply_command()
    bootstrap_result = bootstrap_client(dry_run=True, state_dir=str(_state_dir(state_dir)))
    result = {
        "ok": False,
        "attempted_launch": False,
        "launch_started": False,
        "windows_apply_command": bootstrap_result.get("windows_apply_command") or command,
    }
    if not attempt_launch:
        return result
    script_path = REPO_ROOT / "environment_check" / "install_windows.ps1"
    ps_command = (
        "Start-Process PowerShell -Verb RunAs -ArgumentList "
        f"'-ExecutionPolicy Bypass -File \"{script_path}\" -Apply'"
    )
    try:
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_command,
            ]
        )
    except Exception as exc:  # noqa: BLE001
        result["attempted_launch"] = True
        result["error"] = str(exc)
        return result
    result["ok"] = True
    result["attempted_launch"] = True
    result["launch_started"] = True
    return result


def _dashboard_payload(state_dir: str | None) -> dict[str, Any]:
    resolved_state_dir = str(_state_dir(state_dir))
    sessions = [
        _masked_session(record)
        for record in reversed(list(SESSION_STORE.values()))
        if _session_state_dir(record) == resolved_state_dir
    ]
    return {
        "ok": True,
        "state_dir": resolved_state_dir,
        "summary": {
            "session_count": len(sessions),
            "running_count": sum(1 for item in sessions if item["status"] == "running"),
            "completed_count": sum(1 for item in sessions if item["status"] == "completed"),
            "wireguard_active_count": sum(1 for item in sessions if item["wireguard_status"] == "active"),
            "connected_count": sum(1 for item in sessions if item.get("connection_status") == "connected"),
        },
        "wireguard_helper": _wireguard_helper_status(resolved_state_dir),
        "codex": codex_status(resolved_state_dir),
        "sessions": sessions,
        "local_activity": _read_activity(state_dir),
    }


app = FastAPI(title="Pivot Buyer Local Web")
app.mount("/static", StaticFiles(directory=INDEX_HTML.parent), name="buyer-static")


@app.get("/", response_class=HTMLResponse)
def read_index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/api/health")
def read_health() -> dict[str, Any]:
    return {"status": "ok", "service": "buyer-agent-web"}


@app.get("/api/dashboard")
def read_dashboard(state_dir: str | None = None) -> dict[str, Any]:
    return _dashboard_payload(state_dir)


@app.post("/api/runtime/run-code")
def run_code(payload: RunCodeRequest) -> JSONResponse:
    session = create_runtime_session(
        backend_url=payload.backend_url,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        seller_node_key=payload.seller_node_key,
        code_filename=payload.code_filename,
        code_content=payload.code_content,
        runtime_image=payload.runtime_image,
        requested_duration_minutes=payload.requested_duration_minutes,
    )
    local_id = uuid.uuid4().hex
    record = _record_from_created_session(
        local_id=local_id,
        payload=payload,
        session=session,
        code_filename=payload.code_filename,
        session_mode="code_run",
    )
    SESSION_STORE[local_id] = record
    activity = _append_activity(
        payload.state_dir,
        {
            "action": "run_code",
            "status": "success",
            "title": "Create buyer runtime session",
            "summary": f"Created session {session['session_id']} on seller node {payload.seller_node_key}.",
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "action": "run_code",
            "status": "success",
            "session": _masked_session(record),
            "activity_entry": activity,
        }
    )


@app.post("/api/runtime/run-archive")
def run_archive_endpoint(payload: RunArchiveRequest) -> JSONResponse:
    final_payload = run_archive(
        backend_url=payload.backend_url,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        seller_node_key=payload.seller_node_key,
        source_path=Path(payload.source_path),
        runtime_image=payload.runtime_image,
        poll_seconds=2,
        working_dir=payload.working_dir,
        run_command=["sh", "-lc", payload.run_command] if payload.run_command else None,
        requested_duration_minutes=payload.requested_duration_minutes,
    )
    activity = _append_activity(
        payload.state_dir,
        {
            "action": "run_archive",
            "status": "success",
            "title": "Create archive runtime session",
            "summary": f"Ran archive source on seller node {payload.seller_node_key}.",
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "action": "run_archive",
            "status": "success",
            "result": final_payload,
            "activity_entry": activity,
        }
    )


@app.post("/api/runtime/run-github")
def run_github_endpoint(payload: RunGitHubRequest) -> JSONResponse:
    final_payload = run_github_repo(
        backend_url=payload.backend_url,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        seller_node_key=payload.seller_node_key,
        repo_url=payload.repo_url,
        repo_ref=payload.repo_ref,
        runtime_image=payload.runtime_image,
        poll_seconds=2,
        working_dir=payload.working_dir,
        run_command=["sh", "-lc", payload.run_command] if payload.run_command else None,
        requested_duration_minutes=payload.requested_duration_minutes,
    )
    activity = _append_activity(
        payload.state_dir,
        {
            "action": "run_github",
            "status": "success",
            "title": "Create GitHub runtime session",
            "summary": f"Ran GitHub source {payload.repo_url}@{payload.repo_ref}.",
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "action": "run_github",
            "status": "success",
            "result": final_payload,
            "activity_entry": activity,
        }
    )


@app.post("/api/runtime/start-shell")
def start_shell(payload: StartShellRequest) -> JSONResponse:
    session = start_shell_session(
        backend_url=payload.backend_url,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        seller_node_key=payload.seller_node_key,
        runtime_image=payload.runtime_image,
        requested_duration_minutes=payload.requested_duration_minutes,
    )
    local_id = uuid.uuid4().hex
    record = _record_from_created_session(
        local_id=local_id,
        payload=payload,
        session=session,
        code_filename="__shell__",
        session_mode="shell",
    )
    SESSION_STORE[local_id] = record
    activity = _append_activity(
        payload.state_dir,
        {
            "action": "start_shell",
            "status": "success",
            "title": "Create buyer shell session",
            "summary": f"Created shell session {session['session_id']} on seller node {payload.seller_node_key}.",
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "action": "start_shell",
            "status": "success",
            "session": _masked_session(record),
            "activity_entry": activity,
        }
    )


@app.post("/api/runtime/start-licensed-shell")
def start_licensed_shell(payload: StartLicensedShellRequest) -> JSONResponse:
    session = start_licensed_shell_session(
        backend_url=payload.backend_url,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        license_token=payload.license_token,
    )
    local_id = uuid.uuid4().hex
    record = _record_from_created_session(
        local_id=local_id,
        payload=payload,
        session=session,
        code_filename="__shell__",
        session_mode="shell",
    )
    SESSION_STORE[local_id] = record
    activity = _append_activity(
        payload.state_dir,
        {
            "action": "start_licensed_shell",
            "status": "success",
            "title": "Create buyer licensed shell session",
            "summary": (
                f"Created licensed shell session {session['session_id']} for order "
                f"{session.get('order_id') or 'unknown'} on seller node {session['seller_node_key']}."
            ),
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "action": "start_licensed_shell",
            "status": "success",
            "session": _masked_session(record),
            "activity_entry": activity,
        }
    )


@app.get("/api/runtime/sessions/{local_id}")
def read_runtime_session_status(local_id: str) -> JSONResponse:
    payload = _refresh_session(local_id)
    return JSONResponse({"ok": True, "session": payload})


@app.post("/api/runtime/sessions/{local_id}/connect")
def connect_runtime_session_endpoint(local_id: str, payload: ConnectRequest) -> JSONResponse:
    record = SESSION_STORE.get(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")

    state_dir = str(_state_dir(payload.state_dir or record.get("state_dir")))
    record["state_dir"] = state_dir
    refreshed = _refresh_session(local_id)
    handshake_result: dict[str, Any] | None = None
    gateway_probe: dict[str, Any] | None = None
    wireguard_result: dict[str, Any] | None = None
    errors: list[str] = []

    try:
        handshake_result = handshake_runtime_gateway(
            backend_url=record["backend_url"],
            session_id=record["session_id"],
            session_token=record.get("session_token", ""),
        )
        _apply_gateway_handshake(record, handshake_result)
    except Exception as exc:
        record["connection_status"] = "error"
        errors.append(str(exc))

    if payload.activate_wireguard and record.get("network_mode") == "wireguard" and not errors:
        try:
            _deactivate_other_wireguard_sessions(local_id, state_dir)
            wireguard_result = bootstrap_runtime_session_wireguard(
                backend_url=record["backend_url"],
                buyer_token=record["buyer_token"],
                session_id=record["session_id"],
                state_dir=state_dir,
            )
            bundle = wireguard_result.get("bundle", {})
            activation_result = wireguard_result.get("activation_result", {})
            record["network_mode"] = "wireguard"
            record["wireguard_interface"] = bundle.get("interface_name", "wg-buyer")
            record["wireguard_client_address"] = bundle.get("client_address", "")
            record["seller_wireguard_target"] = bundle.get("seller_wireguard_target", "") or record.get(
                "seller_wireguard_target", ""
            )
            record["wireguard_last_bootstrap_at"] = _utc_now_iso()
            record["wireguard_activation_mode"] = str(
                activation_result.get("mode") or ("failed" if not activation_result.get("ok") else "direct")
            )
            record["wireguard_status"] = "active" if activation_result.get("ok") else "activation_failed"
            if record.get("gateway_host"):
                record["connection_status"] = "handshaken"
        except Exception as exc:
            if record.get("gateway_host"):
                record["connection_status"] = "handshaken"
            else:
                record["connection_status"] = "error"
            errors.append(str(exc))

    if (
        record.get("gateway_host")
        and not errors
        and (record.get("network_mode") != "wireguard" or record.get("wireguard_status") == "active")
    ):
        try:
            gateway_probe = _probe_gateway(record)
            record["connection_status"] = "connected"
        except Exception as exc:
            record["connection_status"] = "error"
            errors.append(str(exc))

    activity = _append_activity(
        state_dir,
        {
            "action": "connect_session",
            "status": "success" if not errors else "error",
            "title": "Connect runtime session",
            "summary": (
                (
                    f"Session {record['session_id']} connected to gateway."
                    if not errors and record.get("connection_status") == "connected"
                    else (
                        f"Session {record['session_id']} gateway handshake complete; local WireGuard is "
                        f"{record.get('wireguard_status') or 'pending'}."
                        if not errors
                        else f"Session {record['session_id']} connection attempt failed."
                    )
                )
            ),
        },
    )
    return JSONResponse(
        {
            "ok": not errors,
            "session": _masked_session(record),
            "refreshed_session": refreshed,
            "handshake_result": handshake_result,
            "gateway_probe": gateway_probe,
            "wireguard_result": wireguard_result,
            "errors": errors,
            "activity_entry": activity,
        }
    )


@app.post("/api/runtime/sessions/{local_id}/wireguard/bootstrap")
def bootstrap_runtime_session_wireguard_endpoint(local_id: str, payload: WireGuardRequest) -> JSONResponse:
    record = SESSION_STORE.get(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")

    state_dir = str(_state_dir(payload.state_dir or record.get("state_dir")))
    record["state_dir"] = state_dir
    _deactivate_other_wireguard_sessions(local_id, state_dir)

    result = bootstrap_runtime_session_wireguard(
        backend_url=record["backend_url"],
        buyer_token=record["buyer_token"],
        session_id=record["session_id"],
        state_dir=state_dir,
    )
    bundle = result.get("bundle", {})
    activation_result = result.get("activation_result", {})
    record["network_mode"] = "wireguard"
    record["wireguard_interface"] = bundle.get("interface_name", "wg-buyer")
    record["wireguard_client_address"] = bundle.get("client_address", "")
    record["seller_wireguard_target"] = bundle.get("seller_wireguard_target", "") or ""
    record["wireguard_last_bootstrap_at"] = _utc_now_iso()
    record["wireguard_activation_mode"] = str(
        activation_result.get("mode") or ("failed" if not activation_result.get("ok") else "direct")
    )
    record["wireguard_status"] = "active" if activation_result.get("ok") else "activation_failed"
    if activation_result.get("ok") and record.get("gateway_host"):
        record["connection_status"] = "connected"
    elif record.get("gateway_host"):
        record["connection_status"] = "handshaken"
    wg_state = wireguard_summary(interface_name=record["wireguard_interface"], state_dir=state_dir)
    activity = _append_activity(
        state_dir,
        {
            "action": "wireguard_bootstrap",
            "status": "success" if activation_result.get("ok") else "error",
            "title": "Bootstrap buyer WireGuard lease",
            "summary": (
                f"Session {record['session_id']} lease credentials issued. "
                f"seller={record['seller_wireguard_target'] or 'unknown'} "
                f"buyer={record['wireguard_client_address'] or 'unknown'}"
            ),
        },
    )
    return JSONResponse(
        {
            "ok": bool(activation_result.get("ok")),
            "session": _masked_session(record),
            "wireguard_result": result,
            "wireguard_state": wg_state,
            "activity_entry": activity,
        }
    )


@app.post("/api/runtime/sessions/{local_id}/wireguard/disconnect")
def disconnect_runtime_session_wireguard_endpoint(local_id: str, payload: WireGuardRequest) -> JSONResponse:
    record = SESSION_STORE.get(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")

    state_dir = str(_state_dir(payload.state_dir or record.get("state_dir")))
    record["state_dir"] = state_dir
    result = disconnect_runtime_session_wireguard(
        state_dir=state_dir,
        interface_name=record.get("wireguard_interface") or "wg-buyer",
    )
    if result.get("ok"):
        record["wireguard_status"] = "disconnected"
        record["wireguard_activation_mode"] = "disconnected"
        if record.get("gateway_host"):
            record["connection_status"] = "handshaken"
    wg_state = wireguard_summary(interface_name=record.get("wireguard_interface") or "wg-buyer", state_dir=state_dir)
    activity = _append_activity(
        state_dir,
        {
            "action": "wireguard_disconnect",
            "status": "success" if result.get("ok") else "error",
            "title": "Disconnect buyer WireGuard lease",
            "summary": f"Local interface {(record.get('wireguard_interface') or 'wg-buyer')} disconnected.",
        },
    )
    return JSONResponse(
        {
            "ok": bool(result.get("ok")),
            "session": _masked_session(record),
            "disconnect_result": result,
            "wireguard_state": wg_state,
            "activity_entry": activity,
        }
    )


@app.post("/api/runtime/wireguard-helper/install")
def install_wireguard_helper_endpoint(payload: WireGuardHelperInstallRequest) -> JSONResponse:
    state_dir = str(_state_dir(payload.state_dir))
    before = _wireguard_helper_status(state_dir)
    launch_result = _launch_wireguard_helper_installer(state_dir, payload.attempt_launch)
    after = _wireguard_helper_status(state_dir)
    activity = _append_activity(
        state_dir,
        {
            "action": "wireguard_helper_install",
            "status": "success" if launch_result.get("ok") or before.get("wireguard_helper_ready") else "warning",
            "title": "Prepare WireGuard elevated helper",
            "summary": (
                "WireGuard helper already ready."
                if before.get("wireguard_helper_ready")
                else "Opened the administrator install flow for the WireGuard helper."
                if launch_result.get("launch_started")
                else "Returned the WireGuard helper install command."
            ),
        },
    )
    return JSONResponse(
        {
            "ok": before.get("wireguard_helper_ready") or launch_result.get("ok"),
            "wireguard_helper": after,
            "before": before,
            "install_result": launch_result,
            "activity_entry": activity,
        }
    )


@app.post("/api/codex/jobs")
def start_codex_job_endpoint(payload: CodexJobRequest) -> JSONResponse:
    record = SESSION_STORE.get(payload.local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")

    state_dir = str(_state_dir(payload.state_dir or record.get("state_dir")))
    record["state_dir"] = state_dir
    session = _refresh_session(payload.local_id)
    buyer_server_url = os.environ.get("PIVOT_BUYER_SERVER_URL") or "http://127.0.0.1:3857"
    try:
        job = create_codex_job(
            local_id=payload.local_id,
            user_prompt=payload.prompt,
            workspace_path=payload.workspace_path,
            state_dir=state_dir,
            backend_url=record["backend_url"],
            buyer_token=record["buyer_token"],
            buyer_server_url=buyer_server_url,
            session_context=session,
            model=payload.model,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    activity = _append_activity(
        state_dir,
        {
            "action": "codex_job_start",
            "status": "success",
            "title": "Start buyer CodeX orchestration job",
            "summary": f"Started CodeX job {job['job_id']} for session {record['session_id']}.",
        },
    )
    return JSONResponse({"ok": True, "job": job, "activity_entry": activity})


@app.get("/api/codex/jobs/{job_id}")
def read_codex_job_endpoint(job_id: str, state_dir: str | None = None) -> JSONResponse:
    resolved_state_dir = str(_state_dir(state_dir))
    try:
        job = get_codex_job(job_id, resolved_state_dir)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Codex job not found.") from exc
    return JSONResponse({"ok": True, "job": job})


@app.post("/api/codex/jobs/{job_id}/cancel")
def cancel_codex_job_endpoint(job_id: str, payload: CodexJobCancelRequest) -> JSONResponse:
    resolved_state_dir = str(_state_dir(payload.state_dir))
    try:
        job = cancel_codex_job(job_id, resolved_state_dir)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Codex job not found.") from exc
    activity = _append_activity(
        payload.state_dir,
        {
            "action": "codex_job_cancel",
            "status": "warning",
            "title": "Cancel buyer CodeX orchestration job",
            "summary": f"Cancellation requested for CodeX job {job_id}.",
        },
    )
    return JSONResponse({"ok": True, "job": job, "activity_entry": activity})


@app.post("/api/runtime/sessions/{local_id}/renew")
def renew_runtime_session_endpoint(local_id: str, payload: RenewSessionRequest) -> JSONResponse:
    record = SESSION_STORE.get(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")
    result = renew_backend_runtime_session(
        backend_url=record["backend_url"],
        buyer_token=record["buyer_token"],
        session_id=record["session_id"],
        additional_minutes=payload.additional_minutes,
    )
    record["expires_at"] = result.get("expires_at")
    activity = _append_activity(
        payload.state_dir or record.get("state_dir"),
        {
            "action": "renew_session",
            "status": "success",
            "title": "Renew buyer runtime lease",
            "summary": f"Session {record['session_id']} extended by {payload.additional_minutes} minutes.",
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "session": _masked_session(record),
            "renew_result": result,
            "activity_entry": activity,
        }
    )


@app.post("/api/runtime/sessions/{local_id}/exec")
def exec_runtime_session(local_id: str, payload: ExecRequest) -> JSONResponse:
    record = SESSION_STORE.get(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")
    _refresh_session(local_id)
    try:
        exec_result = _gateway_exec(record, payload.command)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _cache_exec_result(record, payload.command, exec_result)
    activity = _append_activity(
        payload.state_dir or record.get("state_dir"),
        {
            "action": "exec",
            "status": "success" if exec_result.get("ok") else "error",
            "title": "Gateway exec inside runtime session",
            "summary": f"Session {record['session_id']} gateway exec: {payload.command}",
        },
    )
    return JSONResponse(
        {
            "ok": bool(exec_result.get("ok")),
            "session": _masked_session(record),
            "exec_result": exec_result,
            "activity_entry": activity,
        }
    )


@app.post("/api/runtime/sessions/{local_id}/files/upload")
def upload_runtime_session_files(local_id: str, payload: SessionUploadRequest) -> JSONResponse:
    record = SESSION_STORE.get(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")
    _refresh_session(local_id)
    try:
        upload_result = _gateway_upload_files(
            record,
            local_path=payload.local_path,
            remote_path=payload.remote_path,
        )
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 400 if detail == "local_path_not_found" else 502
        raise HTTPException(status_code=status_code, detail=detail) from exc
    activity = _append_activity(
        payload.state_dir or record.get("state_dir"),
        {
            "action": "files_upload",
            "status": "success",
            "title": "Upload local content to runtime session",
            "summary": (
                f"Uploaded {upload_result.get('entry_name') or payload.local_path} to "
                f"{upload_result.get('uploaded_path') or payload.remote_path}."
            ),
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "session": _masked_session(record),
            "upload_result": upload_result,
            "activity_entry": activity,
        }
    )


@app.post("/api/runtime/sessions/{local_id}/files/download")
def download_runtime_session_files(local_id: str, payload: SessionDownloadRequest) -> JSONResponse:
    record = SESSION_STORE.get(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")
    _refresh_session(local_id)
    try:
        download_result = _gateway_download_files(
            record,
            remote_path=payload.remote_path,
            local_path=payload.local_path,
        )
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 400 if detail.startswith("gateway_download_archive_") else 502
        raise HTTPException(status_code=status_code, detail=detail) from exc
    activity = _append_activity(
        payload.state_dir or record.get("state_dir"),
        {
            "action": "files_download",
            "status": "success",
            "title": "Download runtime content to local machine",
            "summary": (
                f"Downloaded {payload.remote_path} to {download_result.get('local_path') or payload.local_path}."
            ),
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "session": _masked_session(record),
            "download_result": download_result,
            "activity_entry": activity,
        }
    )


@app.get("/api/runtime/sessions/{local_id}/logs")
def read_runtime_session_logs(
    local_id: str,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    tail: bool = Query(default=False),
) -> JSONResponse:
    record = SESSION_STORE.get(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")

    _refresh_session(local_id)
    if record.get("status") in TERMINAL_SESSION_STATES or not record.get("gateway_host"):
        log_result = _slice_log_text(record.get("logs", ""), cursor=cursor, limit=limit, tail=tail)
    else:
        try:
            log_result = _gateway_logs(record, cursor=cursor, limit=limit, tail=tail)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if cursor == 0 and not tail:
            record["remote_logs"] = log_result.get("logs", "")
            record["logs"] = _compose_session_logs(record, record.get("remote_logs"))
    return JSONResponse({"ok": True, "session": _masked_session(record), "log_result": log_result})


@app.websocket("/api/runtime/sessions/{local_id}/terminal")
async def terminal_runtime_session(local_id: str, websocket: WebSocket) -> None:
    record = SESSION_STORE.get(local_id)
    if record is None:
        await websocket.close(code=4404, reason="local_session_not_found")
        return

    try:
        _refresh_session(local_id)
        _require_gateway_session(record, "shell")
    except HTTPException as exc:
        await websocket.accept()
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc.detail)}))
        await websocket.close(code=4409, reason=str(exc.detail))
        return

    rows = max(1, int(websocket.query_params.get("rows") or 24))
    cols = max(1, int(websocket.query_params.get("cols") or 80))
    await websocket.accept()
    _register_terminal(local_id, websocket)
    try:
        await _bridge_runtime_terminal(websocket, record, rows=rows, cols=cols)
    except Exception as exc:
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        except Exception:
            pass
    finally:
        _unregister_terminal(local_id, websocket)
        try:
            await websocket.close()
        except Exception:
            pass


@app.post("/api/runtime/sessions/{local_id}/stop")
async def stop_runtime_session_endpoint(local_id: str, payload: StopSessionRequest) -> JSONResponse:
    record = SESSION_STORE.get(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Local buyer session not found.")

    await _close_terminal_sockets(local_id)

    if record.get("wireguard_status") == "active":
        _deactivate_local_wireguard(record)

    stop_runtime_session(
        backend_url=record["backend_url"],
        buyer_token=record["buyer_token"],
        session_id=record["session_id"],
    )
    record["status"] = "stopped"
    record["connection_status"] = "stopped"
    record["gateway_status"] = "stopped"
    record["ended_at"] = _utc_now_iso()
    activity = _append_activity(
        payload.state_dir or record.get("state_dir"),
        {
            "action": "stop_session",
            "status": "success",
            "title": "Stop buyer runtime session",
            "summary": f"Stopped session {record['session_id']}.",
        },
    )
    return JSONResponse({"ok": True, "session": _masked_session(record), "activity_entry": activity})


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=3857)


if __name__ == "__main__":
    main()
