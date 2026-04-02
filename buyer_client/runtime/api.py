from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable


def _buyer_display_name(display_name: str | None) -> str:
    value = str(display_name or "").strip()
    return value or "Buyer Agent"


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "ok": True,
                "status": response.status,
                "data": json.loads(response.read().decode("utf-8")),
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return {"ok": False, "status": exc.code, "data": parsed}


def login_or_register(backend_url: str, email: str, password: str, display_name: str | None = None) -> dict[str, Any]:
    resolved_display_name = _buyer_display_name(display_name)
    register = request_json(
        "POST",
        f"{backend_url.rstrip('/')}/api/v1/auth/register",
        {"email": email, "password": password, "display_name": resolved_display_name},
    )
    if not register["ok"] and register["status"] != 409:
        raise RuntimeError(f"register_failed: {register['data']}")

    login = request_json(
        "POST",
        f"{backend_url.rstrip('/')}/api/v1/auth/login",
        {"email": email, "password": password},
    )
    if not login["ok"]:
        raise RuntimeError(f"login_failed: {login['data']}")
    return {
        "access_token": str(login["data"]["access_token"]),
        "user": login["data"]["user"],
        "register_result": register,
        "login_result": login,
    }


def redeem_connect_code(*, backend_url: str, connect_code: str) -> dict[str, Any]:
    redeem = request_json(
        "POST",
        f"{backend_url.rstrip('/')}/api/v1/buyer/runtime-sessions/redeem",
        {"connect_code": connect_code},
    )
    if not redeem["ok"]:
        raise RuntimeError(f"redeem_connect_code_failed: {redeem['data']}")
    return redeem["data"]


def redeem_order_license(*, backend_url: str, license_token: str) -> dict[str, Any]:
    redeem = request_json(
        "POST",
        f"{backend_url.rstrip('/')}/api/v1/buyer/orders/redeem",
        {"license_token": license_token},
    )
    if not redeem["ok"]:
        raise RuntimeError(f"redeem_order_license_failed: {redeem['data']}")
    return redeem["data"]


def start_order_runtime_session(
    *,
    backend_url: str,
    buyer_token: str,
    order_id: int,
) -> dict[str, Any]:
    response = request_json(
        "POST",
        f"{backend_url.rstrip('/')}/api/v1/buyer/orders/{order_id}/start-session",
        token=buyer_token,
        timeout=120,
    )
    if not response["ok"]:
        raise RuntimeError(f"start_order_runtime_session_failed: {response['data']}")
    return response["data"]


def fetch_codex_runtime_bootstrap(*, backend_url: str, buyer_token: str) -> dict[str, Any]:
    response = request_json(
        "GET",
        f"{backend_url.rstrip('/')}/api/v1/platform/runtime/codex",
        token=buyer_token,
        timeout=120,
    )
    if not response["ok"]:
        raise RuntimeError(f"fetch_codex_runtime_bootstrap_failed: {response['data']}")
    return response["data"]


def create_runtime_session(
    *,
    backend_url: str,
    email: str,
    password: str,
    display_name: str | None = None,
    seller_node_key: str = "",
    offer_id: int | None = None,
    code_filename: str,
    code_content: str,
    runtime_image: str = "python:3.12-alpine",
    requested_duration_minutes: int = 30,
    session_mode: str = "code_run",
    source_type: str = "inline_code",
    entry_command: list[str] | None = None,
    archive_filename: str | None = None,
    archive_content_base64: str = "",
    source_ref: str | None = None,
    working_dir: str | None = None,
    run_command: list[str] | None = None,
) -> dict[str, Any]:
    auth = login_or_register(backend_url, email, password, display_name=display_name)
    buyer_token = auth["access_token"]

    create = request_json(
        "POST",
        f"{backend_url.rstrip('/')}/api/v1/buyer/runtime-sessions",
        {
            "seller_node_key": seller_node_key or None,
            "offer_id": offer_id,
            "session_mode": session_mode,
            "source_type": source_type,
            "runtime_image": runtime_image,
            "code_filename": code_filename,
            "code_content": code_content,
            "archive_filename": archive_filename,
            "archive_content_base64": archive_content_base64,
            "source_ref": source_ref,
            "working_dir": working_dir,
            "run_command": run_command,
            "requested_duration_minutes": requested_duration_minutes,
            "entry_command": entry_command,
        },
        token=buyer_token,
        timeout=120,
    )
    if not create["ok"]:
        raise RuntimeError(f"create_runtime_session_failed: {create['data']}")

    connect_code = str(create["data"]["connect_code"])
    redeem = redeem_connect_code(
        backend_url=backend_url,
        connect_code=connect_code,
    )

    return {
        "backend_url": backend_url,
        "buyer_email": email,
        "buyer_token": buyer_token,
        "session_id": int(create["data"]["session_id"]),
        "offer_id": create["data"].get("offer_id"),
        "seller_node_key": str(create["data"]["seller_node_key"]),
        "runtime_image": str(create["data"]["runtime_image"]),
        "code_filename": code_filename,
        "session_mode": session_mode,
        "source_type": source_type,
        "connect_code": connect_code,
        "session_token": str(redeem["session_token"]),
        "relay_endpoint": str(redeem["relay_endpoint"]),
        "gateway_required": bool(redeem.get("gateway_required")),
        "gateway_protocol": str(redeem.get("gateway_protocol") or ""),
        "gateway_port": redeem.get("gateway_port"),
        "supported_features": [str(item) for item in (redeem.get("supported_features") or [])],
        "create_result": create,
        "redeem_result": {"data": redeem},
        "auth": auth,
    }


