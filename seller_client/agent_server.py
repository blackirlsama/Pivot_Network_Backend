from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seller_client.agent_mcp import (
    _backend_url,
    _default_node_key,
    _ensure_client_dirs,
    _load_client_config,
    _run_backend_request,
    bootstrap_wireguard_from_platform,
    configure_registry_trust,
    docker_summary,
    environment_check,
    explain_seller_intent,
    ensure_joined_to_platform_swarm,
    fetch_codex_runtime_bootstrap,
    get_client_config,
    list_uploaded_images,
    onboard_seller_from_intent,
    push_and_report_image,
    swarm_summary,
    wireguard_summary,
)
from seller_client.installer import bootstrap_client
from seller_client.installer import buyer_codex_server_name, codex_server_name, mcp_server_attachment_status
from seller_client.windows_elevation import is_windows_platform

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEB_STATE_DIR = str(REPO_ROOT / ".cache" / "seller-web")
INDEX_HTML = REPO_ROOT / "seller_client" / "web" / "index.html"


class InstallerRequest(BaseModel):
    apply: bool = False
    state_dir: str | None = None


class IntentRequest(BaseModel):
    intent: str
    state_dir: str | None = None


class OnboardingRequest(BaseModel):
    intent: str
    email: str
    password: str
    display_name: str | None = None
    backend_url: str | None = None
    state_dir: str | None = None


class RegistryTrustRequest(BaseModel):
    registry: str
    restart_docker: bool = True
    state_dir: str | None = None


class RuntimeBootstrapRequest(BaseModel):
    backend_url: str | None = None
    state_dir: str | None = None


class PushImageRequest(BaseModel):
    local_tag: str
    repository: str
    remote_tag: str = "latest"
    registry: str
    backend_url: str | None = None
    state_dir: str | None = None


def _state_dir(provided: str | None) -> str:
    return provided or DEFAULT_WEB_STATE_DIR


def _state_dir_path(provided: str | None) -> Path:
    return Path(_state_dir(provided)).expanduser().resolve()


def _parse_json_body(body: str | None) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _platform_request(
    state_dir: str,
    path: str,
    *,
    backend_url: str | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 20,
) -> dict[str, Any]:
    base_dir = _state_dir_path(state_dir)
    config = _load_client_config(base_dir)
    access_token = config.get("auth", {}).get("access_token") or ""
    resolved_backend_url = _backend_url(backend_url, base_dir)
    if not access_token:
        return {
            "ok": False,
            "error": "missing_access_token",
            "backend_url": resolved_backend_url,
            "path": path,
        }

    response = _run_backend_request(
        method,
        path,
        backend_url=resolved_backend_url,
        bearer_token=access_token,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    response["data"] = _parse_json_body(response.get("body"))
    return response


def _matching_offer(offers: Any, repository: str, tag: str) -> dict[str, Any] | None:
    if not isinstance(offers, list):
        return None
    candidates = [
        item
        for item in offers
        if isinstance(item, dict) and item.get("repository") == repository and item.get("tag") == tag
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: int(item.get("id") or 0))


def _matching_platform_events(events: Any, repository: str, tag: str) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []

    matched: list[dict[str, Any]] = []
    image_ref = f"{repository}:{tag}"
    for event in events:
        if not isinstance(event, dict):
            continue
        metadata = event.get("event_metadata") or {}
        summary = str(event.get("summary") or "")
        if metadata.get("repository") == repository and metadata.get("tag") == tag:
            matched.append(event)
            continue
        if image_ref in summary:
            matched.append(event)
    return matched[:5]


def _offer_state_detail(offer: dict[str, Any]) -> str:
    status = offer.get("offer_status") or "unknown"
    probe_status = offer.get("probe_status") or "unknown"
    pricing_error = offer.get("pricing_error")
    price = offer.get("current_billable_price_cny_per_hour")
    parts = [f"offer_status={status}", f"probe_status={probe_status}"]
    if price is not None:
        parts.append(f"price_cny_per_hour={price}")
    if pricing_error:
        parts.append(f"pricing_error={pricing_error}")
    return " | ".join(parts)


def _http_result_detail(result: dict[str, Any] | None, fallback: str) -> str:
    payload = result or {}
    parsed_body = payload.get("data")
    if parsed_body is None:
        parsed_body = _parse_json_body(payload.get("body"))

    message: Any = None
    if isinstance(parsed_body, dict):
        message = parsed_body.get("detail") or parsed_body.get("error") or parsed_body.get("message")
    if message is None:
        message = payload.get("detail") or payload.get("error") or payload.get("stderr") or payload.get("body")
    if message is None:
        message = fallback

    prefix: list[str] = []
    if payload.get("status") is not None:
        prefix.append(f"HTTP {payload['status']}")
    if payload.get("url"):
        prefix.append(str(payload["url"]))
    if prefix:
        return f"{' | '.join(prefix)} | {message}"
    return str(message)


def _platform_snapshot(state_dir: str) -> dict[str, Any]:
    base_dir = _state_dir_path(state_dir)
    config = _load_client_config(base_dir)
    access_token = config.get("auth", {}).get("access_token") or ""
    backend_url = _backend_url(None, base_dir)
    snapshot: dict[str, Any] = {
        "ok": False,
        "backend_url": backend_url,
        "overview": None,
        "activity": None,
        "swarm": None,
        "image_offers": None,
    }
    if not access_token:
        snapshot["error"] = "missing_access_token"
        return snapshot

    overview = _platform_request(state_dir, "/api/v1/platform/overview", backend_url=backend_url)
    activity = _platform_request(state_dir, "/api/v1/platform/activity", backend_url=backend_url)
    swarm = _platform_request(state_dir, "/api/v1/platform/swarm/overview", backend_url=backend_url)
    image_offers = _platform_request(state_dir, "/api/v1/platform/image-offers", backend_url=backend_url)
    snapshot["overview"] = overview.get("data")
    snapshot["activity"] = activity.get("data")
    snapshot["swarm"] = swarm.get("data")
    snapshot["image_offers"] = image_offers.get("data")
    snapshot["ok"] = bool(overview.get("ok") and activity.get("ok"))
    if not snapshot["ok"]:
        snapshot["overview_error"] = overview if not overview.get("ok") else None
        snapshot["activity_error"] = activity if not activity.get("ok") else None
    if not swarm.get("ok"):
        snapshot["swarm_error"] = swarm
    if not image_offers.get("ok"):
        snapshot["image_offers_error"] = image_offers
    return snapshot


def _local_activity_path(state_dir: str | None) -> Path:
    base_dir = _state_dir_path(state_dir)
    logs_dir = Path(_ensure_client_dirs(base_dir)["logs_dir"])
    return logs_dir / "local-web-actions.jsonl"


def _read_local_activity(state_dir: str | None, limit: int = 20) -> list[dict[str, Any]]:
    path = _local_activity_path(state_dir)
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(entries) >= limit:
            break
    return entries


def _append_local_activity(
    state_dir: str | None,
    action: str,
    status: str,
    title: str,
    summary: str,
    stages: list[dict[str, Any]],
    result: dict[str, Any],
) -> dict[str, Any]:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "status": status,
        "title": title,
        "summary": summary,
        "stages": stages,
        "result": result,
    }
    path = _local_activity_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return entry


