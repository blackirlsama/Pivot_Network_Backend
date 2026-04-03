from __future__ import annotations

import json
from textwrap import dedent

import paramiko

from app.core.config import Settings
from app.services.session_gateway_template import build_session_gateway_script


class SwarmManagerError(RuntimeError):
    pass


def _task_failure_detail(task: dict[str, object]) -> str:
    state = str(task.get("CurrentState") or "").strip()
    error = str(task.get("Error") or "").strip()
    if state and error:
        return f"{state} | {error}"
    if state:
        return state
    if error:
        return error
    return "task failure with no state detail"


def _ssh_client(settings: Settings) -> paramiko.SSHClient:
    host = settings.WIREGUARD_SERVER_SSH_HOST
    port = settings.WIREGUARD_SERVER_SSH_PORT
    user = settings.WIREGUARD_SERVER_SSH_USER
    password = settings.WIREGUARD_SERVER_SSH_PASSWORD
    key_path = settings.WIREGUARD_SERVER_SSH_KEY_PATH

    if not host or not user:
        raise SwarmManagerError("swarm_manager_ssh_host_or_user_missing")
    if not password and not key_path:
        raise SwarmManagerError("swarm_manager_ssh_credentials_missing")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict[str, object] = {
        "hostname": host,
        "port": port,
        "username": user,
        "timeout": 20,
        "banner_timeout": 20,
        "auth_timeout": 20,
    }
    if key_path:
        kwargs["key_filename"] = key_path
        if password:
            kwargs["passphrase"] = password
    else:
        kwargs["password"] = password
    client.connect(**kwargs)
    return client


def get_worker_join_token(settings: Settings) -> dict[str, object]:
    client = _ssh_client(settings)
    try:
        result = _exec(client, "docker swarm join-token -q worker")
        token = str(result["stdout"]).strip()
        if not result["ok"] or not token:
            raise SwarmManagerError(
                f"swarm_worker_join_token_failed: {result['stderr'] or token or 'unknown error'}"
            )
        return {
            "ok": True,
            "join_token": token,
            "manager_host": settings.SWARM_MANAGER_HOST,
            "manager_port": settings.SWARM_MANAGER_PORT,
        }
    finally:
        client.close()


def _exec(client: paramiko.SSHClient, command: str) -> dict[str, object]:
    stdin, stdout, stderr = client.exec_command(command, timeout=30)
    stdout_text = stdout.read().decode("utf-8", "replace").strip()
    stderr_text = stderr.read().decode("utf-8", "replace").strip()
    return {
        "ok": stdout.channel.recv_exit_status() == 0,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "command": command,
    }


def get_manager_overview(settings: Settings) -> dict[str, object]:
    client = _ssh_client(settings)
    try:
        swarm_info = _exec(client, "docker info --format '{{json .Swarm}}'")
        node_list = _exec(client, "docker node ls")
        service_list = _exec(client, "docker service ls")
        if not swarm_info["ok"]:
            raise SwarmManagerError(
                f"swarm_manager_info_failed: {swarm_info['stderr'] or swarm_info['stdout'] or 'unknown error'}"
            )
        parsed_swarm = json.loads(str(swarm_info["stdout"]))
        return {
            "manager_host": settings.SWARM_MANAGER_HOST,
            "manager_port": settings.SWARM_MANAGER_PORT,
            "swarm": {
                "state": parsed_swarm.get("LocalNodeState"),
                "node_id": parsed_swarm.get("NodeID"),
                "node_addr": parsed_swarm.get("NodeAddr"),
                "control_available": bool(parsed_swarm.get("ControlAvailable")),
                "nodes": parsed_swarm.get("Nodes"),
                "managers": parsed_swarm.get("Managers"),
                "cluster_id": (parsed_swarm.get("Cluster") or {}).get("ID"),
            },
            "node_list": str(node_list["stdout"]),
            "service_list": str(service_list["stdout"]),
        }
    except json.JSONDecodeError as exc:
        raise SwarmManagerError("swarm_manager_info_invalid_json") from exc
    finally:
        client.close()


def _exec_script(client: paramiko.SSHClient, script: str) -> dict[str, object]:
    stdin, stdout, stderr = client.exec_command(script, timeout=90)
    stdout_text = stdout.read().decode("utf-8", "replace").strip()
    stderr_text = stderr.read().decode("utf-8", "replace").strip()
    return {
        "ok": stdout.channel.recv_exit_status() == 0,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "command": script,
    }


