import json
from collections import namedtuple
from pathlib import Path

from seller_client.agent_mcp import (
    _build_remote_image_ref,
    _config_path,
    _normalize_registry_reference,
    _run_windows_wireguard_helper,
    _registry_base_url,
    _wireguard_config_path,
    bootstrap_wireguard_from_platform,
    connect_server_vpn,
    configure_environment,
    configure_registry_trust,
    ensure_joined_to_platform_swarm,
    explain_seller_intent,
    environment_check,
    fetch_codex_runtime_bootstrap,
    fetch_swarm_worker_join_token,
    get_client_config,
    host_summary,
    ping,
    prepare_wireguard_profile,
    push_image,
    push_image_to_server,
    report_image_to_platform,
)


def test_ping_returns_ok_payload() -> None:
    response = ping()

    assert response["status"] == "ok"
    assert response["agent"] == "seller-node-agent"


def test_environment_check_includes_expected_keys() -> None:
    response = environment_check()

    assert "docker_cli" in response
    assert "python" in response
    assert "current_workdir" in response


def test_host_summary_falls_back_to_shutil_disk_usage(monkeypatch) -> None:
    virtual_memory_type = namedtuple("VirtualMemory", ["total", "available"])
    disk_usage_type = namedtuple("DiskUsage", ["total", "used", "free"])

    monkeypatch.setattr("seller_client.agent_mcp.psutil.boot_time", lambda: 123.0)
    monkeypatch.setattr("seller_client.agent_mcp.psutil.net_if_addrs", lambda: {})
    monkeypatch.setattr(
        "seller_client.agent_mcp.psutil.virtual_memory",
        lambda: virtual_memory_type(total=16 * 1024 * 1024 * 1024, available=6 * 1024 * 1024 * 1024),
    )
    monkeypatch.setattr("seller_client.agent_mcp.psutil.cpu_count", lambda logical=True: 8 if logical else 4)
    monkeypatch.setattr(
        "seller_client.agent_mcp.psutil.disk_usage",
        lambda path: (_ for _ in ()).throw(SystemError("argument 1 (impossible<bad format char>)")),
    )
    monkeypatch.setattr(
        "seller_client.agent_mcp.shutil.disk_usage",
        lambda path: disk_usage_type(
            total=500 * 1024 * 1024 * 1024,
            used=200 * 1024 * 1024 * 1024,
            free=300 * 1024 * 1024 * 1024,
        ),
    )

    response = host_summary()

    assert response["disk_total_gb"] == 500.0
    assert response["disk_free_gb"] == 300.0