def _status_rank(status: str) -> int:
    ranks = {"error": 3, "warning": 2, "success": 1, "info": 0}
    return ranks.get(status, 0)


def _combine_status(*statuses: str) -> str:
    return max(statuses, key=_status_rank, default="info")


def _stage(stage_id: str, label: str, status: str, detail: str) -> dict[str, str]:
    return {"id": stage_id, "label": label, "status": status, "detail": detail}


def _stage_from_result(stage_id: str, label: str, result: dict[str, Any] | None, success_detail: str) -> dict[str, str]:
    payload = result or {}
    if payload.get("ok"):
        return _stage(stage_id, label, "success", success_detail)

    detail = (
        payload.get("detail")
        or payload.get("error")
        or payload.get("stderr")
        or payload.get("body")
        or "操作未完成。"
    )
    return _stage(stage_id, label, "error", str(detail))


def _onboarding_environment_stage(result: dict[str, Any]) -> dict[str, str]:
    environment = result.get("environment", {})
    missing: list[str] = []
    if not environment.get("codex_cli"):
        missing.append("CodeX CLI")
    if not environment.get("docker_cli"):
        missing.append("Docker CLI")
    if not (environment.get("wireguard_cli") or environment.get("wireguard_windows_exe")):
        missing.append("WireGuard")

    if missing:
        return _stage(
            "environment",
            "检查本机环境",
            "warning",
            f"已完成环境探测，但仍缺少：{', '.join(missing)}。",
        )

    return _stage(
        "environment",
        "检查本机环境",
        "success",
        f"平台={environment.get('platform') or 'unknown'}，基础依赖入口可见。",
    )


def _onboarding_docker_stage(result: dict[str, Any]) -> dict[str, str]:
    docker = result.get("docker", {})
    if docker.get("ok"):
        action = docker.get("action")
        if action == "already_running":
            detail = "Docker 已在本机运行。"
        else:
            detail = "Docker 已尝试拉起并通过探测。"
        return _stage("docker", "确认 Docker 运行时", "success", detail)

    detail = docker.get("error") or docker.get("docker_info", {}).get("stderr") or "Docker 当前不可用。"
    return _stage("docker", "确认 Docker 运行时", "warning", str(detail))