def start_licensed_shell_session(
    *,
    backend_url: str,
    email: str,
    password: str,
    display_name: str | None = None,
    license_token: str,
) -> dict[str, Any]:
    auth = login_or_register(backend_url, email, password, display_name=display_name)
    buyer_token = auth["access_token"]
    license_info = redeem_order_license(
        backend_url=backend_url,
        license_token=license_token,
    )
    start_result = start_order_runtime_session(
        backend_url=backend_url,
        buyer_token=buyer_token,
        order_id=int(license_info["order_id"]),
    )
    redeem = redeem_connect_code(
        backend_url=backend_url,
        connect_code=str(start_result["connect_code"]),
    )
    return {
        "backend_url": backend_url,
        "buyer_email": email,
        "buyer_token": buyer_token,
        "order_id": int(license_info["order_id"]),
        "offer_id": int(license_info["offer_id"]),
        "seller_node_key": str(license_info["seller_node_key"]),
        "runtime_image": str(license_info["runtime_image_ref"]),
        "license_token": license_token,
        "session_id": int(start_result["session_id"]),
        "session_mode": "shell",
        "source_type": "licensed_order",
        "connect_code": str(start_result["connect_code"]),
        "session_token": str(redeem["session_token"]),
        "relay_endpoint": str(redeem["relay_endpoint"]),
        "gateway_required": bool(redeem.get("gateway_required")),
        "gateway_protocol": str(redeem.get("gateway_protocol") or ""),
        "gateway_port": redeem.get("gateway_port"),
        "supported_features": [str(item) for item in (redeem.get("supported_features") or [])],
        "license_result": {"data": license_info},
        "start_result": {"data": start_result},
        "redeem_result": {"data": redeem},
        "auth": auth,
    }


def handshake_runtime_gateway(
    *,
    backend_url: str,
    session_id: int,
    session_token: str,
) -> dict[str, Any]:
    response = request_json(
        "POST",
        f"{backend_url.rstrip('/')}/api/v1/buyer/runtime-sessions/{session_id}/gateway/handshake",
        {"session_token": session_token},
        timeout=120,
    )
    if not response["ok"]:
        raise RuntimeError(f"gateway_handshake_failed: {response['data']}")
    return response["data"]


def read_runtime_session(*, backend_url: str, buyer_token: str, session_id: int) -> dict[str, Any]:
    response = request_json(
        "GET",
        f"{backend_url.rstrip('/')}/api/v1/buyer/runtime-sessions/{session_id}",
        token=buyer_token,
        timeout=120,
    )
    if not response["ok"]:
        raise RuntimeError(f"read_runtime_session_failed: {response['data']}")
    return response["data"]


def wait_for_runtime_completion(
    *,
    backend_url: str,
    buyer_token: str,
    session_id: int,
    poll_seconds: int = 2,
    timeout_seconds: int = 300,
    require_logs: bool = False,
    log_grace_polls: int = 5,
    on_update: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    terminal_without_logs_polls = 0
    while time.time() < deadline:
        payload = read_runtime_session(backend_url=backend_url, buyer_token=buyer_token, session_id=session_id)
        if on_update is not None:
            on_update(payload)
        if payload.get("status") in {"completed", "failed", "stopped"}:
            if require_logs and not (payload.get("logs") or ""):
                terminal_without_logs_polls += 1
                if terminal_without_logs_polls <= log_grace_polls:
                    time.sleep(poll_seconds)
                    continue
            return payload
        time.sleep(poll_seconds)
    raise RuntimeError("wait_for_runtime_completion_timeout")


def stop_runtime_session(*, backend_url: str, buyer_token: str, session_id: int) -> dict[str, Any]:
    response = request_json(
        "POST",
        f"{backend_url.rstrip('/')}/api/v1/buyer/runtime-sessions/{session_id}/stop",
        token=buyer_token,
        timeout=120,
    )
    if not response["ok"]:
        raise RuntimeError(f"stop_runtime_session_failed: {response['data']}")
    return response["data"]


def renew_runtime_session(*, backend_url: str, buyer_token: str, session_id: int, additional_minutes: int) -> dict[str, Any]:
    response = request_json(
        "POST",
        f"{backend_url.rstrip('/')}/api/v1/buyer/runtime-sessions/{session_id}/renew",
        {"additional_minutes": additional_minutes},
        token=buyer_token,
        timeout=120,
    )
    if not response["ok"]:
        raise RuntimeError(f"renew_runtime_session_failed: {response['data']}")
    return response["data"]


def stop_session(
    *,
    backend_url: str,
    email: str,
    password: str,
    session_id: int,
    display_name: str | None = None,
) -> dict[str, Any]:
    auth = login_or_register(backend_url, email, password, display_name=display_name)
    return stop_runtime_session(
        backend_url=backend_url,
        buyer_token=auth["access_token"],
        session_id=session_id,
    )
