# Backend Skeleton

这是一个最小后端骨架，保留：

- `FastAPI`
- `SQLAlchemy 2`
- `Alembic`
- `Celery`

当前只保留框架，不保留历史业务模块。

## Quick Start

```bash
cd /home/cw/ybj/Pivot_backend_build_team/backend
uv sync
uv run uvicorn app.main:app --reload
```

健康检查：

- `GET /api/v1/health`

当前额外提供的平台 bootstrap 接口：

- `GET /api/v1/platform/runtime/codex`
- `POST /api/v1/platform/nodes/wireguard/bootstrap`
- `GET /api/v1/platform/swarm/worker-join-token`
- `GET /api/v1/platform/swarm/overview`

如果后端配置了：

- `WIREGUARD_SERVER_SSH_ENABLED=true`
- `WIREGUARD_SERVER_SSH_HOST`
- `WIREGUARD_SERVER_SSH_USER`
- `WIREGUARD_SERVER_SSH_PASSWORD` 或 `WIREGUARD_SERVER_SSH_KEY_PATH`

那么 `POST /api/v1/platform/nodes/wireguard/bootstrap` 不只会返回 profile，还会通过 SSH 自动把 peer 写入服务器侧 `wg0`。

## Backend-only CodeX Auth

如果要让平台后端向 seller-Agent 下发 CodeX/OpenAI runtime 配置，推荐只在后端保存认证材料。

支持两种来源：

1. 环境变量 `OPENAI_API_KEY`
2. `backend/.codex/auth.json`

`backend/.codex/auth.json` 格式：

```json
{
  "OPENAI_API_KEY": "replace-with-backend-only-key"
}
```

当前仓库配置已经允许提交根目录 `.env` 和 `backend/.codex/auth.json`，便于团队直接通过 Git 分发同一份后端 CodeX runtime 凭据。
后端仍然只会向已登录客户端下发 runtime bootstrap，而 buyer 侧只会把该 key 注入本次启动的 `codex` 子进程，不会改写用户在系统其它位置手动启动的 Codex 环境。

## Tests

```bash
cd /home/cw/ybj/Pivot_backend_build_team/backend
uv run pytest
```

## Migrations

```bash
cd /home/cw/ybj/Pivot_backend_build_team/backend
uv run alembic upgrade head
```