def _label_args(labels: dict[str, str] | None) -> list[str]:
    if not labels:
        return []
    args: list[str] = []
    for key, value in labels.items():
        args.extend(["--label", f"{key}={value}"])
    return args


def _session_gateway_entrypoint(gateway_script: str) -> str:
    script_body = gateway_script.rstrip() + "\n"
    return (
        "set -e\n"
        "python -m pip install --disable-pip-version-check --no-cache-dir fastapi uvicorn websockets "
        ">/tmp/pivot-session-gateway-pip.log 2>&1\n"
        "cat >/tmp/pivot-session-gateway.py <<'PY'\n"
        f"{script_body}"
        "PY\n"
        "python /tmp/pivot-session-gateway.py"
    )


def create_session_gateway_service(
    settings: Settings,
    *,
    service_name: str,
    placement_constraint: str,
    gateway_port: int,
    runtime_service_name: str,
    session_id: int,
    buyer_user_id: int,
    seller_node_id: int,
    session_token: str,
    supported_features: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, object]:
    client = _ssh_client(settings)
    try:
        gateway_script = build_session_gateway_script()
        supported_features_env = ",".join(supported_features or [])
        gateway_entrypoint = _session_gateway_entrypoint(gateway_script)
        label_args = _label_args(labels)
        script = dedent(
            f"""
            python3 - <<'PY'
            import json
            import subprocess

            try:
                create_command = [
                    "docker", "service", "create",
                    "-d",
                    "--name", {json.dumps(service_name)},
                    "--constraint", {json.dumps(placement_constraint)},
                    "--restart-condition", "any",
                    "--publish", {json.dumps(f"published={gateway_port},target={gateway_port},mode=host")},
                    "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
                    "--env", {json.dumps(f"PIVOT_SESSION_ID={session_id}")},
                    "--env", {json.dumps(f"PIVOT_BUYER_USER_ID={buyer_user_id}")},
                    "--env", {json.dumps(f"PIVOT_SELLER_NODE_ID={seller_node_id}")},
                    "--env", {json.dumps(f"PIVOT_GATEWAY_PORT={gateway_port}")},
                    "--env", {json.dumps(f"PIVOT_RUNTIME_SERVICE_NAME={runtime_service_name}")},
                    "--env", {json.dumps(f"PIVOT_GATEWAY_SERVICE_NAME={service_name}")},
                    "--env", {json.dumps(f"PIVOT_SESSION_TOKEN={session_token}")},
                    "--env", {json.dumps(f"PIVOT_SUPPORTED_FEATURES={supported_features_env}")},
                    *json.loads({json.dumps(json.dumps(label_args))}),
                    {json.dumps(settings.SESSION_GATEWAY_IMAGE)},
                    "sh", "-lc", {json.dumps(gateway_entrypoint)},
                ]
                subprocess.run(create_command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(json.dumps({{"ok": True}}))
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr.decode("utf-8", "replace") if exc.stderr else "") or (exc.stdout.decode("utf-8", "replace") if exc.stdout else "") or str(exc)
                print(json.dumps({{"ok": False, "detail": detail}}))
                raise
            PY
            """
        )
        result = _exec_script(client, script)
        if not result["ok"]:
            raise SwarmManagerError(f"create_session_gateway_service_failed: {result['stderr'] or result['stdout']}")
        return result
    finally:
        client.close()


