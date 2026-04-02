import base64
import json
import tarfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

import buyer_client.agent_server as agent_server


@pytest.fixture(autouse=True)
def _stub_wireguard_helper(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "wireguard_summary",
        lambda interface_name="wg-buyer", state_dir=None: {
            "platform": "Windows",
            "wireguard_windows_exe": "C:/Program Files/WireGuard/wireguard.exe",
            "wireguard_elevated_helper_installed": True,
            "wireguard_elevated_helper_task": "PivotSellerWireGuardElevated",
            "config_path": "d:/tmp/wg-buyer.conf",
        },
    )


def test_buyer_agent_server_health() -> None:
    client = TestClient(agent_server.app)
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_buyer_agent_server_serves_index_page() -> None:
    client = TestClient(agent_server.app)
    response = client.get("/")

    assert response.status_code == 200
    assert "Pivot Buyer Console" in response.text


def test_buyer_dashboard_returns_local_sessions(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "secret",
                "session_id": 1,
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "main.py",
                "status": "completed",
                "logs": "hello\n42\n",
                "relay_endpoint": "relay://buyer-runtime-session/1",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "ended_at": "2026-03-25T00:00:10Z",
            }
        },
    )
    monkeypatch.setattr(agent_server, "_read_activity", lambda state_dir, limit=20: [])

    client = TestClient(agent_server.app)
    response = client.get("/api/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["session_count"] == 1
    assert payload["sessions"][0]["seller_node_key"] == "node-001"
    assert payload["wireguard_helper"]["wireguard_helper_ready"] is True


def test_buyer_run_code_creates_local_session(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_runtime_session(**kwargs):
        captured["kwargs"] = kwargs
        return {
            "backend_url": kwargs["backend_url"],
            "buyer_email": kwargs["email"],
            "buyer_token": "buyer-token",
            "session_id": 7,
            "seller_node_key": kwargs["seller_node_key"],
            "runtime_image": kwargs["runtime_image"],
            "code_filename": kwargs["code_filename"],
            "session_mode": "code_run",
            "connect_code": "connect-code",
            "session_token": "session-token",
            "relay_endpoint": "relay://buyer-runtime-session/7",
            "create_result": {"data": {"session_id": 7}},
            "redeem_result": {"data": {"status": "created"}},
            "auth": {},
        }

    monkeypatch.setattr(
        agent_server,
        "create_runtime_session",
        fake_create_runtime_session,
    )

    client = TestClient(agent_server.app)
    response = client.post(
        "/api/runtime/run-code",
        json={
            "backend_url": "http://127.0.0.1:8011",
            "email": "buyer@example.com",
            "password": "super-secret-password",
            "display_name": "Buyer Display",
            "seller_node_key": "node-001",
            "runtime_image": "python:3.12-alpine",
            "code_filename": "main.py",
            "code_content": "print('hello')",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["session_id"] == 7
    assert payload["session"]["seller_node_key"] == "node-001"
    assert captured["kwargs"]["display_name"] == "Buyer Display"


def test_buyer_start_shell_creates_local_shell_session(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_start_shell_session(**kwargs):
        captured["kwargs"] = kwargs
        return {
            "backend_url": kwargs["backend_url"],
            "buyer_email": kwargs["email"],
            "buyer_token": "buyer-token",
            "session_id": 8,
            "seller_node_key": kwargs["seller_node_key"],
            "runtime_image": kwargs["runtime_image"],
            "code_filename": "__shell__",
            "session_mode": "shell",
            "connect_code": "connect-code",
            "session_token": "session-token",
            "relay_endpoint": "relay://buyer-runtime-session/8",
            "create_result": {"data": {"session_id": 8}},
            "redeem_result": {"data": {"status": "running"}},
            "auth": {},
        }

    monkeypatch.setattr(
        agent_server,
        "start_shell_session",
        fake_start_shell_session,
    )

    client = TestClient(agent_server.app)
    response = client.post(
        "/api/runtime/start-shell",
        json={
            "backend_url": "http://127.0.0.1:8011",
            "email": "buyer@example.com",
            "password": "super-secret-password",
            "display_name": "Shell Buyer",
            "seller_node_key": "node-001",
            "runtime_image": "python:3.12-alpine",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["session_id"] == 8
    assert payload["session"]["session_mode"] == "shell"
    assert captured["kwargs"]["display_name"] == "Shell Buyer"


def test_buyer_start_licensed_shell_creates_local_shell_session(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_start_licensed_shell_session(**kwargs):
        captured["kwargs"] = kwargs
        return {
            "backend_url": kwargs["backend_url"],
            "buyer_email": kwargs["email"],
            "buyer_token": "buyer-token",
            "order_id": 21,
            "offer_id": 9,
            "seller_node_key": "node-licensed",
            "runtime_image": "python:3.12-alpine",
            "license_token": kwargs["license_token"],
            "session_id": 18,
            "session_mode": "shell",
            "source_type": "licensed_order",
            "connect_code": "connect-code",
            "session_token": "session-token",
            "relay_endpoint": "relay://buyer-runtime-session/18",
            "start_result": {"data": {"session_id": 18, "order_id": 21}},
            "redeem_result": {"data": {"status": "running", "network_mode": "wireguard"}},
            "auth": {},
        }

    monkeypatch.setattr(
        agent_server,
        "start_licensed_shell_session",
        fake_start_licensed_shell_session,
    )

    client = TestClient(agent_server.app)
    response = client.post(
        "/api/runtime/start-licensed-shell",
        json={
            "backend_url": "http://127.0.0.1:8011",
            "email": "buyer@example.com",
            "password": "super-secret-password",
            "display_name": "Licensed Buyer",
            "license_token": "license-token-abc",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["session_id"] == 18
    assert payload["session"]["session_mode"] == "shell"
    assert payload["session"]["order_id"] == 21
    assert payload["session"]["source_type"] == "licensed_order"
    assert captured["kwargs"]["display_name"] == "Licensed Buyer"


def test_buyer_run_archive_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "run_archive",
        lambda **kwargs: {
            "session_id": 10,
            "status": "completed",
            "logs": "archive run ok",
        },
    )

    client = TestClient(agent_server.app)
    response = client.post(
        "/api/runtime/run-archive",
        json={
            "backend_url": "http://127.0.0.1:8011",
            "email": "buyer@example.com",
            "password": "super-secret-password",
            "seller_node_key": "node-001",
            "source_path": "d:/tmp/demo.zip",
            "runtime_image": "python:3.12-alpine",
            "run_command": "python main.py",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["status"] == "completed"
    assert "archive run ok" in payload["result"]["logs"]


def test_buyer_run_github_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "run_github_repo",
        lambda **kwargs: {
            "session_id": 11,
            "status": "completed",
            "logs": "github run ok",
        },
    )

    client = TestClient(agent_server.app)
    response = client.post(
        "/api/runtime/run-github",
        json={
            "backend_url": "http://127.0.0.1:8011",
            "email": "buyer@example.com",
            "password": "super-secret-password",
            "seller_node_key": "node-001",
            "repo_url": "https://github.com/example/repo",
            "repo_ref": "main",
            "runtime_image": "python:3.12-alpine",
            "run_command": "python main.py",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["status"] == "completed"
    assert "github run ok" in payload["result"]["logs"]


def test_buyer_refresh_session_reads_backend(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 3,
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "main.py",
                "session_mode": "code_run",
                "status": "created",
                "logs": "",
                "relay_endpoint": "relay://buyer-runtime-session/3",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "ended_at": None,
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "read_runtime_session",
        lambda **kwargs: {
            "session_id": 3,
            "seller_node_key": "node-001",
            "runtime_image": "python:3.12-alpine",
            "code_filename": "main.py",
            "session_mode": "code_run",
            "status": "completed",
            "service_name": "buyer-runtime-test",
            "relay_endpoint": "relay://buyer-runtime-session/3",
            "logs": "hello\n42\n",
            "ended_at": "2026-03-25T00:00:10Z",
        },
    )

    client = TestClient(agent_server.app)
    response = client.get("/api/runtime/sessions/local-1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["status"] == "completed"
    assert "42" in payload["session"]["logs"]


def test_buyer_refresh_session_preserves_local_exec_history(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 3,
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "status": "running",
                "logs": "$ python -V\nPython 3.12.0",
                "remote_logs": "",
                "local_exec_history": "$ python -V\nPython 3.12.0",
                "relay_endpoint": "relay://buyer-runtime-session/3",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "ended_at": None,
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "read_runtime_session",
        lambda **kwargs: {
            "session_id": 3,
            "seller_node_key": "node-001",
            "runtime_image": "python:3.12-alpine",
            "code_filename": "__shell__",
            "session_mode": "shell",
            "status": "running",
            "service_name": "buyer-runtime-test",
            "relay_endpoint": "relay://buyer-runtime-session/3",
            "logs": "",
            "ended_at": None,
        },
    )

    client = TestClient(agent_server.app)
    response = client.get("/api/runtime/sessions/local-1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["status"] == "running"
    assert "Python 3.12.0" in payload["session"]["logs"]


def test_buyer_exec_session_runs_gateway_command(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 9,
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "network_mode": "wireguard",
                "status": "running",
                "service_name": "buyer-runtime-shell",
                "logs": "",
                "remote_logs": "",
                "local_exec_history": "",
                "relay_endpoint": "relay://buyer-runtime-session/9",
                "session_token": "session-token",
                "gateway_host": "10.66.66.10",
                "gateway_port": 20009,
                "gateway_protocol": "http",
                "gateway_supported_features": ["exec", "logs", "shell"],
                "connection_status": "connected",
                "wireguard_status": "active",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "ended_at": None,
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "_refresh_session",
        lambda local_id: agent_server._masked_session(agent_server.SESSION_STORE[local_id]),
    )
    monkeypatch.setattr(
        agent_server,
        "gateway_exec_command",
        lambda **kwargs: {
            "ok": True,
            "command": kwargs["command"],
            "stdout": "Python 3.12.0\n",
            "stderr": "",
            "exit_code": 0,
        },
    )

    client = TestClient(agent_server.app)
    response = client.post(
        "/api/runtime/sessions/local-1/exec",
        json={"command": "python -V"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "Python 3.12.0" in payload["session"]["logs"]


def test_buyer_wireguard_bootstrap_endpoint_updates_local_session(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "state_dir": "d:/tmp/buyer-state",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 12,
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "network_mode": "wireguard",
                "status": "running",
                "service_name": "buyer-runtime-shell",
                "logs": "",
                "relay_endpoint": "relay://buyer-runtime-session/12",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "expires_at": "2026-03-25T01:00:00Z",
                "ended_at": None,
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "bootstrap_runtime_session_wireguard",
        lambda **kwargs: {
            "bundle": {
                "interface_name": "wg-buyer",
                "client_address": "10.66.66.129/32",
                "seller_wireguard_target": "10.66.66.10",
            },
            "activation_result": {"ok": True, "mode": "elevated_helper"},
        },
    )
    monkeypatch.setattr(
        agent_server,
        "wireguard_summary",
        lambda interface_name="wg-buyer", state_dir=None: {"ok": True, "config_path": "d:/tmp/wg-buyer.conf"},
    )

    client = TestClient(agent_server.app)
    response = client.post("/api/runtime/sessions/local-1/wireguard/bootstrap", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["session"]["wireguard_status"] == "active"
    assert payload["session"]["wireguard_client_address"] == "10.66.66.129/32"
    assert payload["session"]["seller_wireguard_target"] == "10.66.66.10"


def test_buyer_connect_endpoint_updates_gateway_state(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "state_dir": "d:/tmp/buyer-state",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 12,
                "session_token": "session-token",
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "network_mode": "wireguard",
                "status": "running",
                "service_name": "buyer-runtime-shell",
                "logs": "",
                "relay_endpoint": "relay://buyer-runtime-session/12",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "expires_at": "2026-03-25T01:00:00Z",
                "ended_at": None,
                "gateway_required": True,
                "gateway_protocol": "http",
                "gateway_port": 20012,
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "_refresh_session",
        lambda local_id: agent_server._masked_session(agent_server.SESSION_STORE[local_id]),
    )
    monkeypatch.setattr(
        agent_server,
        "handshake_runtime_gateway",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "gateway_service_name": "gateway-12",
            "gateway_protocol": "http",
            "gateway_host": "10.66.66.10",
            "gateway_port": 20012,
            "handshake_mode": "session_token",
            "supported_features": ["shell", "logs"],
            "seller_wireguard_target": "10.66.66.10",
        },
    )
    monkeypatch.setattr(
        agent_server,
        "bootstrap_runtime_session_wireguard",
        lambda **kwargs: {
            "bundle": {
                "interface_name": "wg-buyer",
                "client_address": "10.66.66.129/32",
                "seller_wireguard_target": "10.66.66.10",
            },
            "activation_result": {"ok": True, "mode": "elevated_helper"},
        },
    )
    monkeypatch.setattr(agent_server, "_deactivate_other_wireguard_sessions", lambda target_local_id, state_dir: None)
    monkeypatch.setattr(agent_server, "_probe_gateway", lambda record, retries=10, delay_seconds=1.0: {"ok": True})

    client = TestClient(agent_server.app)
    response = client.post("/api/runtime/sessions/local-1/connect", json={"activate_wireguard": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["session"]["gateway_host"] == "10.66.66.10"
    assert payload["session"]["connection_status"] == "connected"
    assert payload["session"]["wireguard_status"] == "active"
    assert payload["session"]["gateway_supported_features"] == ["shell", "logs"]


def test_buyer_connect_endpoint_keeps_handshaken_state_when_wireguard_activation_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "state_dir": "d:/tmp/buyer-state",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 14,
                "session_token": "session-token",
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "network_mode": "wireguard",
                "status": "running",
                "service_name": "buyer-runtime-shell",
                "logs": "",
                "relay_endpoint": "relay://buyer-runtime-session/14",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "expires_at": "2026-03-25T01:00:00Z",
                "ended_at": None,
                "gateway_required": True,
                "gateway_protocol": "http",
                "gateway_port": 20014,
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "_refresh_session",
        lambda local_id: agent_server._masked_session(agent_server.SESSION_STORE[local_id]),
    )
    monkeypatch.setattr(
        agent_server,
        "handshake_runtime_gateway",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "gateway_service_name": "gateway-14",
            "gateway_protocol": "http",
            "gateway_host": "10.66.66.10",
            "gateway_port": 20014,
            "handshake_mode": "session_token",
            "supported_features": ["shell"],
            "seller_wireguard_target": "10.66.66.10",
        },
    )
    monkeypatch.setattr(
        agent_server,
        "bootstrap_runtime_session_wireguard",
        lambda **kwargs: {
            "bundle": {
                "interface_name": "wg-buyer",
                "client_address": "10.66.66.140/32",
                "seller_wireguard_target": "10.66.66.10",
            },
            "activation_result": {"ok": False, "error": "wireguard_elevated_helper_not_installed"},
        },
    )
    monkeypatch.setattr(agent_server, "_deactivate_other_wireguard_sessions", lambda target_local_id, state_dir: None)

    client = TestClient(agent_server.app)
    response = client.post("/api/runtime/sessions/local-1/connect", json={"activate_wireguard": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["session"]["gateway_host"] == "10.66.66.10"
    assert payload["session"]["connection_status"] == "handshaken"
    assert payload["session"]["wireguard_status"] == "activation_failed"
    assert payload["session"]["wireguard_activation_mode"] == "failed"
    assert "handshake complete" in payload["activity_entry"]["summary"]


def test_buyer_logs_endpoint_reads_gateway_logs(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "state_dir": "d:/tmp/buyer-state",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 15,
                "session_token": "session-token",
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "network_mode": "wireguard",
                "status": "running",
                "service_name": "buyer-runtime-shell",
                "logs": "",
                "remote_logs": "",
                "relay_endpoint": "relay://buyer-runtime-session/15",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "expires_at": "2026-03-25T01:00:00Z",
                "ended_at": None,
                "gateway_required": True,
                "gateway_protocol": "http",
                "gateway_port": 20015,
                "gateway_host": "10.66.66.10",
                "gateway_supported_features": ["logs"],
                "connection_status": "connected",
                "wireguard_status": "active",
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "_refresh_session",
        lambda local_id: agent_server._masked_session(agent_server.SESSION_STORE[local_id]),
    )
    monkeypatch.setattr(
        agent_server,
        "gateway_read_logs",
        lambda **kwargs: {
            "ok": True,
            "cursor": kwargs["cursor"],
            "next_cursor": 2,
            "total_lines": 2,
            "logs": "line-1\nline-2",
            "lines": ["line-1", "line-2"],
        },
    )

    client = TestClient(agent_server.app)
    response = client.get("/api/runtime/sessions/local-1/logs?cursor=0&limit=20")

    assert response.status_code == 200
    payload = response.json()
    assert payload["log_result"]["logs"] == "line-1\nline-2"
    assert payload["session"]["logs"] == "line-1\nline-2"


def test_buyer_upload_files_endpoint_sends_gateway_archive(monkeypatch, tmp_path) -> None:
    source_path = tmp_path / "input.txt"
    source_path.write_text("hello gateway upload", encoding="utf-8")
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "state_dir": str(tmp_path),
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 20,
                "session_token": "session-token",
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "source_type": "licensed_order",
                "network_mode": "wireguard",
                "status": "running",
                "service_name": "buyer-runtime-shell",
                "logs": "",
                "relay_endpoint": "relay://buyer-runtime-session/20",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "expires_at": "2026-03-25T01:00:00Z",
                "ended_at": None,
                "gateway_required": True,
                "gateway_protocol": "http",
                "gateway_port": 20020,
                "gateway_host": "10.66.66.10",
                "gateway_supported_features": ["files"],
                "connection_status": "connected",
                "wireguard_status": "active",
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "_refresh_session",
        lambda local_id: agent_server._masked_session(agent_server.SESSION_STORE[local_id]),
    )

    def fake_gateway_upload_archive(**kwargs):
        seen["remote_path"] = kwargs["remote_path"]
        seen["archive_base64"] = kwargs["archive_base64"]
        return {
            "ok": True,
            "uploaded_path": "/workspace/input.txt",
            "entry_name": "input.txt",
            "is_dir": False,
            "size_bytes": 20,
        }

    monkeypatch.setattr(agent_server, "gateway_upload_archive", fake_gateway_upload_archive)

    client = TestClient(agent_server.app)
    response = client.post(
        "/api/runtime/sessions/local-1/files/upload",
        json={"local_path": str(source_path), "remote_path": "/workspace"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["upload_result"]["uploaded_path"] == "/workspace/input.txt"
    assert payload["upload_result"]["entry_name"] == "input.txt"
    assert payload["upload_result"]["local_path"] == str(source_path.resolve())
    assert seen["remote_path"] == "/workspace"
    assert seen["archive_base64"]


def test_buyer_download_files_endpoint_restores_local_content(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "downloaded" / "result.txt"
    archive_buffer = BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
        payload = b"hello from runtime file"
        info = tarfile.TarInfo(name="result.txt")
        info.size = len(payload)
        archive.addfile(info, BytesIO(payload))

    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "state_dir": str(tmp_path),
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 22,
                "session_token": "session-token",
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "source_type": "licensed_order",
                "network_mode": "wireguard",
                "status": "running",
                "service_name": "buyer-runtime-shell",
                "logs": "",
                "relay_endpoint": "relay://buyer-runtime-session/22",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "expires_at": "2026-03-25T01:00:00Z",
                "ended_at": None,
                "gateway_required": True,
                "gateway_protocol": "http",
                "gateway_port": 20022,
                "gateway_host": "10.66.66.10",
                "gateway_supported_features": ["files"],
                "connection_status": "connected",
                "wireguard_status": "active",
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "_refresh_session",
        lambda local_id: agent_server._masked_session(agent_server.SESSION_STORE[local_id]),
    )
    monkeypatch.setattr(
        agent_server,
        "gateway_download_archive",
        lambda **kwargs: {
            "ok": True,
            "path": kwargs["remote_path"],
            "entry_name": "result.txt",
            "is_dir": False,
            "size_bytes": 23,
            "archive_base64": base64.b64encode(archive_buffer.getvalue()).decode("ascii"),
        },
    )

    client = TestClient(agent_server.app)
    response = client.post(
        "/api/runtime/sessions/local-1/files/download",
        json={"remote_path": "/workspace/result.txt", "local_path": str(output_path)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["download_result"]["local_path"] == str(output_path.resolve())
    assert output_path.read_text(encoding="utf-8") == "hello from runtime file"


def test_buyer_wireguard_helper_install_endpoint_returns_apply_command(monkeypatch) -> None:
    states = iter(
        [
            {
                "wireguard_helper_ready": False,
                "wireguard_helper_error": "wireguard_elevated_helper_not_installed",
                "wireguard_helper_install_hint": "Run install once as administrator.",
                "wireguard_helper_apply_command": "powershell -ExecutionPolicy Bypass -File helper.ps1 -Apply",
            },
            {
                "wireguard_helper_ready": False,
                "wireguard_helper_error": "wireguard_elevated_helper_not_installed",
                "wireguard_helper_install_hint": "Run install once as administrator.",
                "wireguard_helper_apply_command": "powershell -ExecutionPolicy Bypass -File helper.ps1 -Apply",
            },
        ]
    )
    monkeypatch.setattr(agent_server, "_wireguard_helper_status", lambda state_dir=None: next(states))
    monkeypatch.setattr(
        agent_server,
        "_launch_wireguard_helper_installer",
        lambda state_dir, attempt_launch: {
            "ok": True,
            "attempted_launch": True,
            "launch_started": True,
            "windows_apply_command": "powershell -ExecutionPolicy Bypass -File helper.ps1 -Apply",
        },
    )

    client = TestClient(agent_server.app)
    response = client.post("/api/runtime/wireguard-helper/install", json={"attempt_launch": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["install_result"]["launch_started"] is True
    assert payload["wireguard_helper"]["wireguard_helper_apply_command"].endswith("-Apply")


def test_buyer_terminal_websocket_bridges_gateway(monkeypatch) -> None:
    seen: dict[str, int] = {}
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "state_dir": "d:/tmp/buyer-state",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 16,
                "session_token": "session-token",
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "network_mode": "wireguard",
                "status": "running",
                "service_name": "buyer-runtime-shell",
                "logs": "",
                "relay_endpoint": "relay://buyer-runtime-session/16",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "expires_at": "2026-03-25T01:00:00Z",
                "ended_at": None,
                "gateway_required": True,
                "gateway_protocol": "http",
                "gateway_port": 20016,
                "gateway_host": "10.66.66.10",
                "gateway_supported_features": ["shell"],
                "connection_status": "connected",
                "wireguard_status": "active",
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "_refresh_session",
        lambda local_id: agent_server._masked_session(agent_server.SESSION_STORE[local_id]),
    )

    async def fake_bridge(websocket, record, *, rows, cols) -> None:
        seen["rows"] = rows
        seen["cols"] = cols
        await websocket.send_text(json.dumps({"type": "output", "data": "Python 3.12.13\r\n"}))

    monkeypatch.setattr(agent_server, "_bridge_runtime_terminal", fake_bridge)

    client = TestClient(agent_server.app)
    with client.websocket_connect("/api/runtime/sessions/local-1/terminal?rows=40&cols=120") as websocket:
        payload = json.loads(websocket.receive_text())

    assert payload["type"] == "output"
    assert "Python 3.12.13" in payload["data"]
    assert seen == {"rows": 40, "cols": 120}


def test_buyer_renew_endpoint_updates_expiry(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "state_dir": "d:/tmp/buyer-state",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 13,
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "network_mode": "wireguard",
                "status": "running",
                "service_name": "buyer-runtime-shell",
                "logs": "",
                "relay_endpoint": "relay://buyer-runtime-session/13",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "expires_at": "2026-03-25T01:00:00Z",
                "ended_at": None,
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "renew_backend_runtime_session",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "status": "running",
            "expires_at": "2026-03-25T01:30:00Z",
        },
    )

    client = TestClient(agent_server.app)
    response = client.post("/api/runtime/sessions/local-1/renew", json={"additional_minutes": 30})

    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["expires_at"] == "2026-03-25T01:30:00Z"
    assert payload["renew_result"]["status"] == "running"


def test_buyer_dashboard_includes_codex_status(monkeypatch) -> None:
    monkeypatch.setattr(agent_server, "SESSION_STORE", {})
    monkeypatch.setattr(agent_server, "_read_activity", lambda state_dir, limit=20: [])
    monkeypatch.setattr(
        agent_server,
        "codex_status",
        lambda state_dir: {
            "codex_ready": True,
            "codex_cli": "codex",
            "buyer_mcp_attached": True,
            "jobs": [{"job_id": "job-1", "status": "running"}],
            "job_count": 1,
        },
    )

    client = TestClient(agent_server.app)
    response = client.get("/api/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["codex"]["codex_ready"] is True
    assert payload["codex"]["jobs"][0]["job_id"] == "job-1"


def test_buyer_start_codex_job_endpoint(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        agent_server,
        "SESSION_STORE",
        {
            "local-1": {
                "local_id": "local-1",
                "state_dir": "d:/tmp/buyer-state",
                "backend_url": "http://127.0.0.1:8011",
                "buyer_email": "buyer@example.com",
                "buyer_token": "buyer-token",
                "session_id": 30,
                "session_token": "session-token",
                "seller_node_key": "node-001",
                "runtime_image": "python:3.12-alpine",
                "code_filename": "__shell__",
                "session_mode": "shell",
                "network_mode": "wireguard",
                "status": "running",
                "service_name": "buyer-runtime-shell",
                "logs": "",
                "relay_endpoint": "relay://buyer-runtime-session/30",
                "connect_code": "abc123",
                "created_at": "2026-03-25T00:00:00Z",
                "expires_at": "2026-03-25T01:00:00Z",
                "ended_at": None,
                "gateway_required": True,
                "gateway_protocol": "http",
                "gateway_port": 20030,
                "gateway_host": "10.66.66.10",
                "gateway_supported_features": ["exec", "logs", "shell", "files"],
                "connection_status": "connected",
                "wireguard_status": "active",
            }
        },
    )
    monkeypatch.setattr(
        agent_server,
        "_refresh_session",
        lambda local_id: agent_server._masked_session(agent_server.SESSION_STORE[local_id]),
    )
    monkeypatch.setattr(
        agent_server,
        "create_codex_job",
        lambda **kwargs: captured.update(kwargs)
        or {
            "job_id": "job-123",
            "local_id": kwargs["local_id"],
            "workspace_path": kwargs["workspace_path"],
            "status": "running",
            "user_prompt": kwargs["user_prompt"],
            "logs": "",
            "final_message": "",
        },
    )

    client = TestClient(agent_server.app)
    response = client.post(
        "/api/codex/jobs",
        json={
            "local_id": "local-1",
            "workspace_path": "d:/tmp/codex-workspace",
            "prompt": "Inspect the workspace and run the container task.",
            "model": "gpt-5.4",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["job_id"] == "job-123"
    assert payload["job"]["local_id"] == "local-1"
    assert captured["backend_url"] == "http://127.0.0.1:8011"
    assert captured["buyer_token"] == "buyer-token"


def test_buyer_read_codex_job_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "get_codex_job",
        lambda job_id, state_dir: {
            "job_id": job_id,
            "status": "completed",
            "logs": "job log",
            "final_message": "done",
        },
    )

    client = TestClient(agent_server.app)
    response = client.get("/api/codex/jobs/job-123")

    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["job_id"] == "job-123"
    assert payload["job"]["final_message"] == "done"


def test_buyer_cancel_codex_job_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_server,
        "cancel_codex_job",
        lambda job_id, state_dir: {
            "job_id": job_id,
            "status": "canceling",
            "logs": "stopping",
            "final_message": "",
        },
    )

    client = TestClient(agent_server.app)
    response = client.post("/api/codex/jobs/job-123/cancel", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["job_id"] == "job-123"
    assert payload["job"]["status"] == "canceling"
