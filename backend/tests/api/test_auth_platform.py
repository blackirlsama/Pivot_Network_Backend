from types import SimpleNamespace

from fastapi.testclient import TestClient


def _fake_bundle_create(_settings, **_kwargs):
    return {"ok": True, "runtime": {"ok": True}, "gateway": {"ok": True}}


def _running_bundle_inspect(logs: str = ""):
    return {
        "runtime": {
            "tasks": [{"CurrentState": "Running 1 second ago", "DesiredState": "Running"}],
            "current_task": {"CurrentState": "Running 1 second ago", "DesiredState": "Running"},
            "logs": logs,
        },
        "gateway": {
            "tasks": [{"CurrentState": "Running 1 second ago", "DesiredState": "Running"}],
            "current_task": {"CurrentState": "Running 1 second ago", "DesiredState": "Running"},
            "logs": "",
        },
    }


def test_register_login_and_me_flow(client: TestClient) -> None:
    register_response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller@example.com",
            "password": "super-secret-password",
            "display_name": "Seller One",
        },
    )
    assert register_response.status_code == 201
    assert register_response.json()["email"] == "seller@example.com"
    assert register_response.json()["seller_status"] == "active"
    assert register_response.json()["buyer_status"] == "active"

    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": "seller@example.com", "password": "super-secret-password"},
    )
    assert login_response.status_code == 200
    assert login_response.json()["user"]["seller_status"] == "active"
    assert login_response.json()["user"]["buyer_status"] == "active"
    token = login_response.json()["access_token"]

    me_response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_response.status_code == 200
    assert me_response.json()["seller_status"] == "active"
    assert me_response.json()["buyer_status"] == "active"