def create_code_runtime_service(
    settings: Settings,
    *,
    service_name: str,
    config_name: str,
    placement_constraint: str,
    runtime_image: str,
    code_filename: str,
    code_content: str,
    entry_command: list[str],
    report_url: str,
    session_token: str,
    source_type: str = "inline_code",
    archive_filename: str | None = None,
    archive_content_base64: str = "",
    working_dir: str | None = None,
    run_command: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, object]:
    client = _ssh_client(settings)
    try:
        archive_config_name = f"{config_name}-archive"
        archive_target = "/workspace/_payload.zip"
        _ = archive_filename
        runner_script = dedent(
            """
            import io
            import json
            import os
            import subprocess
            import zipfile
            import sys
            import traceback
            import urllib.request
            from contextlib import redirect_stderr, redirect_stdout

            buf = io.StringIO()
            status = "completed"
            exit_code = 0
            namespace = {"__name__": "__main__"}

            try:
                source_type = os.environ.get("BUYER_SOURCE_TYPE", "inline_code")
                filename = os.environ.get("BUYER_CODE_FILENAME", "main.py")
                work_root = "/workspace/src"
                os.makedirs(work_root, exist_ok=True)
                if source_type == "archive":
                    archive_path = os.environ.get("BUYER_ARCHIVE_PATH", "/workspace/_payload.zip")
                    with zipfile.ZipFile(archive_path, "r") as handle:
                        handle.extractall(work_root)
                    workdir_hint = os.environ.get("BUYER_WORKDIR", "").strip().strip("/")
                    workdir = os.path.join(work_root, workdir_hint) if workdir_hint else work_root
                    run_command = json.loads(os.environ.get("BUYER_RUN_COMMAND_JSON", "[]")) or ["python", filename]
                    with redirect_stdout(buf), redirect_stderr(buf):
                        proc = subprocess.run(run_command, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    buf.write(proc.stdout or "")
                    buf.write(proc.stderr or "")
                    exit_code = int(proc.returncode)
                    status = "completed" if exit_code == 0 else "failed"
                else:
                    code = os.environ.get("BUYER_CODE", "")
                    with redirect_stdout(buf), redirect_stderr(buf):
                        exec(compile(code, filename, "exec"), namespace, namespace)
            except SystemExit as exc:
                exit_code = int(exc.code) if isinstance(exc.code, int) else 0
                status = "completed" if exit_code == 0 else "failed"
            except Exception:
                status = "failed"
                exit_code = 1
                with redirect_stdout(buf), redirect_stderr(buf):
                    traceback.print_exc()

            logs = buf.getvalue()
            payload = {
                "session_token": os.environ.get("BUYER_SESSION_TOKEN", ""),
                "status": status,
                "logs": logs,
                "exit_code": exit_code,
            }
            request = urllib.request.Request(
                os.environ.get("BUYER_RUNTIME_REPORT_URL", ""),
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=30).read()
            except Exception:
                pass

            sys.stdout.write(logs)
            sys.exit(exit_code)
            """
        ).strip()
        label_args = _label_args(labels)
        script = dedent(
            f"""
            python3 - <<'PY'
            import json
            import subprocess
            import base64
            import time

            config_name = {json.dumps(config_name)}
            try:
                subprocess.run(
                    ["docker", "config", "rm", config_name],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                subprocess.run(
                    ["docker", "config", "rm", {json.dumps(archive_config_name)}],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if {json.dumps(source_type)} == "archive":
                    archive_bytes = base64.b64decode({json.dumps(archive_content_base64)})
                    subprocess.run(["docker", "config", "create", {json.dumps(archive_config_name)}, "-"], input=archive_bytes, check=True)
                    for _ in range(5):
                        probe = subprocess.run(["docker", "config", "inspect", {json.dumps(archive_config_name)}], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        if probe.returncode == 0:
                            break
                        time.sleep(1)
                create_command = [
                    "docker", "service", "create",
                    "-d",
                    "--name", {json.dumps(service_name)},
                    "--constraint", {json.dumps(placement_constraint)},
                    "--restart-condition", "none",
                    "--env", {json.dumps(f"BUYER_SOURCE_TYPE={source_type}")},
                    "--env", {json.dumps(f"BUYER_RUNTIME_REPORT_URL={report_url}")},
                    "--env", {json.dumps(f"BUYER_SESSION_TOKEN={session_token}")},
                    "--env", {json.dumps(f"BUYER_CODE_FILENAME={code_filename}")},
                    "--env", {json.dumps(f"BUYER_WORKDIR={working_dir or ''}")},
                    "--env", {json.dumps(f"BUYER_RUN_COMMAND_JSON={json.dumps(run_command or entry_command)}")},
                    "--env", "PYTHONUNBUFFERED=1",
                    "--env", "PYTHONDONTWRITEBYTECODE=1",
                    "--env", {json.dumps(f"BUYER_CODE={code_content}")},
                    *json.loads({json.dumps(json.dumps(label_args))}),
                    {json.dumps(runtime_image)},
                ]
                if {json.dumps(source_type)} == "archive":
                    create_command.extend(
                        [
                            "--config",
                            {json.dumps(f"source={archive_config_name},target={archive_target}")},
                            "--env",
                            {json.dumps(f"BUYER_ARCHIVE_PATH={archive_target}")},
                        ]
                    )
                create_command.extend(
                    [
                        "python", "-c", {json.dumps(runner_script)},
                    ]
                )
                subprocess.run(create_command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(json.dumps({{"ok": True}}))
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr.decode("utf-8", "replace") if exc.stderr else "") or (exc.stdout.decode("utf-8", "replace") if exc.stdout else "") or str(exc)
                print(json.dumps({{"ok": False, "detail": detail}}))
                raise
            PY
            """
        )
        result = _exec_script(client, script)
        if not result["ok"]:
            raise SwarmManagerError(f"create_code_runtime_service_failed: {result['stderr'] or result['stdout']}")
        return result
    finally:
        client.close()


