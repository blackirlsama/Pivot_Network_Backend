from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from buyer_client.runtime.api import fetch_codex_runtime_bootstrap as fetch_backend_codex_runtime_bootstrap
from seller_client.installer import (
    buyer_codex_server_name,
    codex_config_path,
    environment_check_windows_apply_command,
)

JOB_LOCK = threading.Lock()
JOB_STORE: dict[str, dict[str, Any]] = {}
DEFAULT_REMOTE_WORKSPACE = "/workspace"
DEFAULT_LOG_TAIL_BYTES = 80_000
_CODEX_RUNTIME_ENV_KEYS = {
    "OPENAI_API_KEY",
    "CODEX_AUTH_JSON_PATH",
    "CODEX_MODEL_PROVIDER",
    "CODEX_MODEL",
    "CODEX_REVIEW_MODEL",
    "CODEX_MODEL_REASONING_EFFORT",
    "CODEX_DISABLE_RESPONSE_STORAGE",
    "CODEX_NETWORK_ACCESS",
    "CODEX_WINDOWS_WSL_SETUP_ACKNOWLEDGED",
    "CODEX_MODEL_CONTEXT_WINDOW",
    "CODEX_MODEL_AUTO_COMPACT_TOKEN_LIMIT",
    "CODEX_PROVIDER_NAME",
    "CODEX_PROVIDER_BASE_URL",
    "CODEX_PROVIDER_WIRE_API",
    "CODEX_PROVIDER_REQUIRES_OPENAI_AUTH",
}


def _env_bool(value: Any) -> str:
    return "true" if bool(value) else "false"


def _codex_runtime_env_overrides(runtime_bootstrap: dict[str, Any]) -> dict[str, str]:
    auth = runtime_bootstrap.get("auth") or {}
    provider = runtime_bootstrap.get("provider") or {}
    api_key = str(auth.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("codex_runtime_bootstrap_missing_api_key")

    return {
        "OPENAI_API_KEY": api_key,
        "CODEX_MODEL_PROVIDER": str(runtime_bootstrap.get("model_provider") or ""),
        "CODEX_MODEL": str(runtime_bootstrap.get("model") or ""),
        "CODEX_REVIEW_MODEL": str(runtime_bootstrap.get("review_model") or ""),
        "CODEX_MODEL_REASONING_EFFORT": str(runtime_bootstrap.get("model_reasoning_effort") or ""),
        "CODEX_DISABLE_RESPONSE_STORAGE": _env_bool(runtime_bootstrap.get("disable_response_storage")),
        "CODEX_NETWORK_ACCESS": str(runtime_bootstrap.get("network_access") or ""),
        "CODEX_WINDOWS_WSL_SETUP_ACKNOWLEDGED": _env_bool(runtime_bootstrap.get("windows_wsl_setup_acknowledged")),
        "CODEX_MODEL_CONTEXT_WINDOW": str(runtime_bootstrap.get("model_context_window") or ""),
        "CODEX_MODEL_AUTO_COMPACT_TOKEN_LIMIT": str(runtime_bootstrap.get("model_auto_compact_token_limit") or ""),
        "CODEX_PROVIDER_NAME": str(provider.get("name") or ""),
        "CODEX_PROVIDER_BASE_URL": str(provider.get("base_url") or ""),
        "CODEX_PROVIDER_WIRE_API": str(provider.get("wire_api") or ""),
        "CODEX_PROVIDER_REQUIRES_OPENAI_AUTH": _env_bool(provider.get("requires_openai_auth")),
    }


def _codex_process_env(
    *,
    buyer_server_url: str,
    local_id: str,
    state_dir: str,
    runtime_bootstrap: dict[str, Any],
) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _CODEX_RUNTIME_ENV_KEYS}
    env.update(_codex_runtime_env_overrides(runtime_bootstrap))
    env.update(
        {
            "PIVOT_BUYER_SERVER_URL": buyer_server_url,
            "PIVOT_BUYER_DEFAULT_LOCAL_ID": local_id,
            "PIVOT_BUYER_STATE_DIR": state_dir,
            "PIVOT_BUYER_REMOTE_WORKSPACE": DEFAULT_REMOTE_WORKSPACE,
        }
    )
    return env


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_root(state_dir: str) -> Path:
    path = Path(state_dir).expanduser().resolve() / "codex_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_dir(state_dir: str, job_id: str) -> Path:
    path = _job_root(state_dir) / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _metadata_path(job_dir: Path) -> Path:
    return job_dir / "metadata.json"


