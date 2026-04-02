from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx


def gateway_base_url(
    *,
    gateway_host: str,
    gateway_port: int | str,
    gateway_protocol: str = "http",
) -> str:
    protocol = (gateway_protocol or "http").strip().lower() or "http"
    host = str(gateway_host or "").strip()
    port = str(gateway_port or "").strip()
    if not host or not port:
        raise RuntimeError("gateway_target_missing")
    return f"{protocol}://{host}:{port}"


def gateway_shell_websocket_url(
    *,
    gateway_host: str,
    gateway_port: int | str,
    gateway_protocol: str = "http",
    rows: int | None = None,
    cols: int | None = None,
) -> str:
    base = gateway_base_url(
        gateway_host=gateway_host,
        gateway_port=gateway_port,
        gateway_protocol=gateway_protocol,
    )
    ws_protocol = "wss" if base.startswith("https://") else "ws"
    query: dict[str, int] = {}
    if rows is not None:
        query["rows"] = max(1, int(rows))
    if cols is not None:
        query["cols"] = max(1, int(cols))
    suffix = f"?{urlencode(query)}" if query else ""
    return base.replace("https://", f"{ws_protocol}://").replace("http://", f"{ws_protocol}://") + "/shell/ws" + suffix


def request_gateway_json(
    method: str,
    *,
    gateway_host: str,
    gateway_port: int | str,
    session_token: str,
    path: str,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    gateway_protocol: str = "http",
    timeout: float = 20.0,
) -> dict[str, Any]:
    base_url = gateway_base_url(
        gateway_host=gateway_host,
        gateway_port=gateway_port,
        gateway_protocol=gateway_protocol,
    )
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {session_token}",
    }
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                method.upper(),
                url,
                headers=headers,
                json=payload,
                params=params,
            )
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": None,
            "data": {"detail": str(exc)},
        }

    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}
    return {
        "ok": response.is_success,
        "status": response.status_code,
        "data": data,
    }


def _gateway_failure_detail(prefix: str, data: Any) -> str:
    if isinstance(data, dict):
        for key in ("detail", "error", "raw"):
            value = data.get(key)
            if value:
                return str(value)
    return f"{prefix}: {data}"


def gateway_exec_command(
    *,
    gateway_host: str,
    gateway_port: int | str,
    session_token: str,
    command: str,
    gateway_protocol: str = "http",
    timeout: float = 20.0,
) -> dict[str, Any]:
    response = request_gateway_json(
        "POST",
        gateway_host=gateway_host,
        gateway_port=gateway_port,
        session_token=session_token,
        path="/exec",
        payload={"command": command},
        gateway_protocol=gateway_protocol,
        timeout=timeout,
    )
    if not response["ok"]:
        raise RuntimeError(_gateway_failure_detail("gateway_exec_failed", response["data"]))
    return response["data"]


def gateway_read_logs(
    *,
    gateway_host: str,
    gateway_port: int | str,
    session_token: str,
    cursor: int = 0,
    limit: int = 200,
    gateway_protocol: str = "http",
    timeout: float = 20.0,
) -> dict[str, Any]:
    response = request_gateway_json(
        "GET",
        gateway_host=gateway_host,
        gateway_port=gateway_port,
        session_token=session_token,
        path="/logs",
        params={"cursor": max(0, int(cursor)), "limit": max(1, int(limit))},
        gateway_protocol=gateway_protocol,
        timeout=timeout,
    )
    if not response["ok"]:
        raise RuntimeError(_gateway_failure_detail("gateway_logs_failed", response["data"]))
    return response["data"]


def gateway_upload_archive(
    *,
    gateway_host: str,
    gateway_port: int | str,
    session_token: str,
    remote_path: str,
    archive_base64: str,
    gateway_protocol: str = "http",
    timeout: float = 60.0,
) -> dict[str, Any]:
    response = request_gateway_json(
        "POST",
        gateway_host=gateway_host,
        gateway_port=gateway_port,
        session_token=session_token,
        path="/files/upload",
        payload={"remote_path": remote_path, "archive_base64": archive_base64},
        gateway_protocol=gateway_protocol,
        timeout=timeout,
    )
    if not response["ok"]:
        raise RuntimeError(_gateway_failure_detail("gateway_upload_failed", response["data"]))
    return response["data"]


def gateway_download_archive(
    *,
    gateway_host: str,
    gateway_port: int | str,
    session_token: str,
    remote_path: str,
    gateway_protocol: str = "http",
    timeout: float = 60.0,
) -> dict[str, Any]:
    response = request_gateway_json(
        "GET",
        gateway_host=gateway_host,
        gateway_port=gateway_port,
        session_token=session_token,
        path="/files/download",
        params={"path": remote_path},
        gateway_protocol=gateway_protocol,
        timeout=timeout,
    )
    if not response["ok"]:
        raise RuntimeError(_gateway_failure_detail("gateway_download_failed", response["data"]))
    return response["data"]