def _codex_runtime_stage(result: dict[str, Any] | None) -> dict[str, str]:
    payload = result or {}
    if payload.get("ok"):
        data = payload.get("data", {})
        provider = data.get("provider", {}).get("name", "unknown")
        model = data.get("model", "unknown")
        return _stage("codex_runtime", "获取平台 CodeX runtime", "success", f"后端已提供 {provider} / {model} runtime 配置。")
    detail = payload.get("detail") or payload.get("error") or payload.get("body") or "后端还没有准备好 CodeX runtime。"
    return _stage("codex_runtime", "获取平台 CodeX runtime", "warning", str(detail))


def _wireguard_bootstrap_stages(result: dict[str, Any]) -> list[dict[str, str]]:
    stages: list[dict[str, str]] = []
    if result.get("keypair_result") or result.get("stage") == "keypair":
        keypair_result = result.get("keypair_result")
        if keypair_result and keypair_result.get("ok"):
            stages.append(_stage("wg_keypair", "生成本地 WireGuard keypair", "success", "本机已生成 WireGuard 公私钥。"))
        else:
            stages.append(
                _stage(
                    "wg_keypair",
                    "生成本地 WireGuard keypair",
                    "error",
                    str((keypair_result or {}).get("error") or "本机当前无法生成 WireGuard keypair。"),
                )
            )

    if result.get("bootstrap_result") or result.get("stage") == "backend_bootstrap":
        bootstrap_result = result.get("bootstrap_result")
        if bootstrap_result and bootstrap_result.get("ok"):
            activation_mode = bootstrap_result.get("data", {}).get("activation_mode", "profile_only")
            stages.append(
                _stage(
                    "wg_backend",
                    "向后端申请 WireGuard bootstrap",
                    "success",
                    f"后端已签发 WireGuard profile 参数，模式={activation_mode}。",
                )
            )
        else:
            stages.append(
                _stage(
                    "wg_backend",
                    "向后端申请 WireGuard bootstrap",
                    "error",
                    str((bootstrap_result or {}).get("detail") or (bootstrap_result or {}).get("error") or (bootstrap_result or {}).get("body") or "后端没有返回 WireGuard bootstrap。"),
                )
            )

    if result.get("profile_result") or result.get("stage") == "prepare_profile":
        stages.append(
            _stage_from_result(
                "wg_profile",
                "写入本地 WireGuard profile",
                result.get("profile_result"),
                "本地 WireGuard 配置文件已写入 seller 状态目录。",
            )
        )
    if result.get("activation_result") is not None:
        activation_result = result.get("activation_result")
        if activation_result.get("ok"):
            stages.append(_stage("wg_activate", "激活本地 WireGuard 隧道", "success", "本地 WireGuard 隧道已尝试拉起。"))
        else:
            stages.append(
                _stage(
                    "wg_activate",
                    "激活本地 WireGuard 隧道",
                    "warning",
                    str(activation_result.get("error") or activation_result.get("stderr") or "本地 profile 已准备，但隧道还没有成功激活。"),
                )
            )

    return stages


def _onboarding_stages(result: dict[str, Any]) -> list[dict[str, str]]:
    stages = [
        _stage(
            "intent",
            "理解卖家意图",
            "success",
            result.get("explanation", {}).get("explanation", "已解析卖家接入意图。"),
        ),
        _stage_from_result(
            "workspace",
            "写入本地工作配置",
            result.get("configure_result"),
            "seller-Agent 本地工作目录与服务端入口已写入配置。",
        ),
        _onboarding_environment_stage(result),
        _onboarding_docker_stage(result),
    ]

    register_result = result.get("register_result")
    if register_result:
        if register_result.get("ok"):
            stages.append(_stage("register", "注册卖家账号", "success", "平台卖家账号已创建。"))
        elif register_result.get("status") == 409:
            stages.append(_stage("register", "注册卖家账号", "info", "卖家账号已存在，继续登录。"))
        else:
            stages.append(
                _stage(
                    "register",
                    "注册卖家账号",
                    "error",
                    str(register_result.get("detail") or register_result.get("body") or register_result.get("error")),
                )
            )

    if result.get("login_result") or result.get("stage") == "login":
        stages.append(
            _stage_from_result(
                "login",
                "登录平台并换取访问令牌",
                result.get("login_result"),
                "卖家访问令牌已写入本地配置。",
            )
        )

    if result.get("node_token_result") or result.get("stage") == "issue_node_token":
        stages.append(
            _stage_from_result(
                "node_token",
                "签发节点令牌",
                result.get("node_token_result"),
                "节点注册令牌已签发并缓存。",
            )
        )

    if result.get("codex_runtime_result") or result.get("stage") == "codex_runtime":
        stages.append(_codex_runtime_stage(result.get("codex_runtime_result")))

    if result.get("register_node_result") or result.get("stage") == "register_node":
        stages.append(
            _stage_from_result(
                "register_node",
                "注册节点到平台",
                result.get("register_node_result"),
                "平台已记录本机节点、共享偏好和能力摘要。",
            )
        )

    if result.get("wireguard_result") or result.get("stage") == "wireguard_bootstrap":
        stages.extend(_wireguard_bootstrap_stages(result.get("wireguard_result") or result))

    if result.get("heartbeat_result"):
        stages.append(
            _stage_from_result(
                "heartbeat",
                "发送节点心跳",
                result.get("heartbeat_result"),
                "节点状态已上报为 available。",
            )
        )

    return stages


