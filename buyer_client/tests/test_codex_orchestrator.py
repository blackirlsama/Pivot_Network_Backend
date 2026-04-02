import pytest

from buyer_client import codex_orchestrator


def test_codex_process_env_uses_backend_runtime_only(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "local-machine-key")
    monkeypatch.setenv("CODEX_PROVIDER_BASE_URL", "https://local.example/v1")
    monkeypatch.setenv("CODEX_MODEL", "local-model")

    env = codex_orchestrator._codex_process_env(
        buyer_server_url="http://127.0.0.1:3857",
        local_id="local-1",
        state_dir="d:/tmp/buyer-state",
        runtime_bootstrap={
            "model_provider": "fox",
            "model": "gpt-5",
            "review_model": "gpt-5",
            "model_reasoning_effort": "high",
            "disable_response_storage": True,
            "network_access": "enabled",
            "windows_wsl_setup_acknowledged": True,
            "model_context_window": 1_000_000,
            "model_auto_compact_token_limit": 900_000,
            "provider": {
                "name": "fox",
                "base_url": "https://code.newcli.com/codex/v1",
                "wire_api": "responses",
                "requires_openai_auth": True,
            },
            "auth": {"OPENAI_API_KEY": "platform-issued-key"},
        },
    )

    assert env["OPENAI_API_KEY"] == "platform-issued-key"
    assert env["CODEX_PROVIDER_BASE_URL"] == "https://code.newcli.com/codex/v1"
    assert env["CODEX_MODEL"] == "gpt-5"
    assert env["PIVOT_BUYER_SERVER_URL"] == "http://127.0.0.1:3857"
    assert env["PIVOT_BUYER_DEFAULT_LOCAL_ID"] == "local-1"
    assert env["PIVOT_BUYER_STATE_DIR"] == "d:/tmp/buyer-state"


def test_codex_runtime_env_requires_backend_api_key() -> None:
    with pytest.raises(RuntimeError, match="codex_runtime_bootstrap_missing_api_key"):
        codex_orchestrator._codex_runtime_env_overrides({"auth": {}})