def create_shell_runtime_service(
    settings: Settings,
    *,
    service_name: str,
    placement_constraint: str,
    runtime_image: str,
    entry_command: list[str],
    labels: dict[str, str] | None = None,
) -> dict[str, object]:
    client = _ssh_client(settings)
    try:
        label_args = _label_args(labels)
        script = dedent(
            f"""
            python3 - <<'PY'
            import json
            import subprocess

            try:
                create_command = [
                    "docker", "service", "create",
                    "-d",
                    "--name", {json.dumps(service_name)},
                    "--constraint", {json.dumps(placement_constraint)},
                    "--restart-condition", "none",
                    *json.loads({json.dumps(json.dumps(label_args))}),
                    {json.dumps(runtime_image)},
                    *json.loads({json.dumps(json.dumps(entry_command))}),
                ]
                subprocess.run(create_command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(json.dumps({{"ok": True}}))
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr.decode("utf-8", "replace") if exc.stderr else "") or (exc.stdout.decode("utf-8", "replace") if exc.stdout else "") or str(exc)
                print(json.dumps({{"ok": False, "detail": detail}}))
                raise
            PY
            """
        )
        result = _exec_script(client, script)
        if not result["ok"]:
            raise SwarmManagerError(f"create_shell_runtime_service_failed: {result['stderr'] or result['stdout']}")
        return result
    finally:
        client.close()