def _installer_stages(result: dict[str, Any]) -> list[dict[str, str]]:
    environment = result.get("environment", {})
    attach_result = result.get("attach_result", {})
    codex_mcp_servers = dict(result.get("codex_mcp_servers") or {})
    helper_result = result.get("windows_wireguard_helper", {})
    stages = [
        _stage(
            "workspace",
            "初始化本地工作目录",
            "success" if result.get("ok") else "error",
            f"状态目录：{result.get('state_dir')}",
        ),
        _stage(
            "codex_mcp",
            "挂载本机 CodeX MCP 配置",
            (
                "success"
                if attach_result.get("ok") and not result.get("needs_codex_mcp_attach")
                else "warning"
            ),
            (
                f"配置文件：{attach_result.get('config_path') or result.get('codex_config_path')} | "
                f"{codex_server_name()}={bool(codex_mcp_servers.get(codex_server_name()))} | "
                f"{buyer_codex_server_name()}={bool(codex_mcp_servers.get(buyer_codex_server_name()))}"
            ),
        ),
    ]

    docker_ready = bool(environment.get("docker_cli"))
    stages.append(
        _stage(
            "docker",
            "探测 Docker 入口",
            "success" if docker_ready else "warning",
            "检测到 Docker CLI。" if docker_ready else "尚未检测到 Docker CLI。",
        )
    )

    wireguard_ready = bool(environment.get("wireguard_cli") or environment.get("wireguard_windows_exe"))
    stages.append(
        _stage(
            "wireguard",
            "探测 WireGuard 入口",
            "success" if wireguard_ready else "warning",
            "检测到 WireGuard 入口。" if wireguard_ready else "尚未检测到 WireGuard。",
        )
    )
    if environment.get("platform") == "Windows":
        stages.append(
            _stage(
                "wireguard_helper",
                "准备 WireGuard elevated helper",
                "success" if helper_result.get("ok") and not result.get("needs_windows_wireguard_helper") else "warning",
                (
                    f"已检测到 helper task：{helper_result.get('task_name')}"
                    if helper_result.get("ok") and not result.get("needs_windows_wireguard_helper")
                    else f"需要管理员执行：{result.get('windows_apply_command') or 'install_windows.ps1 -Apply'}"
                ),
            )
        )
    return stages


def _registry_trust_stages(result: dict[str, Any], registry: str) -> list[dict[str, str]]:
    certificate_result = result.get("fetch_result")
    registry_probe_result = result.get("probe_result")
    stages: list[dict[str, str]] = []

    if certificate_result and certificate_result.get("ok"):
        trust_probe = certificate_result.get("trust_probe") or {}
        issuer = trust_probe.get("issuer") or ()
        issuer_text = str(issuer) if issuer else "system CA"
        stages.append(
            _stage(
                "certificate",
                "检查 Registry 证书",
                "success",
                f"已确认 {registry} 走公开 HTTPS 证书链，issuer={issuer_text}",
            )
        )
    else:
        stages.append(
            _stage_from_result(
                "certificate",
                "检查 Registry 证书",
                certificate_result,
                f"{registry} 的 HTTPS 证书可被系统信任。",
            )
        )

    stages.append(
        _stage_from_result(
            "registry_v2",
            "检查 Registry /v2/ 连通性",
            registry_probe_result,
            f"{registry} 的 /v2/ 入口可正常访问。",
        )
    )
    return stages


def _push_image_stages(result: dict[str, Any], repository: str, remote_tag: str) -> list[dict[str, str]]:
    push_result = result.get("push_result", {})
    report_result = result.get("report_result", {})
    offer_state = result.get("offer_state")
    platform_events = result.get("platform_events") or []
    stages: list[dict[str, str]] = []

    if report_result and not report_result.get("ok"):
        stages.append(
            _stage(
                "report_error",
                "HTTP Error Detail",
                "error",
                _http_result_detail(report_result, "Platform image report failed with an unknown backend error."),
            )
        )

    if offer_state:
        offer_status = str(offer_state.get("offer_status") or "")
        stage_status = "success" if offer_status == "active" else "warning" if report_result.get("ok") else "error"
        stages.append(
            _stage(
                "offer_state",
                "Platform Offer State",
                stage_status,
                _offer_state_detail(offer_state),
            )
        )

    if platform_events:
        latest_event = platform_events[0]
        stages.append(
            _stage(
                "platform_event",
                "Latest Platform Event",
                "info",
                f"{latest_event.get('event_type') or 'unknown'} | {latest_event.get('summary') or ''}",
            )
        )


    tag_payload = push_result.get("tag_result")
    if tag_payload:
        stages.append(
            _stage_from_result(
                "tag_image",
                "重标记本地镜像",
                tag_payload,
                f"镜像已准备推送为 {tag_payload.get('remote_ref') or repository + ':' + remote_tag}。",
            )
        )

    if push_result:
        stages.append(
            _stage_from_result(
                "push_image",
                "推送镜像到 Registry",
                push_result.get("push_result"),
                f"镜像 {repository}:{remote_tag} 已推送。",
            )
        )

    if report_result:
        stages.append(
            _stage_from_result(
                "report_image",
                "向平台登记并自动上架镜像",
                report_result,
                f"平台已记录镜像 {repository}:{remote_tag}，并继续自动发布 buyer 可见商品。",
            )
        )

    if result.get("stage") == "push" and not stages:
        stages.append(_stage("push_image", "推送镜像到 Registry", "error", "镜像推送未完成。"))
    if result.get("stage") == "report" and not report_result:
        stages.append(_stage("report_image", "向平台登记并自动上架镜像", "error", "镜像登记或自动上架未完成。"))
    return stages