def test_windows_wireguard_helper_tolerates_result_cleanup_permission_error(tmp_path: Path, monkeypatch) -> None:
    request_path = tmp_path / "request.json"
    result_file = tmp_path / "result.json"

    class StubResultPath:
        def __init__(self, path: Path) -> None:
            self._path = path
            self.parent = path.parent

        def exists(self) -> bool:
            return self._path.exists()

        def unlink(self) -> None:
            raise PermissionError("[WinError 5] Access is denied")

        def read_text(self, encoding: str = "utf-8") -> str:
            return self._path.read_text(encoding=encoding)

        def __str__(self) -> str:
            return str(self._path)

    class StubUuid:
        hex = "req-123"

    result_file.write_text(
        json.dumps({"ok": True, "request_id": "req-123", "action": "install_tunnel_service"}, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr("seller_client.agent_mcp.is_windows_platform", lambda: True)
    monkeypatch.setattr("seller_client.agent_mcp._windows_wireguard_helper_installed", lambda: True)
    monkeypatch.setattr("seller_client.agent_mcp.wireguard_helper_request_path", lambda: request_path)
    monkeypatch.setattr("seller_client.agent_mcp.wireguard_helper_result_path", lambda: StubResultPath(result_file))
    monkeypatch.setattr("seller_client.agent_mcp.wireguard_helper_run_task_command", lambda: ["schtasks", "/Run"])
    monkeypatch.setattr("seller_client.agent_mcp._run_command", lambda command: {"ok": True, "command": command})
    monkeypatch.setattr("seller_client.agent_mcp.uuid.uuid4", lambda: StubUuid())

    response = _run_windows_wireguard_helper(
        action="install_tunnel_service",
        config_path="C:\\temp\\wg.conf",
        interface_name="wg-seller",
        wireguard_exe="C:\\Program Files\\WireGuard\\wireguard.exe",
        timeout_seconds=1,
    )

    assert response["ok"] is True
    assert response["helper_result"]["request_id"] == "req-123"
    assert response["cleanup_warning"]["warning"] == "wireguard_helper_result_cleanup_failed"


def test_configure_environment_writes_client_config(tmp_path: Path) -> None:
    response = configure_environment(
        manager_host="example.com",
        registry="registry.example.com:5000",
        portainer_url="https://example.com:9443",
        wireguard_interface="wg-test",
        wireguard_endpoint_host="vpn.example.com",
        wireguard_endpoint_port=45184,
        state_dir=str(tmp_path),
    )

    assert response["ok"] is True
    assert _config_path(tmp_path).exists()

    config = get_client_config(state_dir=str(tmp_path))
    assert config["data"]["server"]["manager_host"] == "example.com"
    assert config["data"]["server"]["registry"] == "registry.example.com:5000"
    assert config["data"]["wireguard"]["interface"] == "wg-test"


def test_configure_environment_upgrades_legacy_registry_to_public_domain(tmp_path: Path) -> None:
    response = configure_environment(
        registry="81.70.52.75:5000",
        state_dir=str(tmp_path),
    )

    assert response["ok"] is True
    config = get_client_config(state_dir=str(tmp_path))
    assert config["data"]["server"]["registry"] == "pivotcompute.store"


def test_prepare_wireguard_profile_writes_expected_config(tmp_path: Path) -> None:
    configure_environment(state_dir=str(tmp_path), wireguard_interface="wg-seller")

    response = prepare_wireguard_profile(
        server_public_key="server-public",
        client_private_key="client-private",
        client_address="10.88.0.2/32",
        endpoint_host="vpn.example.com",
        endpoint_port=45184,
        allowed_ips="10.88.0.0/24",
        interface_name="wg-seller",
        state_dir=str(tmp_path),
    )

    config_path = _wireguard_config_path("wg-seller", tmp_path)
    assert response["ok"] is True
    assert config_path.exists()
    content = config_path.read_text(encoding="utf-8")
    assert "PrivateKey = client-private" in content
    assert "Endpoint = vpn.example.com:45184" in content
    assert "AllowedIPs = 10.88.0.0/24" in content


def test_build_remote_image_ref_uses_registry_and_repository() -> None:
    remote_ref = _build_remote_image_ref(
        repository="seller/demo",
        remote_tag="v1",
        registry="pivotcompute.store",
    )

    assert remote_ref == "pivotcompute.store/seller/demo:v1"


def test_registry_base_url_defaults_to_https() -> None:
    assert _registry_base_url("pivotcompute.store") == "https://pivotcompute.store"


def test_normalize_registry_reference_upgrades_legacy_ip_registry() -> None:
    assert _normalize_registry_reference("81.70.52.75:5000") == "pivotcompute.store"


def test_explain_seller_intent_extracts_share_percent() -> None:
    response = explain_seller_intent("我能把自己电脑性能的10%上传到平台获取收益吗")

    assert response["ok"] is True
    assert response["share_percent_preference"] == 10
    assert "10%" in response["explanation"]


def test_fetch_codex_runtime_bootstrap_masks_secret_and_updates_config(tmp_path: Path, monkeypatch) -> None:
    configure_environment(state_dir=str(tmp_path), backend_url="http://127.0.0.1:8000")
    config = get_client_config(mask_secrets=False, state_dir=str(tmp_path))["data"]
    config["auth"]["access_token"] = "access-token"
    _config_path(tmp_path).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        "seller_client.agent_mcp._run_backend_request",
        lambda *args, **kwargs: {
            "ok": True,
            "body": json.dumps(
                {
                    "model_provider": "OpenAI",
                    "model": "gpt-5.4",
                    "review_model": "gpt-5.4",
                    "model_reasoning_effort": "xhigh",
                    "disable_response_storage": True,
                    "network_access": "enabled",
                    "windows_wsl_setup_acknowledged": True,
                    "model_context_window": 1000000,
                    "model_auto_compact_token_limit": 900000,
                    "provider": {
                        "name": "OpenAI",
                        "base_url": "https://xlabapi.top/v1",
                        "wire_api": "responses",
                        "requires_openai_auth": True,
                    },
                    "auth": {"OPENAI_API_KEY": "sk-secret-12345678"},
                    "auth_source": "env:OPENAI_API_KEY",
                }
            ),
        },
    )

    response = fetch_codex_runtime_bootstrap(state_dir=str(tmp_path))

    assert response["ok"] is True
    assert response["data"]["auth"]["OPENAI_API_KEY"].startswith("sk-s")
    updated = get_client_config(mask_secrets=False, state_dir=str(tmp_path))["data"]
    assert updated["runtime"]["codex_runtime_ready"] is True
    assert updated["runtime"]["codex_model"] == "gpt-5.4"


def test_bootstrap_wireguard_from_platform_prepares_profile(tmp_path: Path, monkeypatch) -> None:
    configure_environment(state_dir=str(tmp_path), backend_url="http://127.0.0.1:8000", wireguard_interface="wg-seller")
    config = get_client_config(mask_secrets=False, state_dir=str(tmp_path))["data"]
    config["auth"]["node_registration_token"] = "node-token"
    config["auth"]["device_fingerprint"] = "device-001"
    _config_path(tmp_path).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        "seller_client.agent_mcp.generate_wireguard_keypair",
        lambda: {"ok": True, "private_key": "client-private", "public_key": "client-public", "wg_bin": "wg"},
    )
    monkeypatch.setattr(
        "seller_client.agent_mcp.request_wireguard_bootstrap",
        lambda client_public_key, backend_url=None, state_dir=None: {
            "ok": True,
            "data": {
                "server_public_key": "server-public",
                "client_address": "10.88.0.20/32",
                "server_endpoint_host": "pivotcompute.store",
                "server_endpoint_port": 51820,
                "allowed_ips": "10.88.0.0/16",
                "interface_name": "wg-seller",
                "dns": "",
                "persistent_keepalive": 25,
                "activation_mode": "server_peer_applied",
                "server_peer_apply_required": False,
            },
        },
    )
    monkeypatch.setattr(
        "seller_client.agent_mcp.connect_server_vpn",
        lambda interface_name="wg-seller", state_dir=None: {"ok": True, "action": "connected"},
    )

    response = bootstrap_wireguard_from_platform(state_dir=str(tmp_path))

    assert response["ok"] is True
    assert response["profile_result"]["ok"] is True
    assert response["activation_result"]["ok"] is True
    content = _wireguard_config_path("wg-seller", tmp_path).read_text(encoding="utf-8")
    assert "PublicKey = server-public" in content
    updated = get_client_config(mask_secrets=False, state_dir=str(tmp_path))["data"]
    assert updated["runtime"]["wireguard_profile_status"] == "active"
    assert updated["wireguard"]["client_public_key"] == "client-public"