def _prompt_path(job_dir: Path) -> Path:
    return job_dir / "prompt.md"


def _stdout_path(job_dir: Path) -> Path:
    return job_dir / "stdout.log"


def _last_message_path(job_dir: Path) -> Path:
    return job_dir / "last_message.txt"


def _context_path(workspace_path: Path) -> Path:
    context_dir = workspace_path / ".pivot"
    context_dir.mkdir(parents=True, exist_ok=True)
    return context_dir / "buyer-runtime-context.json"


def _load_codex_config_text() -> str:
    path = codex_config_path()
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _job_record_for_json(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in {"process", "thread"}}


def _persist_job(record: dict[str, Any]) -> None:
    job_dir = Path(record["job_dir"])
    payload = _job_record_for_json(record)
    _metadata_path(job_dir).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_text_tail(path: Path, limit_bytes: int = DEFAULT_LOG_TAIL_BYTES) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit_bytes:
            handle.seek(max(size - limit_bytes, 0))
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _load_persisted_job(job_id: str, state_dir: str) -> dict[str, Any] | None:
    job_dir = _job_dir(state_dir, job_id)
    metadata_path = _metadata_path(job_dir)
    if not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    payload["logs"] = _read_text_tail(_stdout_path(job_dir))
    payload["final_message"] = _last_message_path(job_dir).read_text(encoding="utf-8") if _last_message_path(job_dir).exists() else ""
    return payload


def _list_persisted_jobs(state_dir: str) -> list[dict[str, Any]]:
    root = _job_root(state_dir)
    items: list[dict[str, Any]] = []
    for metadata_path in root.glob("*/metadata.json"):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        job_dir = metadata_path.parent
        payload["logs"] = _read_text_tail(_stdout_path(job_dir))
        payload["final_message"] = _last_message_path(job_dir).read_text(encoding="utf-8") if _last_message_path(job_dir).exists() else ""
        items.append(payload)
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return items


def _masked_job(record: dict[str, Any], *, include_logs: bool = False) -> dict[str, Any]:
    payload = _job_record_for_json(record)
    payload["prompt_excerpt"] = str(payload.get("user_prompt") or "")[:500]
    if include_logs:
        payload["logs"] = _read_text_tail(Path(payload["stdout_path"]))
        payload["final_message"] = (
            Path(payload["last_message_path"]).read_text(encoding="utf-8") if Path(payload["last_message_path"]).exists() else ""
        )
    else:
        payload.pop("logs", None)
        payload.pop("final_message", None)
    return payload


def _codex_command(
    *,
    workspace_path: Path,
    last_message_path: Path,
    model: str,
) -> list[str]:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise RuntimeError("codex_cli_not_found")
    command = [
        codex_bin,
        "exec",
        "--color",
        "never",
        "--full-auto",
        "--skip-git-repo-check",
        "--ephemeral",
        "-C",
        str(workspace_path),
        "--output-last-message",
        str(last_message_path),
    ]
    if model:
        command.extend(["-m", model])
    command.append("-")
    return command


def _write_workspace_context(
    *,
    workspace_path: Path,
    session_context: dict[str, Any],
    local_id: str,
    buyer_server_url: str,
    state_dir: str,
) -> str:
    context_path = _context_path(workspace_path)
    payload = {
        "local_id": local_id,
        "buyer_server_url": buyer_server_url,
        "state_dir": state_dir,
        "remote_workspace": DEFAULT_REMOTE_WORKSPACE,
        "session": session_context,
        "written_at": _utc_now_iso(),
    }
    context_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(context_path)