def _make_next_steps(readiness: list[dict[str, str]], platform: dict[str, Any]) -> list[str]:
    next_steps: list[str] = []
    for item in readiness:
        if item["status"] == "warning":
            next_steps.append(item["hint"])
    overview = platform.get("overview") or {}
    if isinstance(overview, dict) and overview.get("node_count", 0) > 0 and overview.get("image_count", 0) == 0:
        next_steps.append("节点已进入平台视图，可以开始推送第一个可出租镜像。")
    if not next_steps:
        next_steps.append("本地 seller 控制面已经就绪，可以继续做 WireGuard 接入和镜像上架。")
    return next_steps[:4]


def _readiness_checks(state_dir: str, platform: dict[str, Any]) -> list[dict[str, str]]:
    base_dir = _state_dir_path(state_dir)
    config = _load_client_config(base_dir)
    environment = environment_check()
    codex_mcp_servers = mcp_server_attachment_status()
    docker = docker_summary()
    swarm = swarm_summary()
    wireguard = wireguard_summary(state_dir=str(base_dir))
    overview = platform.get("overview") or {}
    nodes = overview.get("nodes", []) if isinstance(overview, dict) else []
    current_node_key = _default_node_key(base_dir)
    current_node = next((item for item in nodes if item.get("node_key") == current_node_key), None)
    if current_node is None and nodes:
        current_node = nodes[0]
    platform_wireguard_ready = bool((current_node or {}).get("wireguard_ready_for_buyer"))
    platform_wireguard_target = str((current_node or {}).get("wireguard_target") or "")

    checks = [
        {
            "id": "codex",
            "label": "CodeX CLI",
            "status": "success" if environment.get("codex_cli") else "warning",
            "detail": "检测到 CodeX CLI。" if environment.get("codex_cli") else "还没有检测到 CodeX CLI。",
            "hint": "先安装 CodeX CLI，再通过 environment_check 把 MCP 配置挂好。",
        },
        {
            "id": "codex_mcp",
            "label": "CodeX MCP 配置",
            "status": "success" if all(codex_mcp_servers.values()) else "warning",
            "detail": (
                f"{codex_server_name()}={bool(codex_mcp_servers.get(codex_server_name()))}，"
                f"{buyer_codex_server_name()}={bool(codex_mcp_servers.get(buyer_codex_server_name()))}。"
            ),
            "hint": "用 environment_check/install_windows.ps1 -Apply 统一挂载 sellerNodeAgent 和 buyerRuntimeAgent；seller 侧只使用 sellerNodeAgent 做接入和上架流程。",
        },
        {
            "id": "codex_runtime",
            "label": "卖家 CodeX runtime",
            "status": "success" if config.get("runtime", {}).get("codex_runtime_ready") else "warning",
            "detail": (
                f"后端已准备 {config.get('runtime', {}).get('codex_provider') or 'CodeX'} runtime。"
                if config.get("runtime", {}).get("codex_runtime_ready")
                else "还没有从后端成功获取 CodeX runtime。"
            ),
            "hint": "先登录平台，再从后端拉取卖家侧 CodeX runtime bootstrap，用它执行自然语言接入、镜像上架和节点管理。",
        },
        {
            "id": "docker_cli",
            "label": "Docker CLI",
            "status": "success" if environment.get("docker_cli") else "warning",
            "detail": "检测到 Docker CLI。" if environment.get("docker_cli") else "本机还没有 Docker CLI。",
            "hint": "安装 Docker Desktop 或 Docker Engine，并让 seller-Agent 能调用 docker 命令。",
        },
        {
            "id": "docker_engine",
            "label": "Docker 运行时",
            "status": "success" if docker.get("ok") else "warning",
            "detail": (
                docker.get("info", {}).get("stdout")
                if docker.get("ok")
                else str(docker.get("error") or docker.get("info", {}).get("stderr") or "Docker 当前未就绪。")
            ),
            "hint": "先把 Docker 守护进程拉起来，再做节点注册和镜像推送。",
        },
        {
            "id": "wireguard",
            "label": "WireGuard 入口",
            "status": "success" if (environment.get("wireguard_cli") or environment.get("wireguard_windows_exe")) else "warning",
            "detail": (
                "检测到 WireGuard 入口。"
                if (environment.get("wireguard_cli") or environment.get("wireguard_windows_exe"))
                else "还没有检测到 WireGuard CLI 或 Windows 客户端。"
            ),
            "hint": "准备 WireGuard 运行时，后续节点才能进入平台内网。",
        },
        {
            "id": "wireguard_helper",
            "label": "WireGuard elevated helper",
            "status": (
                "success"
                if wireguard.get("wireguard_elevated_helper_installed") or not is_windows_platform()
                else "warning"
            ),
            "detail": (
                f"已检测到受权 helper task：{wireguard.get('wireguard_elevated_helper_task')}"
                if wireguard.get("wireguard_elevated_helper_installed")
                else "当前还没有安装 Windows 受权 helper task。"
            ),
            "hint": "先用安装器 apply 一次，把 Windows 受权 helper task 注册好，普通权限下才能激活 WireGuard 隧道。",
        },
        {
            "id": "wireguard_profile",
            "label": "WireGuard profile",
            "status": (
                "success"
                if config.get("runtime", {}).get("wireguard_profile_status") in {"prepared", "active"}
                else "warning"
            ),
            "detail": (
                (
                    f"本地 WireGuard 已激活，地址={wireguard.get('client_address') or 'unknown'}。"
                    if config.get("runtime", {}).get("wireguard_profile_status") == "active"
                    else f"本地 profile 已准备，地址={wireguard.get('client_address') or 'unknown'}。"
                )
                if config.get("runtime", {}).get("wireguard_profile_status") in {"prepared", "active"}
                else (
                    f"本地 profile 已写入，但激活失败，地址={wireguard.get('client_address') or 'unknown'}。"
                    if config.get("runtime", {}).get("wireguard_profile_status") == "activation_failed"
                    else "还没有从后端拿到并写入 WireGuard profile。"
                )
            ),
            "hint": "注册节点后执行 WireGuard bootstrap，把平台下发的 profile 写到本地。",
        },
        {
            "id": "platform_auth",
            "label": "平台登录态",
            "status": "success" if config.get("auth", {}).get("access_token") else "warning",
            "detail": (
                f"当前卖家：{config.get('auth', {}).get('seller_email') or 'unknown'}。"
                if config.get("auth", {}).get("access_token")
                else "本地还没有 access token。"
            ),
            "hint": "先在本地网页完成一次登录或接入流程，换取 access token。",
        },
        {
            "id": "node_token",
            "label": "节点令牌",
            "status": "success" if config.get("auth", {}).get("node_registration_token") else "warning",
            "detail": (
                "节点注册令牌已缓存。"
                if config.get("auth", {}).get("node_registration_token")
                else "还没有 node registration token。"
            ),
            "hint": "登录平台后签发节点令牌，seller-Agent 才能注册本机节点。",
        },
        {
            "id": "platform_node",
            "label": "平台节点记录",
            "status": "success" if nodes else "warning",
            "detail": "平台已经看见本机节点。" if nodes else "平台侧还没有看到本机节点。",
            "hint": "执行一次 seller onboarding，让平台记录节点、共享偏好和能力摘要。",
        },
        {
            "id": "platform_wireguard_ready",
            "label": "Buyer WireGuard target",
            "status": "success" if platform_wireguard_ready else "warning",
            "detail": (
                f"平台已经确认 buyer 可用 WireGuard 目标：{platform_wireguard_target}。"
                if platform_wireguard_ready
                else "平台还没有看到可用的 wg-seller IPv4，buyer connect 还不会就绪。"
            ),
            "hint": "确保 seller 节点在 register/heartbeat 上报 interfaces.wg-seller IPv4，再刷新平台 readiness。",
        },
        {
            "id": "swarm",
            "label": "Swarm 状态",
            "status": "success" if swarm.get("info", {}).get("ok") else "warning",
            "detail": (
                swarm.get("info", {}).get("stdout")
                if swarm.get("info", {}).get("ok")
                else str(swarm.get("info", {}).get("stderr") or swarm.get("error") or "Swarm 状态未知。")
            ),
            "hint": "后续接入 manager 后，再把节点正式加入 Docker Swarm。",
        },
    ]
    return checks