def test_fetch_swarm_worker_join_token_updates_config(tmp_path: Path, monkeypatch) -> None:
    configure_environment(state_dir=str(tmp_path), backend_url="http://127.0.0.1:8000")
    config = get_client_config(mask_secrets=False, state_dir=str(tmp_path))["data"]
    config["auth"]["access_token"] = "access-token"
    _config_path(tmp_path).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        "seller_client.agent_mcp._run_backend_request",
        lambda *args, **kwargs: {
            "ok": True,
            "body": json.dumps(
                {
                    "join_token": "SWMTKN-test",
                    "manager_host": "pivotcompute.store",
                    "manager_port": 2377,
                }
            ),
        },
    )

    response = fetch_swarm_worker_join_token(state_dir=str(tmp_path))

    assert response["ok"] is True
    updated = get_client_config(mask_secrets=False, state_dir=str(tmp_path))["data"]
    assert updated["server"]["manager_host"] == "pivotcompute.store"
    assert updated["server"]["manager_port"] == 2377


def test_ensure_joined_to_platform_swarm_noops_when_already_active(monkeypatch) -> None:
    monkeypatch.setattr(
        "seller_client.agent_mcp.swarm_summary",
        lambda: {"info": {"ok": True, "stdout": "state=active node_id=node-1 control=false"}},
    )

    response = ensure_joined_to_platform_swarm()

    assert response["ok"] is True
    assert response["action"] == "already_joined"


