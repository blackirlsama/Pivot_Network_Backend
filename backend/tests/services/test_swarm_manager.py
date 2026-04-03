from app.services.session_gateway_template import build_session_gateway_script
from app.services.swarm_manager import _session_gateway_entrypoint, _task_failure_detail


def test_session_gateway_entrypoint_writes_script_without_indentation() -> None:
    entrypoint = _session_gateway_entrypoint(build_session_gateway_script())

    assert "cat >/tmp/pivot-session-gateway.py <<'PY'\nimport asyncio\n" in entrypoint
    assert "python /tmp/pivot-session-gateway.py" in entrypoint
    assert "python - <<'PY'" not in entrypoint


def test_task_failure_detail_includes_service_error_text() -> None:
    detail = _task_failure_detail(
        {
            "CurrentState": "Rejected 2 seconds ago",
            "Error": "No such image: python:3.12-alpine",
        }
    )

    assert detail == "Rejected 2 seconds ago | No such image: python:3.12-alpine"

