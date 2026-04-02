# Backend-only Runtime Config

只在后端保存，不同步到 seller_client / 前端。

## OpenAI / CodeX Runtime

```toml
model_provider = "OpenAI"
model = "gpt-5.4"
review_model = "gpt-5.4"
model_reasoning_effort = "xhigh"
disable_response_storage = true
network_access = "enabled"
windows_wsl_setup_acknowledged = true
model_context_window = 1000000
model_auto_compact_token_limit = 900000

[model_providers.OpenAI]
name = "OpenAI"
base_url = "https://xlabapi.top/v1"
wire_api = "responses"
requires_openai_auth = true
```

## Auth File

路径：

`backend/.codex/auth.json`

内容：

```json
{
  "OPENAI_API_KEY": "sk-c1fa2c700df9706ec8fb1685b4d54e3aa541a23cf05fb41bf3ff0711af00925e"
}
```