def test_connect_server_vpn_uses_elevated_helper_on_access_denied(tmp_path: Path, monkeypatch) -> None:
    configure_environment(state_dir=str(tmp_path), wireguard_interface="wg-seller")
    prepare_wireguard_profile(
        server_public_key="server-public",
        client_private_key="client-private",
        client_address="10.66.66.10/32",
        endpoint_host="pivotcompute.store",
        endpoint_port=45182,
        allowed_ips="10.66.66.0/24",
        interface_name="wg-seller",
        state_dir=str(tmp_path),
    )

    monkeypatch.setattr("seller_client.agent_mcp.platform.system", lambda: "Windows")
    monkeypatch.setattr("seller_client.agent_mcp.windows_is_elevated", lambda: False)
    monkeypatch.setattr("seller_client.agent_mcp._windows_wireguard_helper_installed", lambda: True)
    monkeypatch.setattr("seller_client.agent_mcp._wireguard_windows_exe", lambda: "C:\\Program Files\\WireGuard\\wireguard.exe")
    monkeypatch.setattr(
        "seller_client.agent_mcp._run_windows_wireguard_helper",
        lambda **kwargs: {"ok": True, "helper_result": {"ok": True, "request_id": "req-1"}},
    )

    response = connect_server_vpn(state_dir=str(tmp_path))

    assert response["ok"] is True
    assert response["mode"] == "elevated_helper"


def test_push_image_retries_transient_registry_errors(monkeypatch) -> None:
    attempts = iter(
        [
            {
                "command": ["docker", "push", "registry.example.com/demo:v1"],
                "cwd": None,
                "returncode": 1,
                "stdout": "",
                "stderr": "Patch \"https://registry.example.com/v2/demo/blobs/uploads/123\": EOF",
                "ok": False,
            },
            {
                "command": ["docker", "push", "registry.example.com/demo:v1"],
                "cwd": None,
                "returncode": 0,
                "stdout": "digest: sha256:test size: 1234",
                "stderr": "",
                "ok": True,
            },
        ]
    )

    monkeypatch.setattr("seller_client.agent_mcp._docker_available", lambda: True)
    monkeypatch.setattr("seller_client.agent_mcp._run_command", lambda command, cwd=None: next(attempts))
    monkeypatch.setattr("seller_client.agent_mcp.time.sleep", lambda seconds: None)

    response = push_image("registry.example.com/demo:v1")

    assert response["ok"] is True
    assert response["attempt"] == 2
    assert len(response["attempts"]) == 2


def test_push_image_to_server_persists_last_pushed_image_after_retry(tmp_path: Path, monkeypatch) -> None:
    configure_environment(state_dir=str(tmp_path), registry="registry.example.com:5000")

    monkeypatch.setattr("seller_client.agent_mcp._docker_available", lambda: True)
    monkeypatch.setattr(
        "seller_client.agent_mcp._run_command",
        lambda command, cwd=None: {
            "command": command,
            "cwd": cwd,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "ok": True,
        }
        if command[:2] == ["docker", "tag"]
        else None,
    )
    monkeypatch.setattr(
        "seller_client.agent_mcp.push_image",
        lambda tag, retries=2, retry_delay_seconds=2.0: {
            "command": ["docker", "push", tag],
            "cwd": None,
            "returncode": 0,
            "stdout": "digest: sha256:test size: 1234",
            "stderr": "",
            "ok": True,
            "attempt": 2,
            "attempts": [
                {
                    "command": ["docker", "push", tag],
                    "cwd": None,
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "Patch \"https://registry.example.com:5000/v2/demo/blobs/uploads/123\": EOF",
                    "ok": False,
                    "attempt": 1,
                },
                {
                    "command": ["docker", "push", tag],
                    "cwd": None,
                    "returncode": 0,
                    "stdout": "digest: sha256:test size: 1234",
                    "stderr": "",
                    "ok": True,
                    "attempt": 2,
                },
            ],
        },
    )

    response = push_image_to_server(
        local_tag="python:3.12-alpine",
        repository="seller/demo",
        remote_tag="v1",
        registry="registry.example.com:5000",
        state_dir=str(tmp_path),
    )

    assert response["ok"] is True
    updated = get_client_config(mask_secrets=False, state_dir=str(tmp_path))["data"]
    assert updated["docker"]["last_pushed_image"] == "registry.example.com:5000/seller/demo:v1"