def test_same_user_can_access_buyer_and_seller_surfaces(client: TestClient) -> None:
    client.post(
        "/api/v1/auth/register",
        json={
            "email": "hybrid-user@example.com",
            "password": "super-secret-password",
            "display_name": "Hybrid User",
        },
    )
    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": "hybrid-user@example.com", "password": "super-secret-password"},
    )
    token = login_response.json()["access_token"]

    wallet_response = client.get(
        "/api/v1/buyer/wallet",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert wallet_response.status_code == 200
    assert wallet_response.json()["buyer_user_id"] > 0
    assert wallet_response.json()["balance_cny_credits"] == 100.0

    node_token_response = client.post(
        "/api/v1/platform/node-registration-token",
        json={"label": "hybrid-user-node", "expires_hours": 48},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert node_token_response.status_code == 200
    assert node_token_response.json()["node_registration_token"]


def test_node_registration_heartbeat_and_image_report_flow(client: TestClient, monkeypatch) -> None:
    client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller2@example.com",
            "password": "super-secret-password",
            "display_name": "Seller Two",
        },
    )
    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": "seller2@example.com", "password": "super-secret-password"},
    )
    access_token = login_response.json()["access_token"]

    token_response = client.post(
        "/api/v1/platform/node-registration-token",
        json={"label": "desktop-node", "expires_hours": 48},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert token_response.status_code == 200
    node_token = token_response.json()["node_registration_token"]

    register_response = client.post(
        "/api/v1/platform/nodes/register",
        json={
            "node_id": "node-001",
            "device_fingerprint": "device-001",
            "hostname": "seller-host",
            "system": "Windows",
            "machine": "AMD64",
            "shared_percent_preference": 10,
            "capabilities": {"cpu_count_logical": 24, "memory_total_mb": 32000},
            "seller_intent": "我能把自己电脑性能的10%上传到平台获取收益吗",
            "docker_status": "ready",
            "swarm_state": "active",
            "node_class": "cpu-basic",
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )
    assert register_response.status_code == 200
    assert register_response.json()["shared_percent_preference"] == 10
    assert register_response.json()["seller_intent"].startswith("我能把自己电脑性能的10%")

    heartbeat_response = client.post(
        "/api/v1/platform/nodes/heartbeat",
        json={
            "node_id": "node-001",
            "status": "available",
            "docker_status": "running",
            "swarm_state": "active",
            "capabilities": {"cpu_count_logical": 24, "memory_total_mb": 32000},
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )
    assert heartbeat_response.status_code == 200
    assert heartbeat_response.json()["docker_status"] == "running"

    monkeypatch.setattr(
        "app.api.routes.platform.run_offer_probe_and_pricing",
        lambda db, **kwargs: SimpleNamespace(id=1, current_billable_price_cny_per_hour=1.23),
    )

    image_response = client.post(
        "/api/v1/platform/images/report",
        json={
            "node_id": "node-001",
            "repository": "seller/demo",
            "tag": "v1",
            "digest": "sha256:test",
            "registry": "pivotcompute.store",
            "source_image": "alpine:3.20",
            "status": "uploaded",
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )
    assert image_response.status_code == 200
    assert image_response.json()["repository"] == "seller/demo"

    overview_response = client.get(
        "/api/v1/platform/overview",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert overview_response.status_code == 200
    assert overview_response.json()["node_count"] == 1
    assert overview_response.json()["image_count"] == 1

    nodes_response = client.get(
        "/api/v1/platform/nodes",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert nodes_response.status_code == 200
    node_id = nodes_response.json()[0]["id"]
    assert nodes_response.json()[0]["ready_for_registry_push"] is True

    node_detail_response = client.get(
        f"/api/v1/platform/nodes/{node_id}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert node_detail_response.status_code == 200

    images_response = client.get(
        "/api/v1/platform/images",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert images_response.status_code == 200
    image_id = images_response.json()[0]["id"]
    assert images_response.json()[0]["push_ready"] is True

    image_detail_response = client.get(
        f"/api/v1/platform/images/{image_id}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert image_detail_response.status_code == 200

    tokens_response = client.get(
        "/api/v1/platform/node-registration-tokens",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert tokens_response.status_code == 200
    assert tokens_response.json()[0]["used_node_key"] == "node-001"

    activity_response = client.get(
        "/api/v1/platform/activity",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert activity_response.status_code == 200
    event_types = [event["event_type"] for event in activity_response.json()]
    assert "node_token_issued" in event_types
    assert "node_registered" in event_types
    assert "node_heartbeat" in event_types
    assert "image_reported" in event_types


def test_codex_runtime_and_wireguard_bootstrap_flow(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.platform.settings.OPENAI_API_KEY", "test-platform-key")
    monkeypatch.setattr("app.api.routes.platform.settings.WIREGUARD_SERVER_PUBLIC_KEY", "server-public-key")
    monkeypatch.setattr("app.api.routes.platform.settings.WIREGUARD_ENDPOINT_HOST", "pivotcompute.store")
    monkeypatch.setattr("app.api.routes.platform.settings.WIREGUARD_ENDPOINT_PORT", 45182)
    monkeypatch.setattr("app.api.routes.platform.settings.WIREGUARD_NETWORK_CIDR", "10.66.66.0/24")
    monkeypatch.setattr("app.api.routes.platform.settings.WIREGUARD_ALLOWED_IPS", "10.66.66.0/24")
    monkeypatch.setattr("app.api.routes.platform.settings.WIREGUARD_SERVER_SSH_ENABLED", True)
    monkeypatch.setattr(
        "app.api.routes.platform.apply_server_peer",
        lambda settings, public_key, client_address, persistent_keepalive: {
            "ok": True,
            "apply_result": {"ok": True},
            "upsert_result": {"ok": True},
            "inspect_result": {"ok": True},
        },
    )

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller3@example.com",
            "password": "super-secret-password",
            "display_name": "Seller Three",
        },
    )
    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": "seller3@example.com", "password": "super-secret-password"},
    )
    access_token = login_response.json()["access_token"]

    codex_response = client.get(
        "/api/v1/platform/runtime/codex",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert codex_response.status_code == 200
    assert codex_response.json()["model"] == "gpt-5"
    assert codex_response.json()["model_provider"] == "fox"
    assert codex_response.json()["provider"]["name"] == "fox"
    assert codex_response.json()["provider"]["base_url"] == "https://code.newcli.com/codex/v1"
    assert codex_response.json()["auth"]["OPENAI_API_KEY"] == "test-platform-key"

    token_response = client.post(
        "/api/v1/platform/node-registration-token",
        json={"label": "desktop-node", "expires_hours": 48},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    node_token = token_response.json()["node_registration_token"]

    register_response = client.post(
        "/api/v1/platform/nodes/register",
        json={
            "node_id": "node-002",
            "device_fingerprint": "device-002",
            "hostname": "seller-host-2",
            "system": "Windows",
            "machine": "AMD64",
            "shared_percent_preference": 25,
            "capabilities": {"cpu_count_logical": 16},
            "seller_intent": "请把我的机器接入平台",
            "docker_status": "ready",
            "swarm_state": "inactive",
            "node_class": "cpu-basic",
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )
    assert register_response.status_code == 200

    wireguard_response = client.post(
        "/api/v1/platform/nodes/wireguard/bootstrap",
        json={
            "node_id": "node-002",
            "client_public_key": "client-public-key",
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )
    assert wireguard_response.status_code == 200
    payload = wireguard_response.json()
    assert payload["interface_name"] == "wg-seller"
    assert payload["server_endpoint_host"] == "pivotcompute.store"
    assert payload["client_address"].endswith("/32")
    assert payload["server_peer_apply_required"] is False
    assert payload["server_peer_apply_status"] == "applied"
    assert payload["activation_mode"] == "server_peer_applied"


def test_logged_in_buyer_can_fetch_codex_runtime_bootstrap(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.platform.settings.OPENAI_API_KEY", "test-platform-key")

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "buyer-codex@example.com",
            "password": "super-secret-password",
            "display_name": "Buyer Codex",
        },
    )
    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": "buyer-codex@example.com", "password": "super-secret-password"},
    )
    access_token = login_response.json()["access_token"]

    codex_response = client.get(
        "/api/v1/platform/runtime/codex",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert codex_response.status_code == 200
    assert codex_response.json()["auth"]["OPENAI_API_KEY"] == "test-platform-key"

    activity_response = client.get(
        "/api/v1/platform/activity",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    event_types = [event["event_type"] for event in activity_response.json()]
    assert "codex_runtime_issued" in event_types


def test_remote_swarm_overview_and_worker_join_token_endpoints(client: TestClient, monkeypatch) -> None:
    client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller4@example.com",
            "password": "super-secret-password",
            "display_name": "Seller Four",
        },
    )
    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": "seller4@example.com", "password": "super-secret-password"},
    )
    access_token = login_response.json()["access_token"]

    monkeypatch.setattr(
        "app.api.routes.platform.get_worker_join_token",
        lambda settings: {
            "join_token": "SWMTKN-test",
            "manager_host": "pivotcompute.store",
            "manager_port": 2377,
        },
    )
    monkeypatch.setattr(
        "app.api.routes.platform.get_manager_overview",
        lambda settings: {
            "manager_host": "pivotcompute.store",
            "manager_port": 2377,
            "swarm": {
                "state": "active",
                "node_id": "manager-node",
                "node_addr": "pivotcompute.store",
                "control_available": True,
                "nodes": 2,
                "managers": 1,
                "cluster_id": "cluster-1",
            },
            "node_list": "docker-desktop Ready Active",
            "service_list": "portainer_agent global",
        },
    )

    token_response = client.get(
        "/api/v1/platform/swarm/worker-join-token",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert token_response.status_code == 200
    assert token_response.json()["join_token"] == "SWMTKN-test"

    overview_response = client.get(
        "/api/v1/platform/swarm/overview",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert overview_response.status_code == 200
    assert overview_response.json()["swarm"]["nodes"] == 2
    assert "docker-desktop" in overview_response.json()["node_list"]

    activity_response = client.get(
        "/api/v1/platform/activity",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    event_types = [event["event_type"] for event in activity_response.json()]
    assert "swarm_worker_join_token_issued" in event_types
    assert "swarm_overview_viewed" in event_types


def test_buyer_runtime_session_flow(client: TestClient, monkeypatch) -> None:
    client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller-runtime@example.com",
            "password": "super-secret-password",
            "display_name": "Runtime Seller",
        },
    )
    seller_login = client.post(
        "/api/v1/auth/login",
        json={"email": "seller-runtime@example.com", "password": "super-secret-password"},
    )
    seller_token = seller_login.json()["access_token"]
    node_token_response = client.post(
        "/api/v1/platform/node-registration-token",
        json={"label": "buyer-runtime-node", "expires_hours": 48},
        headers={"Authorization": f"Bearer {seller_token}"},
    )
    node_token = node_token_response.json()["node_registration_token"]
    client.post(
        "/api/v1/platform/nodes/register",
        json={
            "node_id": "buyer-runtime-node-001",
            "device_fingerprint": "device-rt-001",
            "hostname": "docker-desktop",
            "system": "Windows",
            "machine": "AMD64",
            "shared_percent_preference": 10,
            "capabilities": {
                "cpu_count_logical": 24,
                "interfaces": {
                    "wg-seller": [
                        {"family": 2, "address": "10.66.66.10"},
                    ]
                },
            },
            "seller_intent": "seller runtime test",
            "docker_status": "ready",
            "swarm_state": "active",
            "node_class": "cpu-basic",
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "buyer-runtime@example.com",
            "password": "super-secret-password",
            "display_name": "Runtime Buyer",
        },
    )
    buyer_login = client.post(
        "/api/v1/auth/login",
        json={"email": "buyer-runtime@example.com", "password": "super-secret-password"},
    )
    buyer_token = buyer_login.json()["access_token"]

    monkeypatch.setattr("app.api.routes.buyer.create_runtime_session_bundle", _fake_bundle_create)
    monkeypatch.setattr(
        "app.api.routes.buyer.inspect_runtime_session_bundle",
        lambda settings, **kwargs: _running_bundle_inspect(logs="hello from buyer runtime"),
    )
    monkeypatch.setattr("app.api.routes.buyer.remove_runtime_session_bundle", lambda settings, **kwargs: {"ok": True})

    create_response = client.post(
        "/api/v1/buyer/runtime-sessions",
        json={
            "seller_node_key": "buyer-runtime-node-001",
            "runtime_image": "python:3.12-alpine",
            "code_filename": "main.py",
            "code_content": "print('hello from buyer runtime')",
        },
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert create_response.status_code == 200
    create_payload = create_response.json()
    assert create_payload["seller_node_key"] == "buyer-runtime-node-001"

    redeem_response = client.post(
        "/api/v1/buyer/runtime-sessions/redeem",
        json={"connect_code": create_payload["connect_code"]},
    )
    assert redeem_response.status_code == 200
    assert redeem_response.json()["access_mode"] == "relay"
    assert redeem_response.json()["gateway_required"] is True
    assert redeem_response.json()["gateway_port"] is not None

    session_id = create_payload["session_id"]
    status_response = client.get(
        f"/api/v1/buyer/runtime-sessions/{session_id}",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "running"
    assert status_response.json()["gateway_status"] == "online"
    assert "hello from buyer runtime" in status_response.json()["logs"]

    stop_response = client.post(
        f"/api/v1/buyer/runtime-sessions/{session_id}/stop",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert stop_response.status_code == 200
    assert stop_response.json()["status"] == "stopped"


def test_buyer_runtime_session_create_requires_seller_wireguard_ready(client: TestClient) -> None:
    client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller-runtime-missing-wg@example.com",
            "password": "super-secret-password",
            "display_name": "Runtime Seller Missing WG",
        },
    )
    seller_login = client.post(
        "/api/v1/auth/login",
        json={"email": "seller-runtime-missing-wg@example.com", "password": "super-secret-password"},
    )
    seller_token = seller_login.json()["access_token"]
    node_token_response = client.post(
        "/api/v1/platform/node-registration-token",
        json={"label": "buyer-runtime-node-missing-wg", "expires_hours": 48},
        headers={"Authorization": f"Bearer {seller_token}"},
    )
    node_token = node_token_response.json()["node_registration_token"]
    client.post(
        "/api/v1/platform/nodes/register",
        json={
            "node_id": "buyer-runtime-node-missing-wg-001",
            "device_fingerprint": "device-rt-missing-wg-001",
            "hostname": "docker-desktop",
            "system": "Windows",
            "machine": "AMD64",
            "shared_percent_preference": 10,
            "capabilities": {"cpu_count_logical": 24},
            "seller_intent": "seller runtime test missing wireguard",
            "docker_status": "ready",
            "swarm_state": "active",
            "node_class": "cpu-basic",
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "buyer-runtime-missing-wg@example.com",
            "password": "super-secret-password",
            "display_name": "Runtime Buyer Missing WG",
        },
    )
    buyer_login = client.post(
        "/api/v1/auth/login",
        json={"email": "buyer-runtime-missing-wg@example.com", "password": "super-secret-password"},
    )
    buyer_token = buyer_login.json()["access_token"]

    create_response = client.post(
        "/api/v1/buyer/runtime-sessions",
        json={
            "seller_node_key": "buyer-runtime-node-missing-wg-001",
            "runtime_image": "python:3.12-alpine",
            "code_filename": "main.py",
            "code_content": "print('hello from buyer runtime')",
        },
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert create_response.status_code == 409
    assert create_response.json()["detail"] == "seller_node_wireguard_not_ready"


def test_buyer_runtime_session_archive_source_flow(client: TestClient, monkeypatch) -> None:
    client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller-archive@example.com",
            "password": "super-secret-password",
            "display_name": "Archive Seller",
        },
    )
    seller_login = client.post(
        "/api/v1/auth/login",
        json={"email": "seller-archive@example.com", "password": "super-secret-password"},
    )
    seller_token = seller_login.json()["access_token"]
    node_token_response = client.post(
        "/api/v1/platform/node-registration-token",
        json={"label": "archive-node", "expires_hours": 48},
        headers={"Authorization": f"Bearer {seller_token}"},
    )
    node_token = node_token_response.json()["node_registration_token"]
    client.post(
        "/api/v1/platform/nodes/register",
        json={
            "node_id": "archive-node-001",
            "device_fingerprint": "device-archive-001",
            "hostname": "docker-desktop",
            "system": "Windows",
            "machine": "AMD64",
            "shared_percent_preference": 10,
            "capabilities": {
                "cpu_count_logical": 24,
                "interfaces": {
                    "wg-seller": [
                        {"family": 2, "address": "10.66.66.10"},
                    ]
                },
            },
            "seller_intent": "archive seller test",
            "docker_status": "ready",
            "swarm_state": "state=active node_id=ql6wifxs5vfs2d8ezr884pihx",
            "node_class": "cpu-basic",
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "buyer-archive@example.com",
            "password": "super-secret-password",
            "display_name": "Archive Buyer",
        },
    )
    buyer_login = client.post(
        "/api/v1/auth/login",
        json={"email": "buyer-archive@example.com", "password": "super-secret-password"},
    )
    buyer_token = buyer_login.json()["access_token"]

    monkeypatch.setattr("app.api.routes.buyer.create_runtime_session_bundle", _fake_bundle_create)
    monkeypatch.setattr(
        "app.api.routes.buyer.inspect_runtime_session_bundle",
        lambda settings, **kwargs: _running_bundle_inspect(),
    )

    create_response = client.post(
        "/api/v1/buyer/runtime-sessions",
        json={
            "seller_node_key": "archive-node-001",
            "source_type": "archive",
            "archive_filename": "workspace.zip",
            "archive_content_base64": "UEsDBAoAAAAAA",
            "runtime_image": "python:3.12-alpine",
            "code_filename": "workspace.zip",
            "working_dir": "",
            "run_command": ["python", "main.py"],
        },
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert create_response.status_code == 200
    assert create_response.json()["source_type"] == "archive"


def test_buyer_runtime_session_renew_flow(client: TestClient, monkeypatch) -> None:
    client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller-renew@example.com",
            "password": "super-secret-password",
            "display_name": "Renew Seller",
        },
    )
    seller_login = client.post(
        "/api/v1/auth/login",
        json={"email": "seller-renew@example.com", "password": "super-secret-password"},
    )
    seller_token = seller_login.json()["access_token"]
    node_token_response = client.post(
        "/api/v1/platform/node-registration-token",
        json={"label": "renew-node", "expires_hours": 48},
        headers={"Authorization": f"Bearer {seller_token}"},
    )
    node_token = node_token_response.json()["node_registration_token"]
    client.post(
        "/api/v1/platform/nodes/register",
        json={
            "node_id": "renew-node-001",
            "device_fingerprint": "device-renew-001",
            "hostname": "docker-desktop",
            "system": "Windows",
            "machine": "AMD64",
            "shared_percent_preference": 10,
            "capabilities": {
                "cpu_count_logical": 24,
                "interfaces": {
                    "wg-seller": [
                        {"family": 2, "address": "10.66.66.10"},
                    ]
                },
            },
            "seller_intent": "renew seller test",
            "docker_status": "ready",
            "swarm_state": "state=active node_id=ql6wifxs5vfs2d8ezr884pihx",
            "node_class": "cpu-basic",
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "buyer-renew@example.com",
            "password": "super-secret-password",
            "display_name": "Renew Buyer",
        },
    )
    buyer_login = client.post(
        "/api/v1/auth/login",
        json={"email": "buyer-renew@example.com", "password": "super-secret-password"},
    )
    buyer_token = buyer_login.json()["access_token"]

    monkeypatch.setattr("app.api.routes.buyer.create_runtime_session_bundle", _fake_bundle_create)
    monkeypatch.setattr(
        "app.api.routes.buyer.inspect_runtime_session_bundle",
        lambda settings, **kwargs: _running_bundle_inspect(),
    )

    create_response = client.post(
        "/api/v1/buyer/runtime-sessions",
        json={
            "seller_node_key": "renew-node-001",
            "session_mode": "shell",
            "runtime_image": "python:3.12-alpine",
            "requested_duration_minutes": 30,
        },
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["session_id"]

    renew_response = client.post(
        f"/api/v1/buyer/runtime-sessions/{session_id}/renew",
        json={"additional_minutes": 15},
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert renew_response.status_code == 200
    assert renew_response.json()["status"] == "running"
    assert renew_response.json()["expires_at"] is not None


def test_buyer_runtime_session_wireguard_bootstrap_flow(client: TestClient, monkeypatch) -> None:
    client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller-wireguard@example.com",
            "password": "super-secret-password",
            "display_name": "WireGuard Seller",
        },
    )
    seller_login = client.post(
        "/api/v1/auth/login",
        json={"email": "seller-wireguard@example.com", "password": "super-secret-password"},
    )
    seller_token = seller_login.json()["access_token"]
    node_token_response = client.post(
        "/api/v1/platform/node-registration-token",
        json={"label": "wg-node", "expires_hours": 48},
        headers={"Authorization": f"Bearer {seller_token}"},
    )
    node_token = node_token_response.json()["node_registration_token"]
    client.post(
        "/api/v1/platform/nodes/register",
        json={
            "node_id": "wg-node-001",
            "device_fingerprint": "device-wg-001",
            "hostname": "docker-desktop",
            "system": "Windows",
            "machine": "AMD64",
            "shared_percent_preference": 10,
            "capabilities": {
                "cpu_count_logical": 24,
                "interfaces": {
                    "wg-seller": [
                        {"family": 2, "address": "10.66.66.10"},
                    ]
                },
            },
            "seller_intent": "wireguard seller test",
            "docker_status": "ready",
            "swarm_state": "state=active node_id=ql6wifxs5vfs2d8ezr884pihx",
            "node_class": "cpu-basic",
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "buyer-wireguard@example.com",
            "password": "super-secret-password",
            "display_name": "WireGuard Buyer",
        },
    )
    buyer_login = client.post(
        "/api/v1/auth/login",
        json={"email": "buyer-wireguard@example.com", "password": "super-secret-password"},
    )
    buyer_token = buyer_login.json()["access_token"]

    monkeypatch.setattr("app.api.routes.buyer.create_runtime_session_bundle", _fake_bundle_create)
    monkeypatch.setattr(
        "app.api.routes.buyer.inspect_runtime_session_bundle",
        lambda settings, **kwargs: _running_bundle_inspect(),
    )
    monkeypatch.setattr("app.api.routes.buyer.settings.WIREGUARD_SERVER_PUBLIC_KEY", "server-public-key")
    monkeypatch.setattr("app.api.routes.buyer.settings.WIREGUARD_ENDPOINT_HOST", "pivotcompute.store")
    monkeypatch.setattr("app.api.routes.buyer.settings.WIREGUARD_ENDPOINT_PORT", 45182)
    monkeypatch.setattr("app.api.routes.buyer.settings.WIREGUARD_BUYER_INTERFACE", "wg-buyer")
    monkeypatch.setattr("app.api.routes.buyer.settings.WIREGUARD_NETWORK_CIDR", "10.66.66.0/24")
    monkeypatch.setattr("app.api.routes.buyer.settings.WIREGUARD_BUYER_NETWORK_CIDR", "10.66.66.128/25")
    monkeypatch.setattr(
        "app.api.routes.buyer.apply_server_peer",
        lambda settings, public_key, client_address, persistent_keepalive: {"ok": True},
    )

    create_response = client.post(
        "/api/v1/buyer/runtime-sessions",
        json={
            "seller_node_key": "wg-node-001",
            "session_mode": "shell",
            "runtime_image": "python:3.12-alpine",
        },
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert create_response.status_code == 200
    create_payload = create_response.json()
    session_id = create_payload["session_id"]

    redeem_response = client.post(
        "/api/v1/buyer/runtime-sessions/redeem",
        json={"connect_code": create_payload["connect_code"]},
    )
    assert redeem_response.status_code == 200
    assert redeem_response.json()["gateway_required"] is True

    handshake_response = client.post(
        f"/api/v1/buyer/runtime-sessions/{session_id}/gateway/handshake",
        json={"session_token": redeem_response.json()["session_token"]},
    )
    assert handshake_response.status_code == 200
    assert handshake_response.json()["gateway_host"] == "10.66.66.10"
    assert handshake_response.json()["gateway_port"] == create_payload["gateway_port"]

    bootstrap_response = client.post(
        f"/api/v1/buyer/runtime-sessions/{session_id}/wireguard/bootstrap",
        json={"client_public_key": "buyer-public-key"},
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert bootstrap_response.status_code == 200
    bundle = bootstrap_response.json()
    assert bundle["interface_name"] == "wg-buyer"
    assert bundle["seller_wireguard_target"] == "10.66.66.10"
    assert "10.66.66.10/32" in bundle["allowed_ips"]

    status_response = client.get(
        f"/api/v1/buyer/runtime-sessions/{session_id}",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert status_response.status_code == 200
    assert status_response.json()["network_mode"] == "wireguard"
    assert status_response.json()["buyer_wireguard_client_address"].endswith("/32")
    assert status_response.json()["seller_wireguard_target"] == "10.66.66.10"


def test_cleanup_expired_runtime_sessions_revokes_wireguard_peer(client: TestClient, monkeypatch) -> None:
    from datetime import datetime, timedelta

    from app.core.db import SessionLocal
    from app.models.platform import RuntimeAccessSession
    from app.services.runtime_sessions import cleanup_expired_runtime_sessions

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "seller-expire@example.com",
            "password": "super-secret-password",
            "display_name": "Expire Seller",
        },
    )
    seller_login = client.post(
        "/api/v1/auth/login",
        json={"email": "seller-expire@example.com", "password": "super-secret-password"},
    )
    seller_token = seller_login.json()["access_token"]
    node_token_response = client.post(
        "/api/v1/platform/node-registration-token",
        json={"label": "expire-node", "expires_hours": 48},
        headers={"Authorization": f"Bearer {seller_token}"},
    )
    node_token = node_token_response.json()["node_registration_token"]
    client.post(
        "/api/v1/platform/nodes/register",
        json={
            "node_id": "expire-node-001",
            "device_fingerprint": "device-expire-001",
            "hostname": "docker-desktop",
            "system": "Windows",
            "machine": "AMD64",
            "shared_percent_preference": 10,
            "capabilities": {
                "cpu_count_logical": 24,
                "interfaces": {
                    "wg-seller": [
                        {"family": 2, "address": "10.66.66.10"},
                    ]
                },
            },
            "seller_intent": "expire seller test",
            "docker_status": "ready",
            "swarm_state": "state=active node_id=ql6wifxs5vfs2d8ezr884pihx",
            "node_class": "cpu-basic",
        },
        headers={"Authorization": f"Bearer {node_token}"},
    )

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "buyer-expire@example.com",
            "password": "super-secret-password",
            "display_name": "Expire Buyer",
        },
    )
    buyer_login = client.post(
        "/api/v1/auth/login",
        json={"email": "buyer-expire@example.com", "password": "super-secret-password"},
    )
    buyer_token = buyer_login.json()["access_token"]

    monkeypatch.setattr("app.api.routes.buyer.create_runtime_session_bundle", _fake_bundle_create)
    monkeypatch.setattr(
        "app.api.routes.buyer.inspect_runtime_session_bundle",
        lambda settings, **kwargs: _running_bundle_inspect(),
    )
    removed_peers: list[str] = []
    monkeypatch.setattr("app.services.runtime_sessions.remove_runtime_session_bundle", lambda settings, **kwargs: {"ok": True})
    monkeypatch.setattr(
        "app.services.runtime_sessions.remove_server_peer",
        lambda settings, public_key: removed_peers.append(public_key) or {"ok": True},
    )

    create_response = client.post(
        "/api/v1/buyer/runtime-sessions",
        json={
            "seller_node_key": "expire-node-001",
            "session_mode": "shell",
            "runtime_image": "python:3.12-alpine",
        },
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["session_id"]

    db = SessionLocal()
    try:
        session = db.get(RuntimeAccessSession, session_id)
        assert session is not None
        session.status = "running"
        session.buyer_wireguard_public_key = "buyer-expire-public-key"
        session.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    expired_count = cleanup_expired_runtime_sessions()
    assert expired_count == 1
    assert removed_peers == ["buyer-expire-public-key"]

    db = SessionLocal()
    try:
        session = db.get(RuntimeAccessSession, session_id)
        assert session is not None
        assert session.status == "expired"
        assert session.ended_at is not None
    finally:
        db.close()