def _dashboard_payload(state_dir: str) -> dict[str, Any]:
    base_dir = _state_dir_path(state_dir)
    config = get_client_config(state_dir=str(base_dir))
    environment = environment_check()
    docker = docker_summary()
    swarm = swarm_summary()
    wireguard = wireguard_summary(state_dir=str(base_dir))
    registry = list_uploaded_images(state_dir=str(base_dir))
    platform = _platform_snapshot(str(base_dir))
    readiness = _readiness_checks(str(base_dir), platform)
    overview = platform.get("overview") or {}
    nodes = overview.get("nodes", []) if isinstance(overview, dict) else []
    images = overview.get("images", []) if isinstance(overview, dict) else []
    summary_status = _combine_status(*(item["status"] for item in readiness))
    return {
        "ok": True,
        "state_dir": str(base_dir),
        "summary": {
            "status": summary_status,
            "seller_email": config["data"]["auth"].get("seller_email") or "",
            "backend_url": config["data"]["server"].get("backend_url") or "",
            "registry": config["data"]["server"].get("registry") or "",
            "node_id": _default_node_key(base_dir),
            "node_count": len(nodes),
            "image_count": len(images),
            "last_pushed_image": config["data"]["docker"].get("last_pushed_image") or "",
            "next_steps": _make_next_steps(readiness, platform),
        },
        "readiness": readiness,
        "local": {
            "environment": environment,
            "docker": docker,
            "swarm": swarm,
            "wireguard": wireguard,
            "config": config,
            "registry": registry,
        },
        "platform": platform,
        "local_activity": _read_local_activity(str(base_dir)),
    }