def test_report_image_to_platform_uses_extended_timeout(tmp_path: Path, monkeypatch) -> None:
    configure_environment(state_dir=str(tmp_path), backend_url="http://127.0.0.1:8000")
    config = get_client_config(mask_secrets=False, state_dir=str(tmp_path))["data"]
    config["auth"]["node_registration_token"] = "node-token"
    config["auth"]["device_fingerprint"] = "device-001"
    _config_path(tmp_path).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run_backend_request(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["timeout_seconds"] = kwargs["timeout_seconds"]
        return {
            "ok": True,
            "body": json.dumps(
                {
                    "id": 1,
                    "seller_user_id": 1,
                    "node_id": 1,
                    "repository": "seller/demo",
                    "tag": "v1",
                    "digest": "sha256:test",
                    "registry": "registry.example.com:5000",
                    "source_image": "python:3.12-alpine",
                    "status": "uploaded",
                    "push_ready": True,
                    "created_at": "2026-03-27T00:00:00Z",
                    "updated_at": "2026-03-27T00:00:00Z",
                }
            ),
        }

    monkeypatch.setattr("seller_client.agent_mcp._run_backend_request", fake_run_backend_request)

    response = report_image_to_platform(
        repository="seller/demo",
        tag="v1",
        registry="https://pivotcompute.store",
        digest="sha256:test",
        source_image="python:3.12-alpine",
        state_dir=str(tmp_path),
    )

    assert response["ok"] is True
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/platform/images/report"
    assert captured["timeout_seconds"] == 240


def test_report_image_to_platform_normalizes_registry_reference(tmp_path: Path, monkeypatch) -> None:
    configure_environment(state_dir=str(tmp_path), backend_url="http://127.0.0.1:8000")
    config = get_client_config(mask_secrets=False, state_dir=str(tmp_path))["data"]
    config["auth"]["node_registration_token"] = "node-token"
    config["auth"]["device_fingerprint"] = "device-001"
    _config_path(tmp_path).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run_backend_request(method, path, **kwargs):
        captured["payload"] = kwargs["payload"]
        return {"ok": True, "body": json.dumps({"id": 1})}

    monkeypatch.setattr("seller_client.agent_mcp._run_backend_request", fake_run_backend_request)

    response = report_image_to_platform(
        repository="seller/demo",
        tag="v1",
        registry="https://pivotcompute.store",
        state_dir=str(tmp_path),
    )

    assert response["ok"] is True
    assert captured["payload"]["registry"] == "pivotcompute.store"


def test_configure_registry_trust_checks_public_https_registry(monkeypatch) -> None:
    monkeypatch.setattr(
        "seller_client.agent_mcp.fetch_registry_certificate",
        lambda registry: {
            "ok": True,
            "registry": registry,
            "publicly_trusted": True,
            "trust_probe": {"ok": True, "trusted": True},
        },
    )
    monkeypatch.setattr(
        "seller_client.agent_mcp.probe_registry",
        lambda registry: {"ok": True, "status": 200, "url": f"https://{registry}/v2/_catalog"},
    )

    response = configure_registry_trust("pivotcompute.store")

    assert response["ok"] is True
    assert response["trust_mode"] == "public_https"
    assert response["probe_result"]["status"] == 200
    assert response["legacy_input_upgraded"] is False


def test_configure_registry_trust_upgrades_legacy_ip_input(monkeypatch) -> None:
    monkeypatch.setattr(
        "seller_client.agent_mcp.fetch_registry_certificate",
        lambda registry: {
            "ok": True,
            "registry": registry,
            "publicly_trusted": True,
            "trust_probe": {"ok": True, "trusted": True},
        },
    )
    monkeypatch.setattr(
        "seller_client.agent_mcp.probe_registry",
        lambda registry: {"ok": True, "status": 200, "url": f"https://{registry}/v2/_catalog"},
    )

    response = configure_registry_trust("81.70.52.75:5000")

    assert response["ok"] is True
    assert response["registry"] == "pivotcompute.store"
    assert response["legacy_input_upgraded"] is True