def _render_codex_prompt(
    *,
    user_prompt: str,
    local_id: str,
    workspace_path: Path,
    context_path: str,
) -> str:
    return (
        "You are operating inside the local buyer workstation.\n"
        "Your runtime control plane is the buyerRuntimeAgent MCP server.\n"
        f"The default buyer session local_id is already configured as {local_id}.\n"
        f"The local workspace path is {workspace_path}.\n"
        f"A session context file is available at {context_path}.\n"
        "Use the buyerRuntimeAgent MCP tools instead of handwritten curl or raw HTTP whenever you need to:\n"
        "- inspect the selected buyer runtime session\n"
        "- connect the session to the seller gateway\n"
        "- run commands inside the remote container\n"
        "- read container logs\n"
        "- upload local files or directories to the container\n"
        "- download generated results back to the local machine\n"
        "Preferred workflow:\n"
        "1. Inspect the local workspace and the buyer session context first.\n"
        "2. If the session is not connected, connect it before container work.\n"
        "3. Edit local files only inside the selected workspace.\n"
        f"4. Use MCP upload/download to exchange files with the container under {DEFAULT_REMOTE_WORKSPACE}.\n"
        "5. Run container commands through MCP exec and verify outputs.\n"
        "6. End with a concise summary listing edited local files, uploaded paths, commands run, and downloaded results.\n\n"
        "User task:\n"
        f"{user_prompt.strip()}\n"
    )


def codex_status(state_dir: str) -> dict[str, Any]:
    config_text = _load_codex_config_text()
    jobs = list_codex_jobs(state_dir)
    return {
        "codex_cli": shutil.which("codex") or "",
        "codex_ready": bool(shutil.which("codex")),
        "codex_config_path": str(codex_config_path()),
        "buyer_mcp_server_name": buyer_codex_server_name(),
        "buyer_mcp_attached": f"[mcp_servers.{buyer_codex_server_name()}]" in config_text,
        "seller_mcp_attached": "[mcp_servers.sellerNodeAgent]" in config_text,
        "windows_apply_command": environment_check_windows_apply_command() if os.name == "nt" else "",
        "job_count": len(jobs),
        "jobs": jobs,
    }


def list_codex_jobs(state_dir: str) -> list[dict[str, Any]]:
    persisted = {item["job_id"]: item for item in _list_persisted_jobs(state_dir)}
    with JOB_LOCK:
        for job_id, record in JOB_STORE.items():
            if str(record.get("state_dir") or "") != str(Path(state_dir).expanduser().resolve()):
                continue
            persisted[job_id] = _masked_job(record, include_logs=True)
    items = list(persisted.values())
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return items


def get_codex_job(job_id: str, state_dir: str) -> dict[str, Any]:
    with JOB_LOCK:
        record = JOB_STORE.get(job_id)
        if record and str(record.get("state_dir") or "") == str(Path(state_dir).expanduser().resolve()):
            return _masked_job(record, include_logs=True)
    payload = _load_persisted_job(job_id, state_dir)
    if payload is None:
        raise KeyError(job_id)
    return payload


def _watch_codex_process(job_id: str, state_dir: str) -> None:
    with JOB_LOCK:
        record = JOB_STORE.get(job_id)
        if record is None:
            return
        process: subprocess.Popen[str] | None = record.get("process")
        stdout_path = Path(record["stdout_path"])
        last_message_path = Path(record["last_message_path"])
    if process is None:
        return

    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("a", encoding="utf-8") as handle:
        if process.stdout is not None:
            for line in process.stdout:
                handle.write(line)
                handle.flush()
    returncode = process.wait()
    final_message = last_message_path.read_text(encoding="utf-8") if last_message_path.exists() else ""

    with JOB_LOCK:
        current = JOB_STORE.get(job_id)
        if current is None:
            return
        current["returncode"] = returncode
        current["ended_at"] = _utc_now_iso()
        current["final_message"] = final_message
        current["logs"] = _read_text_tail(Path(current["stdout_path"]))
        if current.get("status") == "canceling":
            current["status"] = "canceled"
        else:
            current["status"] = "completed" if returncode == 0 else "failed"
        current.pop("process", None)
        current.pop("thread", None)
        _persist_job(current)