def _operation_payload(
    state_dir: str,
    action: str,
    title: str,
    stages: list[dict[str, str]],
    result: dict[str, Any],
    success_summary: str,
    failure_summary: str,
) -> dict[str, Any]:
    derived_status = _combine_status(*(stage["status"] for stage in stages))
    if result.get("ok"):
        status = "success" if derived_status in {"success", "info"} else derived_status
        summary = success_summary
    else:
        status = "error" if derived_status in {"info", "success"} else derived_status
        summary = failure_summary

    activity_entry = _append_local_activity(
        state_dir=state_dir,
        action=action,
        status=status,
        title=title,
        summary=summary,
        stages=stages,
        result=result,
    )
    return {
        "ok": result.get("ok", False),
        "action": action,
        "status": status,
        "title": title,
        "summary": summary,
        "stages": stages,
        "result": result,
        "activity_entry": activity_entry,
    }


app = FastAPI(title="Pivot Seller Local Web")


@app.get("/", response_class=HTMLResponse)
def read_index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/api/health")
def read_health() -> dict[str, Any]:
    return {"status": "ok", "service": "seller-agent-web"}


@app.get("/api/dashboard")
def read_dashboard(state_dir: str | None = None) -> dict[str, Any]:
    return _dashboard_payload(_state_dir(state_dir))


@app.get("/api/status")
def read_status(state_dir: str | None = None) -> dict[str, Any]:
    return _dashboard_payload(_state_dir(state_dir))


@app.get("/api/local-activity")
def read_local_activity(state_dir: str | None = None, limit: int = 20) -> dict[str, Any]:
    resolved_state_dir = _state_dir(state_dir)
    return {"ok": True, "state_dir": resolved_state_dir, "items": _read_local_activity(resolved_state_dir, limit=limit)}


@app.post("/api/intents/explain")
def run_intent_explain(payload: IntentRequest) -> JSONResponse:
    result = explain_seller_intent(payload.intent)
    next_steps = [
        "先确认本机 Docker、WireGuard 和 CodeX 入口是否可见。",
        "登录平台并签发节点令牌。",
        "执行 seller onboarding，把共享偏好和节点能力注册到平台。",
    ]
    return JSONResponse({"ok": True, "result": result, "next_steps": next_steps})


@app.post("/api/installer")
def run_installer(payload: InstallerRequest) -> JSONResponse:
    state_dir = _state_dir(payload.state_dir)
    result = bootstrap_client(dry_run=not payload.apply, state_dir=state_dir)
    response = _operation_payload(
        state_dir=state_dir,
        action="installer",
        title="安装器检查",
        stages=_installer_stages(result),
        result=result,
        success_summary="本地安装器检查已完成，工作目录、sellerNodeAgent 和 buyerRuntimeAgent 的 CodeX MCP 挂载位已准备。",
        failure_summary="安装器检查未完成，需要先修复本机依赖入口。",
    )
    return JSONResponse(response)


@app.post("/api/runtime/codex")
def run_codex_runtime_bootstrap(payload: RuntimeBootstrapRequest) -> JSONResponse:
    state_dir = _state_dir(payload.state_dir)
    result = fetch_codex_runtime_bootstrap(
        backend_url=payload.backend_url,
        state_dir=state_dir,
        mask_secret=True,
    )
    response = _operation_payload(
        state_dir=state_dir,
        action="codex_runtime",
        title="平台 CodeX runtime",
        stages=[_codex_runtime_stage(result)],
        result=result,
        success_summary="后端已向 seller-Agent 提供 CodeX runtime 配置。",
        failure_summary="后端还没有成功提供 CodeX runtime 配置。",
    )
    return JSONResponse(response)