def validate_runtime_image_on_node(
    settings: Settings,
    *,
    service_name: str,
    placement_constraint: str,
    runtime_image: str,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    client = _ssh_client(settings)
    try:
        script = dedent(
            f"""
            python3 - <<'PY'
            import json
            import subprocess
            import time

            name = {json.dumps(service_name)}
            try:
                subprocess.run(["docker", "service", "rm", name], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(
                    [
                        "docker", "service", "create",
                        "-d",
                        "--name", name,
                        "--constraint", {json.dumps(placement_constraint)},
                        "--restart-condition", "none",
                        {json.dumps(runtime_image)},
                        "sh", "-lc", "echo image-validated",
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                deadline = time.time() + {int(timeout_seconds)}
                last = ""
                while time.time() < deadline:
                    ps = subprocess.run(
                        ["docker", "service", "ps", name, "--no-trunc", "--format", "{{{{json .}}}}"],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        universal_newlines=True,
                    )
                    lines = [line for line in ps.stdout.splitlines() if line.strip()]
                    if lines:
                        current = json.loads(lines[0])
                        last = current.get("CurrentState", "")
                        if "Complete" in last or "Running" in last:
                            break
                        if "Failed" in last or "Rejected" in last:
                            error = (current.get("Error") or "").strip()
                            detail = f"{last} | {error}" if error else last
                            raise RuntimeError(detail)
                    time.sleep(2)
                logs = subprocess.run(
                    ["docker", "service", "logs", name, "--raw", "--no-task-ids", "--tail", "20"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                subprocess.run(["docker", "service", "rm", name], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(json.dumps({{"ok": True, "logs": logs.stdout, "state": last}}))
            except Exception as exc:
                subprocess.run(["docker", "service", "rm", name], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(json.dumps({{"ok": False, "detail": str(exc)}}))
                raise
            PY
            """
        )
        result = _exec_script(client, script)
        if not result["ok"]:
            raise SwarmManagerError(f"validate_runtime_image_on_node_failed: {result['stderr'] or result['stdout']}")
        return result
    finally:
        client.close()


def probe_node_capabilities_on_node(
    settings: Settings,
    *,
    service_name: str,
    placement_constraint: str,
    probe_image: str,
    timeout_seconds: int = 180,
) -> dict[str, object]:
    client = _ssh_client(settings)
    try:
        probe_script = dedent(
            """
            import json
            import os
            import re
            import subprocess

            def memory_total_mb() -> float:
                try:
                    with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                        for line in handle:
                            if line.startswith("MemTotal:"):
                                parts = re.findall(r"\\d+", line)
                                if parts:
                                    return round(int(parts[0]) / 1024.0, 2)
                except Exception:
                    return 0.0
                return 0.0

            gpus = []
            try:
                proc = subprocess.run(
                    ["sh", "-lc", "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                    check=False,
                )
                for line in proc.stdout.splitlines():
                    if not line.strip():
                        continue
                    parts = [part.strip() for part in line.split(",", maxsplit=1)]
                    if len(parts) != 2:
                        continue
                    digits = re.findall(r"\\d+", parts[1])
                    gpus.append(
                        {
                            "model": parts[0],
                            "memory_total_mb": int(digits[0]) if digits else 0,
                            "count": 1,
                        }
                    )
            except Exception:
                pass

            payload = {
                "cpu_logical": os.cpu_count() or 0,
                "memory_total_mb": memory_total_mb(),
                "gpus": gpus,
                "probe_image": os.environ.get("PRICING_PROBE_IMAGE", ""),
            }
            print(json.dumps(payload))
            """
        ).strip()
        script = dedent(
            f"""
            python3 - <<'PY'
            import json
            import subprocess
            import time

            name = {json.dumps(service_name)}
            try:
                subprocess.run(["docker", "service", "rm", name], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                subprocess.run(
                    [
                        "docker", "service", "create",
                        "-d",
                        "--name", name,
                        "--constraint", {json.dumps(placement_constraint)},
                        "--restart-condition", "none",
                        "--env", {json.dumps(f"PRICING_PROBE_IMAGE={probe_image}")},
                        {json.dumps(probe_image)},
                        "python", "-c", {json.dumps(probe_script)},
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                deadline = time.time() + {int(timeout_seconds)}
                last = ""
                while time.time() < deadline:
                    ps = subprocess.run(
                        ["docker", "service", "ps", name, "--no-trunc", "--format", "{{{{json .}}}}"],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        universal_newlines=True,
                    )
                    lines = [line for line in ps.stdout.splitlines() if line.strip()]
                    if lines:
                        current = json.loads(lines[0])
                        last = current.get("CurrentState", "")
                        if "Complete" in last:
                            break
                        if "Failed" in last or "Rejected" in last:
                            error = (current.get("Error") or "").strip()
                            detail = f"{last} | {error}" if error else last
                            raise RuntimeError(detail)
                    time.sleep(2)
                logs = subprocess.run(
                    ["docker", "service", "logs", name, "--raw", "--no-task-ids", "--tail", "40"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                subprocess.run(["docker", "service", "rm", name], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                payload = {{}}
                for line in logs.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("{{") and line.endswith("}}"):
                        payload = json.loads(line)
                        break
                print(json.dumps({{"ok": True, "probe": payload, "logs": logs.stdout, "state": last}}))
            except Exception as exc:
                subprocess.run(["docker", "service", "rm", name], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(json.dumps({{"ok": False, "detail": str(exc)}}))
                raise
            PY
            """
        )
        result = _exec_script(client, script)
        if not result["ok"]:
            raise SwarmManagerError(f"probe_node_capabilities_on_node_failed: {result['stderr'] or result['stdout']}")
        return result
    finally:
        client.close()


def inspect_code_runtime_service(settings: Settings, *, service_name: str) -> dict[str, object]:
    return inspect_swarm_service(settings, service_name=service_name)


def inspect_swarm_service(settings: Settings, *, service_name: str) -> dict[str, object]:
    client = _ssh_client(settings)
    try:
        ps_result = _exec(
            client,
            f"docker service ps {service_name} --no-trunc --format '{{{{json .}}}}'",
        )
        logs_result = _exec(client, f"docker service logs {service_name} --raw --no-task-ids --tail 200")
        if not ps_result["ok"]:
            raise SwarmManagerError(f"inspect_code_runtime_service_failed: {ps_result['stderr'] or ps_result['stdout']}")
        tasks = []
        for line in str(ps_result["stdout"]).splitlines():
            line = line.strip()
            if not line:
                continue
            tasks.append(json.loads(line))
        current = tasks[0] if tasks else {}
        return {
            "tasks": tasks,
            "current_task": current,
            "current_task_error_detail": _task_failure_detail(current) if current else "",
            "logs": str(logs_result["stdout"]) if logs_result["ok"] else "",
        }
    finally:
        client.close()


def remove_code_runtime_service(settings: Settings, *, service_name: str, config_name: str) -> dict[str, object]:
    return remove_runtime_session_bundle(
        settings,
        runtime_service_name=service_name,
        config_name=config_name,
        gateway_service_name=None,
    )


def remove_runtime_session_bundle(
    settings: Settings,
    *,
    runtime_service_name: str,
    config_name: str,
    gateway_service_name: str | None,
) -> dict[str, object]:
    client = _ssh_client(settings)
    try:
        archive_config_name = f"{config_name}-archive"
        runtime_service_result = _exec(client, f"docker service rm {runtime_service_name}")
        gateway_service_result = (
            _exec(client, f"docker service rm {gateway_service_name}") if gateway_service_name else {"ok": False}
        )
        config_result = _exec(client, f"docker config rm {config_name}")
        archive_config_result = _exec(client, f"docker config rm {archive_config_name}")
        return {
            "ok": bool(
                runtime_service_result["ok"]
                or gateway_service_result["ok"]
                or config_result["ok"]
                or archive_config_result["ok"]
            ),
            "runtime_service_result": runtime_service_result,
            "gateway_service_result": gateway_service_result,
            "config_result": config_result,
            "archive_config_result": archive_config_result,
        }
    finally:
        client.close()


def create_runtime_session_bundle(
    settings: Settings,
    *,
    session_id: int,
    buyer_user_id: int,
    seller_node_id: int,
    runtime_service_name: str,
    config_name: str,
    gateway_service_name: str,
    gateway_port: int,
    placement_constraint: str,
    runtime_image: str,
    session_mode: str,
    entry_command: list[str],
    report_url: str,
    session_token: str,
    code_filename: str = "main.py",
    code_content: str = "",
    source_type: str = "inline_code",
    archive_filename: str | None = None,
    archive_content_base64: str = "",
    working_dir: str | None = None,
    run_command: list[str] | None = None,
) -> dict[str, object]:
    runtime_labels = {
        "pivot.session_id": str(session_id),
        "pivot.buyer_user_id": str(buyer_user_id),
        "pivot.seller_node_id": str(seller_node_id),
        "pivot.role": "runtime",
    }
    gateway_features = [
        item.strip()
        for item in settings.SESSION_GATEWAY_SUPPORTED_FEATURES.split(",")
        if item.strip()
    ] or ["exec", "logs", "shell"]
    gateway_labels = {
        "pivot.session_id": str(session_id),
        "pivot.buyer_user_id": str(buyer_user_id),
        "pivot.seller_node_id": str(seller_node_id),
        "pivot.role": "gateway",
    }
    created_runtime = False
    created_gateway = False
    try:
        gateway_result = create_session_gateway_service(
            settings,
            service_name=gateway_service_name,
            placement_constraint=placement_constraint,
            gateway_port=gateway_port,
            runtime_service_name=runtime_service_name,
            session_id=session_id,
            buyer_user_id=buyer_user_id,
            seller_node_id=seller_node_id,
            session_token=session_token,
            supported_features=gateway_features,
            labels=gateway_labels,
        )
        created_gateway = True
        if session_mode == "shell":
            runtime_result = create_shell_runtime_service(
                settings,
                service_name=runtime_service_name,
                placement_constraint=placement_constraint,
                runtime_image=runtime_image,
                entry_command=entry_command,
                labels=runtime_labels,
            )
        else:
            runtime_result = create_code_runtime_service(
                settings,
                service_name=runtime_service_name,
                config_name=config_name,
                placement_constraint=placement_constraint,
                runtime_image=runtime_image,
                code_filename=code_filename,
                code_content=code_content,
                entry_command=entry_command,
                report_url=report_url,
                session_token=session_token,
                source_type=source_type,
                archive_filename=archive_filename,
                archive_content_base64=archive_content_base64,
                working_dir=working_dir,
                run_command=run_command,
                labels=runtime_labels,
            )
        created_runtime = True
        return {
            "ok": True,
            "runtime": runtime_result,
            "gateway": gateway_result,
        }
    except Exception:
        if created_runtime or created_gateway:
            try:
                remove_runtime_session_bundle(
                    settings,
                    runtime_service_name=runtime_service_name,
                    config_name=config_name,
                    gateway_service_name=gateway_service_name,
                )
            except Exception:
                pass
        raise


def inspect_runtime_session_bundle(
    settings: Settings,
    *,
    runtime_service_name: str,
    gateway_service_name: str | None,
) -> dict[str, object]:
    runtime_result = inspect_swarm_service(settings, service_name=runtime_service_name)
    gateway_result = inspect_swarm_service(settings, service_name=gateway_service_name) if gateway_service_name else {}
    return {
        "runtime": runtime_result,
        "gateway": gateway_result,
    }