def create_codex_job(
    *,
    local_id: str,
    user_prompt: str,
    workspace_path: str,
    state_dir: str,
    backend_url: str,
    buyer_token: str,
    buyer_server_url: str,
    session_context: dict[str, Any],
    model: str = "",
) -> dict[str, Any]:
    resolved_state_dir = str(Path(state_dir).expanduser().resolve())
    resolved_workspace = Path(workspace_path).expanduser().resolve()
    resolved_workspace.mkdir(parents=True, exist_ok=True)
    runtime_bootstrap = fetch_backend_codex_runtime_bootstrap(
        backend_url=backend_url,
        buyer_token=buyer_token,
    )
    job_id = uuid.uuid4().hex
    job_dir = _job_dir(resolved_state_dir, job_id)
    context_path = _write_workspace_context(
        workspace_path=resolved_workspace,
        session_context=session_context,
        local_id=local_id,
        buyer_server_url=buyer_server_url,
        state_dir=resolved_state_dir,
    )
    final_prompt = _render_codex_prompt(
        user_prompt=user_prompt,
        local_id=local_id,
        workspace_path=resolved_workspace,
        context_path=context_path,
    )
    _prompt_path(job_dir).write_text(final_prompt, encoding="utf-8")
    command = _codex_command(
        workspace_path=resolved_workspace,
        last_message_path=_last_message_path(job_dir),
        model=model.strip(),
    )
    env = _codex_process_env(
        buyer_server_url=buyer_server_url,
        local_id=local_id,
        state_dir=resolved_state_dir,
        runtime_bootstrap=runtime_bootstrap,
    )
    process = subprocess.Popen(
        command,
        cwd=str(resolved_workspace),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdin is not None:
        process.stdin.write(final_prompt)
        process.stdin.close()

    record = {
        "job_id": job_id,
        "state_dir": resolved_state_dir,
        "local_id": local_id,
        "workspace_path": str(resolved_workspace),
        "buyer_server_url": buyer_server_url,
        "status": "running",
        "model": model.strip(),
        "created_at": _utc_now_iso(),
        "ended_at": "",
        "pid": process.pid,
        "returncode": None,
        "user_prompt": user_prompt,
        "command": command,
        "job_dir": str(job_dir),
        "prompt_path": str(_prompt_path(job_dir)),
        "stdout_path": str(_stdout_path(job_dir)),
        "last_message_path": str(_last_message_path(job_dir)),
        "context_path": context_path,
        "final_message": "",
        "logs": "",
        "process": process,
    }
    with JOB_LOCK:
        JOB_STORE[job_id] = record
        _persist_job(record)
        thread = threading.Thread(target=_watch_codex_process, args=(job_id, resolved_state_dir), daemon=True)
        record["thread"] = thread
        thread.start()
    return _masked_job(record, include_logs=True)


def cancel_codex_job(job_id: str, state_dir: str) -> dict[str, Any]:
    resolved_state_dir = str(Path(state_dir).expanduser().resolve())
    with JOB_LOCK:
        record = JOB_STORE.get(job_id)
        if record is None or str(record.get("state_dir") or "") != resolved_state_dir:
            payload = _load_persisted_job(job_id, resolved_state_dir)
            if payload is None:
                raise KeyError(job_id)
            return payload
        process: subprocess.Popen[str] | None = record.get("process")
        if process is None or record.get("status") not in {"running", "canceling"}:
            return _masked_job(record, include_logs=True)
        record["status"] = "canceling"
        _persist_job(record)
    process.terminate()
    return get_codex_job(job_id, resolved_state_dir)