@app.post("/api/onboarding")
def run_onboarding(payload: OnboardingRequest) -> JSONResponse:
    state_dir = _state_dir(payload.state_dir)
    result = onboard_seller_from_intent(
        intent=payload.intent,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        backend_url=payload.backend_url,
        state_dir=state_dir,
    )
    response = _operation_payload(
        state_dir=state_dir,
        action="onboarding",
        title="卖家节点接入",
        stages=_onboarding_stages(result),
        result=result,
        success_summary="卖家节点接入流程已经执行，平台应该能看到节点和共享意向。",
        failure_summary="卖家节点接入中途停止，需要按失败阶段继续补齐。",
    )
    return JSONResponse(response)


@app.post("/api/wireguard/bootstrap")
def run_wireguard_bootstrap(payload: RuntimeBootstrapRequest) -> JSONResponse:
    state_dir = _state_dir(payload.state_dir)
    result = bootstrap_wireguard_from_platform(
        backend_url=payload.backend_url,
        state_dir=state_dir,
    )
    response = _operation_payload(
        state_dir=state_dir,
        action="wireguard_bootstrap",
        title="WireGuard profile bootstrap",
        stages=_wireguard_bootstrap_stages(result),
        result=result,
        success_summary="后端 WireGuard bootstrap 已拉取，本地 profile 已写入。",
        failure_summary="WireGuard bootstrap 还没有完成，请先看失败阶段。",
    )
    return JSONResponse(response)


@app.post("/api/swarm/ensure-joined")
def run_swarm_ensure_joined(payload: RuntimeBootstrapRequest) -> JSONResponse:
    state_dir = _state_dir(payload.state_dir)
    result = ensure_joined_to_platform_swarm(
        backend_url=payload.backend_url,
        state_dir=state_dir,
    )
    stages = [
        _stage_from_result(
            "swarm_join",
            "确保本机加入平台 Swarm",
            result.get("join_result") if result.get("action") == "joined" else result,
            "本机已加入平台 Swarm worker。",
        )
    ]
    if result.get("action") == "already_joined":
        stages = [_stage("swarm_join", "确保本机加入平台 Swarm", "success", "本机已经在平台 Swarm 中。")]
    response = _operation_payload(
        state_dir=state_dir,
        action="swarm_join",
        title="平台 Swarm 接入",
        stages=stages,
        result=result,
        success_summary="本机 Swarm 状态已经满足平台要求。",
        failure_summary="本机还没有成功加入平台 Swarm。",
    )
    return JSONResponse(response)


@app.post("/api/registry/trust")
def run_registry_trust(payload: RegistryTrustRequest) -> JSONResponse:
    state_dir = _state_dir(payload.state_dir)
    result = configure_registry_trust(
        registry=payload.registry,
        restart_docker=payload.restart_docker,
    )
    response = _operation_payload(
        state_dir=state_dir,
        action="registry_trust",
        title="Registry 连接检查",
        stages=_registry_trust_stages(result, result.get("registry") or payload.registry),
        result=result,
        success_summary="Registry HTTPS 入口已确认可用，接下来可以继续推送镜像。",
        failure_summary="Registry HTTPS 入口检查未通过，镜像推送大概率会继续失败。",
    )
    return JSONResponse(response)


@app.post("/api/images/push")
def run_push_image(payload: PushImageRequest) -> JSONResponse:
    state_dir = _state_dir(payload.state_dir)
    result = push_and_report_image(
        local_tag=payload.local_tag,
        repository=payload.repository,
        remote_tag=payload.remote_tag,
        registry=payload.registry,
        backend_url=payload.backend_url,
        state_dir=state_dir,
    )
    offer_response = _platform_request(state_dir, "/api/v1/platform/image-offers", backend_url=payload.backend_url)
    activity_response = _platform_request(state_dir, "/api/v1/platform/activity", backend_url=payload.backend_url)
    result["offer_state"] = _matching_offer(offer_response.get("data"), payload.repository, payload.remote_tag)
    result["platform_events"] = _matching_platform_events(
        activity_response.get("data"),
        payload.repository,
        payload.remote_tag,
    )
    if not offer_response.get("ok"):
        result["offer_lookup_error"] = offer_response
    if not activity_response.get("ok"):
        result["platform_activity_error"] = activity_response
    response = _operation_payload(
        state_dir=state_dir,
        action="push_image",
        title="镜像推送与上架",
        stages=_push_image_stages(result, payload.repository, payload.remote_tag),
        result=result,
        success_summary="镜像已经推送到 Registry，并由平台后端自动发布为 buyer 可见商品。",
        failure_summary="镜像推送或平台自动上架失败，需要先看失败阶段。",
    )
    return JSONResponse(response)


@app.get("/api/platform/overview")
def read_platform_overview(state_dir: str | None = None) -> JSONResponse:
    result = _platform_snapshot(_state_dir(state_dir))
    return JSONResponse(result)


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=3847)


if __name__ == "__main__":
    main()
