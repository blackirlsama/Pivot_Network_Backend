from __future__ import annotations

import base64
import json
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from buyer_client.runtime.api import create_runtime_session, wait_for_runtime_completion


def run_code(
    *,
    backend_url: str,
    email: str,
    password: str,
    display_name: str | None = None,
    seller_node_key: str,
    code_path: Path,
    runtime_image: str,
    poll_seconds: int,
    requested_duration_minutes: int = 30,
) -> dict[str, Any]:
    code_content = code_path.read_text(encoding="utf-8")
    session = create_runtime_session(
        backend_url=backend_url,
        email=email,
        password=password,
        display_name=display_name,
        seller_node_key=seller_node_key,
        code_filename=code_path.name,
        code_content=code_content,
        runtime_image=runtime_image,
        session_mode="code_run",
        requested_duration_minutes=requested_duration_minutes,
    )

    last_logs = ""

    def on_update(payload: dict[str, Any]) -> None:
        nonlocal last_logs
        logs = str(payload.get("logs") or "")
        if logs and logs != last_logs:
            print(logs)
            last_logs = logs

    return wait_for_runtime_completion(
        backend_url=backend_url,
        buyer_token=session["buyer_token"],
        session_id=session["session_id"],
        poll_seconds=poll_seconds,
        require_logs=True,
        on_update=on_update,
    )


def start_shell_session(
    *,
    backend_url: str,
    email: str,
    password: str,
    display_name: str | None = None,
    seller_node_key: str,
    runtime_image: str = "python:3.12-alpine",
    requested_duration_minutes: int = 30,
) -> dict[str, Any]:
    return create_runtime_session(
        backend_url=backend_url,
        email=email,
        password=password,
        display_name=display_name,
        seller_node_key=seller_node_key,
        code_filename="__shell__",
        code_content="shell session",
        runtime_image=runtime_image,
        session_mode="shell",
        entry_command=["sh", "-lc", "while true; do sleep 3600; done"],
        requested_duration_minutes=requested_duration_minutes,
    )


def _zip_directory(directory: Path) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in directory.rglob("*"):
            if path.is_file():
                archive.write(path, arcname=path.relative_to(directory).as_posix())
    return buffer.getvalue()


def _read_archive_source(path: Path) -> tuple[str, bytes]:
    if path.is_dir():
        return f"{path.name}.zip", _zip_directory(path)
    return path.name, path.read_bytes()


def _normalize_github_repo_url(repo_url: str) -> str:
    normalized = repo_url.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def _download_github_archive(repo_url: str, ref: str) -> tuple[str, bytes]:
    normalized = _normalize_github_repo_url(repo_url)
    if not normalized.startswith("https://github.com/"):
        raise RuntimeError("unsupported_github_repo_url")
    suffix = normalized.removeprefix("https://github.com/")
    archive_url = f"https://codeload.github.com/{suffix}/zip/{ref}"
    with urllib.request.urlopen(archive_url, timeout=120) as response:
        return f"{suffix.replace('/', '-')}-{ref}.zip", response.read()


def run_archive(
    *,
    backend_url: str,
    email: str,
    password: str,
    display_name: str | None = None,
    seller_node_key: str,
    source_path: Path,
    runtime_image: str,
    poll_seconds: int,
    working_dir: str | None = None,
    run_command: list[str] | None = None,
    requested_duration_minutes: int = 30,
) -> dict[str, Any]:
    archive_filename, archive_bytes = _read_archive_source(source_path)
    session = create_runtime_session(
        backend_url=backend_url,
        email=email,
        password=password,
        display_name=display_name,
        seller_node_key=seller_node_key,
        code_filename=archive_filename,
        code_content="",
        runtime_image=runtime_image,
        session_mode="code_run",
        source_type="archive",
        archive_filename=archive_filename,
        archive_content_base64=base64.b64encode(archive_bytes).decode("ascii"),
        source_ref=str(source_path),
        working_dir=working_dir,
        run_command=run_command,
        requested_duration_minutes=requested_duration_minutes,
    )
    return wait_for_runtime_completion(
        backend_url=backend_url,
        buyer_token=session["buyer_token"],
        session_id=session["session_id"],
        poll_seconds=poll_seconds,
        require_logs=True,
    )


def run_github_repo(
    *,
    backend_url: str,
    email: str,
    password: str,
    display_name: str | None = None,
    seller_node_key: str,
    repo_url: str,
    repo_ref: str,
    runtime_image: str,
    poll_seconds: int,
    working_dir: str | None = None,
    run_command: list[str] | None = None,
    requested_duration_minutes: int = 30,
) -> dict[str, Any]:
    archive_filename, archive_bytes = _download_github_archive(repo_url, repo_ref)
    session = create_runtime_session(
        backend_url=backend_url,
        email=email,
        password=password,
        display_name=display_name,
        seller_node_key=seller_node_key,
        code_filename=archive_filename,
        code_content="",
        runtime_image=runtime_image,
        session_mode="code_run",
        source_type="archive",
        archive_filename=archive_filename,
        archive_content_base64=base64.b64encode(archive_bytes).decode("ascii"),
        source_ref=f"{repo_url}@{repo_ref}",
        working_dir=working_dir,
        run_command=run_command,
        requested_duration_minutes=requested_duration_minutes,
    )
    return wait_for_runtime_completion(
        backend_url=backend_url,
        buyer_token=session["buyer_token"],
        session_id=session["session_id"],
        poll_seconds=poll_seconds,
        require_logs=True,
    )
