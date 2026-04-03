# OVERVIEW

更新时间：`2026-04-02`

这份文档基于当前仓库里的实际代码、当前仓库里的测试、以及仓库内已归档的闭环文档整理，不以 `README.md` 的宣称为准。

本次判断主要依据：

- 代码阅读：`backend/`、`frontend/`、`buyer_client/`、`seller_client/`、`environment_check/`
- 实测通过：
  - `python -m pytest backend\tests\api\test_payments_api.py backend\tests\api\test_auth_platform.py -q`
  - `python -m pytest seller_client\tests\test_windows_gateway_bridge.py backend\tests\services\test_swarm_manager.py -q`
- 辅助证据：
  - `docs/completed/e2e/seller-to-buyer-ui-closed-loop-2026-04-02.md`

状态标记说明：

- `已实现`：有真实代码路径，且本次至少读到关键实现；部分还跑过测试。
- `部分实现`：有代码，但链路不完整、不稳定，或者只实现了其中一段。
- `占位/兼容`：有字段、字符串、接口壳子或旧 helper，但不是当前真正的产品闭环。
- `仅文档/证据`：不是功能实现本身，只是说明或归档。

## 1. 顶层结构

| 目录 / 文件 | 作用 | 主要判断 |
| --- | --- | --- |
| `backend/` | 平台后端，负责认证、节点、镜像、商品、订单、许可证、runtime session、gateway、WireGuard、计费 | 当前仓库里最核心的业务编排层，且大部分关键路径已经是真实现 |
| `frontend/` | 平台交易前端，被后端挂载到 `/platform-ui` | 只负责交易入口，不负责真正进入运行时 |
| `buyer_client/` | 买家本地控制面和本地 API server | 现在已经是真正的消费执行器，不再只是演示壳子 |
| `seller_client/` | 卖家本地控制面、MCP、安装与接入逻辑 | 卖家接入、镜像上报、Swarm/WireGuard 接入是真实现 |
| `environment_check/` | Windows 一次性安装与远端环境检查 | 真实工具模块，不是文档占位 |
| `Docker_swarm/` | 远端 Swarm/Registry/调度相关资产 | 基础设施资产目录，不是用户直接操作的主业务入口 |
| `docs/` | 已完成文档、闭环报告、图片证据 | 证据与说明，不等于功能实现 |
| `tests/` | 根目录测试与脚本 | 验证脚本目录，不是产品模块 |
| `future_task_packages/` | 后续任务包 | 不是当前主系统实现 |
| `docs.zip` | 文档归档 | 归档文件，不是代码 |
| `compose.yml` | 本地开发依赖编排 | 真实开发环境入口，但不等于完整生产拓扑 |

## 2. 先说结论

### 2.1 当前最重要的事实

1. `支付` 已经不是空白。
   现在有真实的 `payment_orders` / `payment_transactions` / `wallet_ledgers`、真实 API、真实测试。
   但它不是第三方支付闭环，而是“手工确认成功后给钱包加钱”的内部充值域。

2. `下单` 已经实现，但没有和支付真正绑定。
   当前 `BuyerOrder` 的生成并不会扣款、冻结余额、锁库存，也不会调用支付域。
   `payment_status` 在下单逻辑里直接写成了 `not_required`。

3. `许可证` 已经实现，而且真的会驱动订单型 runtime session。
   现在 buyer 侧不只是单独 redeem token，还可以通过 `POST /buyer/orders/{order_id}/start-session` 启动订单型 shell session。

4. `runtime gateway` 已经不是占位符。
   后端已经能创建 `gateway-{session_id}`，buyer client 也已经走真实的 `gateway handshake -> exec/logs/files/shell`。
   但 `relay_endpoint=relay://buyer-runtime-session/{id}` 这个字段本身仍然只是兼容性字符串，不是真正传输通道。

5. `frontend` 仍然不是完整买家控制台。
   平台前端现在能登录、看商品、看钱包、下单、redeem license。
   但它还不能直接连接 runtime，也没有把新的 `payments` API 接进去。

6. seller 的“推镜像后立刻变成 buyer 可见商品”链路仍然不稳定。
   代码里有自动发布与定价链路，但 `docs/completed/e2e/seller-to-buyer-ui-closed-loop-2026-04-02.md` 记录了实际 UI 跑通时，这一段仍可能失败，offer 会卡在 `probing`。

### 2.2 当前最容易被误判成“已实现”的点

- `DEFAULT_TEST_BALANCE_CNY_CREDITS=100.0` 是默认测试余额，不是真实充值结果。
- `BuyerWallet.frozen_amount_cny` 字段存在，但当前下单和支付链路没有使用冻结逻辑。
- `BuyerOrder.payment_status`、`BuyerOrder.paid_at` 字段存在，但当前下单服务没有把它们接到支付流程里。
- `relay_endpoint` 字段存在，但真正的传输已经改成 gateway + WireGuard。
- buyer 侧仍保留 `exec_runtime_command_locally` 旧 helper，用于 CLI 兼容；但本地 buyer Web 产品路径已经声明“不走本地 Docker fallback”。

## 3. 模块总览

| 模块 | 功能 | 代码位置 | 当前判断 |
| --- | --- | --- | --- |
| 平台认证与会话 | 用户注册、登录、节点注册 token、会话 token | `backend/app/services/auth.py`、`backend/app/api/routes/auth.py` | `已实现` |
| 卖家节点接入 | 节点注册、心跳、WireGuard bootstrap、Swarm join 令牌 | `backend/app/api/routes/platform/nodes.py`、`backend/app/api/routes/platform/runtime.py`、`backend/app/api/routes/platform/swarm.py`、`seller_client/agent_mcp.py` | `已实现` |
| 镜像上报与商品化 | 镜像上报、offer stub、probe、pricing、发布 active offer | `backend/app/api/routes/platform/images.py`、`backend/app/api/routes/platform/offers.py`、`backend/app/services/image_offer_publishing.py`、`backend/app/services/pricing_engine.py` | `部分实现` |
| Buyer 商品目录 | 只向 buyer 暴露 `offer_status=active` 的商品 | `backend/app/api/routes/buyer/catalog.py` | `已实现` |
| 钱包 | 查询余额、流水 | `backend/app/api/routes/buyer/wallet.py`、`backend/app/services/buyer_wallets.py` | `已实现`，但初始余额是测试值 |
| 支付 / 充值 | 创建充值单、确认成功后加余额、写交易和账本 | `backend/app/api/routes/buyer/payments.py`、`backend/app/services/buyer_payments.py`、`backend/app/models/payment.py` | `已实现`，但只是内部手工确认充值 |
| 下单 | 生成订单、记录时价、签发 license token | `backend/app/api/routes/buyer/orders.py`、`backend/app/services/buyer_orders.py` | `已实现`，但未扣款、未冻结、未锁资源 |
| 许可证 | 凭 `license_token` redeem 订单信息 | `backend/app/api/routes/buyer/orders.py` | `已实现` |
| 订单型 session | 用订单启动 shell session | `backend/app/api/routes/buyer/runtime_sessions.py`、`buyer_client/runtime/api.py` | `已实现` |
| 直接 session | 直接对 seller node 或 offer 创建 runtime session | `backend/app/api/routes/buyer/runtime_sessions.py`、`buyer_client/runtime/transfer.py` | `已实现` |
| Runtime gateway | 创建 gateway service，握手并返回 host/port/features | `backend/app/api/routes/buyer/runtime_sessions.py`、`backend/app/services/session_gateway_template.py`、`buyer_client/runtime/gateway.py` | `已实现` |
| Buyer WireGuard | buyer lease bootstrap、本地 `wg-buyer` 激活 | `backend/app/api/routes/buyer/runtime_sessions.py`、`buyer_client/runtime/wireguard.py`、`buyer_client/agent_server.py` | `已实现` |
| Exec / Logs / Files / Terminal | 通过 gateway 操作卖家 runtime | `buyer_client/agent_server.py`、`buyer_client/runtime/gateway.py`、`buyer_client/web/index.html` | `已实现` |
| Windows seller gateway bridge | 在 seller Windows 主机上桥接 gateway 容器与 host 侧访问 | `seller_client/windows_gateway_bridge_manager.py`、`seller_client/windows_session_gateway_host.py` | `已实现` |
| CodeX 买家编排 | 本地 CodeX 编辑工作区，借 buyer MCP 连接远端 runtime | `buyer_client/codex_orchestrator.py`、`buyer_client/agent_server.py` | `已实现` |
| 计费 | 对 offer 型 session 按小时扣费 | `backend/app/services/usage_billing.py` | `已实现`，但只覆盖 offer 型 session |
| 过期回收 | 清理 runtime/gateway service、撤销 buyer peer | `backend/app/services/runtime_sessions.py`、`backend/app/services/usage_billing.py` | `已实现` |
| 环境安装与检查 | 本地安装 helper/bridge/firewall，远端检查 WireGuard/Swarm | `environment_check/install_windows.ps1`、`environment_check/windows_bootstrap.py` | `已实现` |

## 4. 详细分析

### 4.1 `backend/`：平台后端

#### 4.1.1 认证 / 注册 / 登录

- 主要代码：
  - `backend/app/services/auth.py`
  - `backend/app/api/routes/auth.py`
- 实际行为：
  - 注册时创建 `User`
  - 同时创建 `SellerProfile`
  - 同时创建 `BuyerWallet`
  - 默认余额来自 `backend/app/core/config.py` 的 `DEFAULT_TEST_BALANCE_CNY_CREDITS=100.0`
- 结论：
  - `已实现`
  - 但 buyer 钱包初始资金是系统默认值，不是支付所得

#### 4.1.2 卖家节点接入

- 主要代码：
  - `backend/app/api/routes/platform/nodes.py`
  - `backend/app/services/platform_nodes.py`
  - `backend/app/api/routes/platform/runtime.py`
  - `backend/app/api/routes/platform/swarm.py`
  - `seller_client/agent_mcp.py`
- 实际行为：
  - 卖家可先申请 `node_registration_token`
  - 节点用 token 调用 `/platform/nodes/register`
  - 心跳走 `/platform/nodes/heartbeat`
  - WireGuard profile 通过 `/platform/nodes/wireguard/bootstrap` 下发
  - Swarm worker join token 由平台提供，真正 `docker swarm join` 在 seller 本机执行
  - 节点状态和 `wg-seller` 目标地址来自 `Node.capabilities.interfaces["wg-seller"]`
- 结论：
  - `已实现`
  - 这是 seller 接入的真实主链路，不是占位

#### 4.1.3 镜像上报、probe、pricing、offer 发布

- 主要代码：
  - `backend/app/api/routes/platform/images.py`
  - `backend/app/api/routes/platform/offers.py`
  - `backend/app/services/image_offer_publishing.py`
  - `backend/app/services/pricing_engine.py`
  - `backend/app/models/seller.py`
- 实际行为：
  - seller 上报镜像后，后端会创建或更新 `ImageArtifact`
  - 然后尝试自动执行：
    - `validate_runtime_image_on_node(...)`
    - `probe_node_capabilities_on_node(...)`
    - `publish_or_update_image_offer(...)`
    - `price_image_offer(...)`
  - `ImageOffer` 持久化了：
    - `offer_status`
    - `probe_status`
    - `probe_measured_capabilities`
    - `current_billable_price_cny_per_hour`
    - `pricing_error`
- 真实限制：
  - buyer 目录只展示 `offer_status == "active"` 的 offer
  - 仓库里的 `2026-04-02` UI 闭环文档记录了真实一次失败：
    - seller push 成功
    - `POST /api/v1/platform/images/report` 后半段失败
    - 新 offer 留在 `probing`
    - `current_billable_price_cny_per_hour=null`
- 结论：
  - `部分实现`
  - 代码链路不是占位，但产品级稳定性还不够

#### 4.1.4 Buyer 商品目录

- 主要代码：
  - `backend/app/api/routes/buyer/catalog.py`
- 实际行为：
  - buyer 只能看到 `offer_status == "active"` 的商品
  - 单商品详情也要求 `active`
- 结论：
  - `已实现`
  - 也因此 seller 自动发布失败时，buyer 侧会直接“看不到”

#### 4.1.5 钱包

- 主要代码：
  - `backend/app/models/buyer.py`
  - `backend/app/services/buyer_wallets.py`
  - `backend/app/api/routes/buyer/wallet.py`
- 实际行为：
  - 有 `BuyerWallet`
  - 有 `WalletLedger`
  - 有余额查询、流水查询
  - usage billing 与 payment top-up 都会写 `WalletLedger`
- 真实限制：
  - `frozen_amount_cny` 字段当前没有被下单流程使用
  - 钱包不是支付闭环的结算中心，只是余额与账本层
- 结论：
  - `已实现`
  - 但当前更像“余额账本骨架 + usage debit + top-up credit”

#### 4.1.6 支付 / 充值

- 主要代码：
  - `backend/app/models/payment.py`
  - `backend/app/services/buyer_payments.py`
  - `backend/app/api/routes/buyer/payments.py`
  - `backend/app/schemas/buyer/payments.py`
  - `backend/tests/api/test_payments_api.py`
- 实际行为：
  - `POST /buyer/payments` 创建 `PaymentOrder`
  - `POST /buyer/payments/{id}/confirm` 可把状态推进到 `succeeded/failed/cancelled`
  - 若确认 `succeeded`：
    - buyer wallet 加余额
    - 创建 `PaymentTransaction`
    - 创建 `WalletLedger(entry_type="topup_credit")`
  - 这部分已经有 API 测试覆盖，且本次实测通过
- 真实限制：
  - 没有微信/支付宝/Stripe/PayPal 之类真实支付通道接入
  - 没有支付二维码、支付链接、支付回调签名校验
  - `third_party_txn_id` 只是确认时传入的字符串，不是平台主动向第三方核验出来的结果
- 结论：
  - `已实现`
  - 但它是“内部手工确认充值域”，不是“真实第三方支付系统”

#### 4.1.7 下单

- 主要代码：
  - `backend/app/models/buyer.py`
  - `backend/app/services/buyer_orders.py`
  - `backend/app/api/routes/buyer/orders.py`
- 实际行为：
  - 创建 `BuyerOrder`
  - 固化 `requested_duration_minutes`
  - 固化当时的 `issued_hourly_price_cny`
  - 签发 `license_token`
  - `order_status` 初始为 `issued`
- 真实限制：
  - 下单不会扣余额
  - 下单不会冻结余额
  - 下单不会锁 seller 节点资源
  - 下单不会校验支付成功
  - `payment_status` 在 `issue_buyer_order()` 中直接写成 `not_required`
  - `paid_at` 字段存在，但当前下单服务没有使用
- 结论：
  - `已实现`
  - 但本质上只是“签发带价格快照的订单 + 许可证”，不是完整交易结算

#### 4.1.8 许可证

- 主要代码：
  - `backend/app/api/routes/buyer/orders.py`
  - `backend/app/services/buyer_orders.py`
- 实际行为：
  - `POST /buyer/orders/redeem` 通过 `license_token` 找订单
  - 首次 redeem 会把 `license_redeemed_at` 记上，并把 `order_status` 设成 `redeemed`
  - 返回 `order_id`、`offer_id`、`seller_node_key`、`runtime_image_ref`
- 真实限制：
  - 这个 redeem 接口本身不要求登录，属于“持 token 即可读订单元信息”的 bearer 风格
  - redeem 本身不会直接创建 runtime session
  - 真正启动订单型 session 时，仍要用 buyer 登录身份去调 `/buyer/orders/{order_id}/start-session`
- 结论：
  - `已实现`
  - 它是真 entitlement token，但不是强隔离的 license server

#### 4.1.9 订单型 session

- 主要代码：
  - `backend/app/api/routes/buyer/runtime_sessions.py`
  - `buyer_client/runtime/api.py`
- 实际行为：
  - 新增了 `POST /buyer/orders/{order_id}/start-session`
  - 会校验订单属于当前 buyer
  - 会确保 offer 仍然 `active`、node 仍然 `available`
  - 若同一 `order:{id}` 已有未终止 session，则复用已有 session
  - 否则创建新的 `source_type="licensed_order"` shell session
- 结论：
  - `已实现`
  - 这说明 `订单 -> 许可证 -> session` 现在已经是真链路，不再只是文档设想

#### 4.1.10 直接 runtime session

- 主要代码：
  - `backend/app/api/routes/buyer/runtime_sessions.py`
  - `buyer_client/runtime/transfer.py`
  - `buyer_client/runtime/api.py`
- 实际行为：
  - buyer 仍可直接创建 runtime session
  - 支持两类入口：
    - 用 `offer_id`
    - 用 `seller_node_key + runtime_image`
  - 支持三类源：
    - `inline_code`
    - `archive`
    - `licensed_order`
  - 支持两类模式：
    - `code_run`
    - `shell`
- 真实限制：
  - 这意味着“订单不是 runtime 的唯一入口”
  - 直接 ad hoc session 仍可绕过订单/许可证链路
- 结论：
  - `已实现`
  - 同时也说明当前权限和计费模型并未完全强制 buyer 必须先下单

#### 4.1.11 runtime redeem / relay metadata

- 主要代码：
  - `backend/app/api/routes/buyer/runtime_sessions.py`
- 实际行为：
  - `POST /buyer/runtime-sessions/redeem` 会返回：
    - `session_token`
    - `gateway_required`
    - `gateway_protocol`
    - `gateway_port`
    - `supported_features`
  - 同时仍返回 `relay_endpoint=relay://buyer-runtime-session/{id}`
- 结论：
  - redeem 本身是 `已实现`
  - `relay_endpoint` 这个字段本身属于 `占位/兼容`
  - 真正连接已经依赖 gateway handshake

#### 4.1.12 runtime gateway

- 主要代码：
  - `backend/app/api/routes/buyer/runtime_sessions.py`
  - `backend/app/services/session_gateway_template.py`
  - `backend/app/services/swarm_manager.py`
  - `buyer_client/runtime/gateway.py`
  - `backend/tests/api/test_auth_platform.py`
  - `backend/tests/services/test_swarm_manager.py`
- 实际行为：
  - 创建 session 时会同时准备：
    - `runtime-{session_id}`
    - `gateway-{session_id}`
  - gateway 端口为 `SESSION_GATEWAY_BASE_PORT + session_id`
  - gateway 握手接口会返回真实：
    - `gateway_host`
    - `gateway_port`
    - `gateway_protocol`
    - `supported_features`
  - gateway 模板里已经实现：
    - `/`
    - `/exec`
    - `/logs`
    - `/files/upload`
    - `/files/download`
    - `/shell/ws`
  - 这部分本次对应测试已通过
- 结论：
  - `已实现`
  - 旧的“gateway 只是 relay 字符串”的结论在当前仓库里已经不成立

#### 4.1.13 buyer WireGuard bootstrap

- 主要代码：
  - `backend/app/api/routes/buyer/runtime_sessions.py`
  - `backend/app/services/runtime_bootstrap.py`
  - `buyer_client/runtime/wireguard.py`
  - `buyer_client/agent_server.py`
- 实际行为：
  - buyer 可以对 session 申请 WireGuard lease
  - 后端会构造 buyer profile，并调用 `apply_server_peer(...)`
  - session 上会记录：
    - `buyer_wireguard_public_key`
    - `buyer_wireguard_client_address`
    - `seller_wireguard_target`
  - 对应 API 测试已通过
- 结论：
  - `已实现`

#### 4.1.14 停止、续租、上报、状态查询

- 主要代码：
  - `backend/app/api/routes/buyer/runtime_sessions.py`
- 实际行为：
  - `/stop` 会删除 runtime bundle，并尽量撤销 buyer peer
  - `/renew` 会延长过期时间
  - `/report` 会让 runtime 把状态与日志回写
  - `GET /runtime-sessions/{id}` 会尝试刷新 runtime/gateway 远端状态
- 结论：
  - `已实现`

#### 4.1.15 计费

- 主要代码：
  - `backend/app/services/usage_billing.py`
  - `backend/app/models/runtime.py`
  - `backend/app/models/buyer.py`
  - `backend/tests/services/test_usage_billing.py`
- 实际行为：
  - 只对 `image_offer_id is not None` 的 session 计费
  - 按小时扣 `offer.current_billable_price_cny_per_hour`
  - 扣费成功后创建：
    - `UsageCharge`
    - `WalletLedger(entry_type="hourly_debit")`
  - `RuntimeAccessSession.accrued_usage_cny` 会累计
  - 余额不足时会停掉 session
- 真实限制：
  - 直接 ad hoc session 若没有 `offer_id`，当前不会走 usage billing
  - 允许负债地板由 `SESSION_ALLOWED_DEBT_MULTIPLIER` 决定，默认 `1.0`
  - 也就是说系统允许账户最多再欠一个小时单价左右，然后才停
- 结论：
  - `已实现`
  - 但只覆盖 offer 型 / 订单型 session，不覆盖所有 session

#### 4.1.16 过期清理与后台线程

- 主要代码：
  - `backend/app/main.py`
  - `backend/app/services/runtime_sessions.py`
  - `backend/app/services/usage_billing.py`
- 实际行为：
  - 后端启动后会拉起 4 个后台线程：
    - `runtime_session_reaper`
    - `price_feed_refresher`
    - `offer_repricing_worker`
    - `usage_billing_worker`
  - 会做过期清理、价格刷新、重定价、周期计费
- 结论：
  - `已实现`

### 4.2 `frontend/`：平台交易前端

#### 4.2.1 已接入功能

- 主要代码：
  - `frontend/app.js`
- 已接入的后端接口：
  - `/api/v1/auth/register`
  - `/api/v1/auth/login`
  - `/api/v1/buyer/wallet`
  - `/api/v1/buyer/wallet/ledger`
  - `/api/v1/buyer/catalog/offers`
  - `/api/v1/buyer/orders`
  - `/api/v1/buyer/orders/{id}`
  - `/api/v1/buyer/orders/redeem`
- 实际可做的事：
  - 注册 / 登录
  - 浏览 active offers
  - 下单
  - 查看订单
  - 把订单里的 `license_token` 填回 UI
  - redeem license
  - 查看钱包余额和账本
- 结论：
  - `已实现`
  - 但它是“交易入口”，不是“运行时控制台”

#### 4.2.2 未接入功能

- `frontend/app.js` 没有接入新的 `buyer/payments` API
- 没有 runtime session 创建、gateway connect、WireGuard、终端、文件上传下载
- 结论：
  - `支付前端`：`部分实现`
  - `runtime 控制前端`：`未在这个模块实现`

### 4.3 `buyer_client/`：买家本地控制面

#### 4.3.1 本地 buyer server

- 主要代码：
  - `buyer_client/agent_server.py`
- 实际行为：
  - 本地 FastAPI server
  - 维护 `SESSION_STORE`
  - 提供 Web UI 所需的本地 API
  - 把浏览器操作桥接到：
    - 平台后端
    - WireGuard 本地状态
    - seller gateway
    - CodeX job
- 结论：
  - `已实现`

#### 4.3.2 直接 shell / inline code / archive / GitHub repo

- 主要代码：
  - `buyer_client/runtime/transfer.py`
  - `buyer_client/runtime/api.py`
- 实际行为：
  - `run_code(...)`
  - `run_archive(...)`
  - `run_github_repo(...)`
  - `start_shell_session(...)`
  - 都会落到真实的 `/buyer/runtime-sessions` 创建链路
- 结论：
  - `已实现`

#### 4.3.3 订单许可证启动 shell

- 主要代码：
  - `buyer_client/runtime/api.py`
  - `buyer_client/agent_server.py`
  - `buyer_client/web/index.html`
- 实际行为：
  - `start_licensed_shell_session(...)` 的真实顺序是：
    - `redeem_order_license(...)`
    - `start_order_runtime_session(...)`
    - `redeem_connect_code(...)`
  - buyer Web UI 也有 “Start Licensed Shell” 按钮，走的不是占位逻辑
- 结论：
  - `已实现`

#### 4.3.4 Connect Gateway

- 主要代码：
  - `buyer_client/agent_server.py`
  - `buyer_client/runtime/api.py`
  - `buyer_client/runtime/gateway.py`
- 实际行为：
  - 本地 `/api/runtime/sessions/{local_id}/connect` 会依次做：
    - `_refresh_session`
    - `handshake_runtime_gateway(...)`
    - `bootstrap_runtime_session_wireguard(...)`
    - `_probe_gateway(...)`
  - 成功后 `connection_status=connected`
- 结论：
  - `已实现`

#### 4.3.5 Exec / Logs / Files / Browser Terminal

- 主要代码：
  - `buyer_client/runtime/gateway.py`
  - `buyer_client/agent_server.py`
  - `buyer_client/web/index.html`
  - `buyer_client/web/vendor/xterm.js`
  - `buyer_client/web/vendor/xterm-addon-fit.js`
- 实际行为：
  - gateway exec：真实 HTTP `POST /exec`
  - gateway logs：真实 HTTP `GET /logs`
  - 文件上传下载：真实 `/files/upload` 和 `/files/download`
  - 浏览器终端：xterm.js + 本地 WebSocket + seller gateway `/shell/ws`
- 结论：
  - `已实现`
  - 当前 buyer Web 产品路径已经是真终端，不再是静态演示

#### 4.3.6 CodeX 编排

- 主要代码：
  - `buyer_client/codex_orchestrator.py`
  - `buyer_client/agent_server.py`
- 实际行为：
  - 检测本机 `codex` CLI
  - 拉取后端 `/platform/runtime/codex` bootstrap
  - 为 CodeX 进程注入运行时所需 env
  - 在本地工作区生成上下文文件
  - 通过 buyer MCP 与当前 runtime 交互
  - 维护持久化 job 记录、stdout、final message
- 结论：
  - `已实现`
  - 它不是空 wrapper，而是真正执行本地 `codex exec` 的 orchestration 层

#### 4.3.7 仍然保留的旧 helper

- 主要代码：
  - `buyer_client/runtime/exec.py`
  - `buyer_client/agent_cli.py`
- 实际行为：
  - 仍保留 `find_local_service_container(...)`
  - 仍保留 `exec_runtime_command_locally(...)`
  - CLI 子命令 `exec` 仍走本地 Docker 容器路径
- 结论：
  - 这些属于 `兼容/旧辅助路径`
  - 不应和当前 buyer Web 的 gateway 产品主路径混为一谈

### 4.4 `seller_client/`：卖家本地控制面

#### 4.4.1 卖家接入与平台沟通

- 主要代码：
  - `seller_client/agent_mcp.py`
  - `seller_client/agent_server.py`
  - `seller_client/installer.py`
- 实际行为：
  - 卖家本地会做：
    - 注册 / 登录
    - 申请 node token
    - 节点注册
    - WireGuard key/profile/bootstrap
    - Swarm join
    - 镜像 push
    - 向平台 report image
- 结论：
  - `已实现`

#### 4.4.2 Windows seller gateway bridge

- 主要代码：
  - `seller_client/windows_gateway_bridge_manager.py`
  - `seller_client/windows_session_gateway_host.py`
  - `seller_client/tests/test_windows_gateway_bridge.py`
- 实际行为：
  - bridge manager 会扫描运行中的 `gateway-*` 容器
  - 从容器 env 提取：
    - `PIVOT_SESSION_ID`
    - `PIVOT_GATEWAY_PORT`
    - `PIVOT_RUNTIME_SERVICE_NAME`
    - `PIVOT_SESSION_TOKEN`
  - 然后在 Windows host 上拉起 `windows_session_gateway_host`
  - host 侧 gateway 暴露：
    - `/`
    - `/exec`
    - `/logs`
    - `/files/upload`
    - `/files/download`
    - `/shell/ws`
  - 这部分基础测试本次已通过
- 结论：
  - `已实现`
  - 当前仓库里 seller Windows 桥接不是文档空话

#### 4.4.3 卖家 UI 的边界

- 主要代码：
  - `seller_client/web/index.html`
- 实际行为：
  - seller UI 负责节点接入、环境状态、镜像上架、Swarm/WireGuard 状态
  - 不负责 buyer 侧消费运行时
- 结论：
  - 模块边界清晰
  - 不应把 seller UI 当成 buyer runtime 面板

### 4.5 `environment_check/`：环境安装与核验

#### 4.5.1 Windows 一次性安装

- 主要代码：
  - `environment_check/install_windows.ps1`
  - `environment_check/README.md`
- 实际行为：
  - 安装 / 挂载：
    - WireGuard elevated helper
    - session gateway bridge 计划任务
    - gateway 防火墙规则
    - seller / buyer MCP 相关准备
- 结论：
  - `已实现`

#### 4.5.2 远端 WireGuard / Swarm 检查

- 主要代码：
  - `environment_check/windows_bootstrap.py`
  - 根目录测试：`tests/test_environment_check_windows_bootstrap.py`
- 实际行为：
  - 会读取 `.env` / 环境变量
  - 用 `paramiko` SSH 到远端机器
  - 检查：
    - `wg-quick@wg0`
    - `ip addr show`
    - `wg show`
    - `ss -lunp`
    - `docker info --format '{{json .Swarm}}'`
  - 会输出本地与远端综合报告
- 结论：
  - `已实现`

### 4.6 `Docker_swarm/`、`docs/`、`tests/`

#### 4.6.1 `Docker_swarm/`

- 定位：
  - 基础设施资产目录
- 结论：
  - 是远端运行面相关材料
  - 不是当前仓库里最主要的业务入口

#### 4.6.2 `docs/`

- 定位：
  - 闭环说明、截图、手工验证记录
- 结论：
  - `仅文档/证据`
  - 可用来证明“有人跑过”，不能替代代码判断

#### 4.6.3 根目录 `tests/`

- 文件：
  - `tests/test_environment_check_windows_bootstrap.py`
  - `tests/test_cccc_layout.py`
  - `tests/e2e_local_web_flow.py`
- 结论：
  - 属于验证脚本与测试，不是产品模块

## 5. 重点业务单元结论

### 5.1 支付

- 真实代码位置：
  - `backend/app/models/payment.py`
  - `backend/app/services/buyer_payments.py`
  - `backend/app/api/routes/buyer/payments.py`
  - `backend/tests/api/test_payments_api.py`
- 已做到：
  - 创建支付单
  - 确认支付单
  - 成功后给钱包充值
  - 生成交易记录和账本
- 没做到：
  - 第三方支付网关
  - 回调签名验证
  - 自动核验支付成功
  - 前端支付页面
- 判断：
  - `支付域已实现`
  - `真实支付产品未实现`

### 5.2 下单

- 真实代码位置：
  - `backend/app/services/buyer_orders.py`
  - `backend/app/api/routes/buyer/orders.py`
  - `frontend/app.js`
- 已做到：
  - 生成订单号
  - 固化时价
  - 记录购买时长
  - 签发许可证
  - 平台前端可以创建和查看订单
- 没做到：
  - 下单扣款
  - 冻结余额
  - 库存/容量锁定
  - 与支付状态绑定
- 判断：
  - `下单已实现`
  - `支付结算型下单未实现`

### 5.3 许可证

- 真实代码位置：
  - `backend/app/api/routes/buyer/orders.py`
  - `buyer_client/runtime/api.py`
  - `buyer_client/web/index.html`
- 已做到：
  - 平台前端拿 token
  - buyer client 输入 token
  - token 可驱动订单型 shell session
- 真实边界：
  - redeem 接口本身不要求登录
  - 但真正 start-session 仍要求订单归属当前 buyer
- 判断：
  - `许可证已实现`

### 5.4 Runtime Access

- 真实代码位置：
  - `backend/app/api/routes/buyer/runtime_sessions.py`
  - `backend/app/services/session_gateway_template.py`
  - `buyer_client/agent_server.py`
  - `buyer_client/runtime/gateway.py`
  - `buyer_client/web/index.html`
  - `seller_client/windows_gateway_bridge_manager.py`
  - `seller_client/windows_session_gateway_host.py`
- 已做到：
  - 创建 runtime service
  - 创建 gateway service
  - redeem connect code
  - gateway handshake
  - WireGuard bootstrap
  - exec / logs / files / shell
  - 停止 / 续租 / 状态查询
- 判断：
  - `runtime 消费链路已实现`

### 5.5 计费

- 真实代码位置：
  - `backend/app/services/usage_billing.py`
  - `backend/app/models/runtime.py`
  - `backend/app/models/buyer.py`
- 已做到：
  - 对 offer 型 session 按小时计费
  - 写 usage charge 和 wallet ledger
  - 余额不足时停 session
- 没做到：
  - 对所有 session 统一收费
  - 下单时预授权 / 冻结 / 预扣
- 判断：
  - `usage 计费已实现`
  - `交易结算闭环未实现`

## 6. 当前最值得注意的缺口

1. `下单` 和 `支付` 仍然是两条分离链路。
   充值成功不等于下单支付成功，下单也不会消费 payment order。

2. `直接 runtime session` 仍然存在。
   buyer 仍可不经过订单，直接用 `seller_node_key` 或 `offer_id` 创建 session。

3. `ad hoc session` 当前不一定进入 usage billing。
   `usage_billing.py` 只对 `image_offer_id is not None` 的 session 扣费。

4. `frontend` 没接新的支付 API。
   所以“后端有支付域”和“平台前端可充值支付”不能画等号。

5. seller 自动上架链路还不稳定。
   “push 完新镜像立刻 buyer 可见”这件事，代码在写，但仓库内最新闭环证据显示它仍可能失败。

6. 部分字段目前只是“为未来完整交易预留”。
   典型包括：
   - `BuyerWallet.frozen_amount_cny`
   - `BuyerOrder.payment_status`
   - `BuyerOrder.paid_at`

## 7. 本次已验证到什么程度

### 7.1 已跑通的测试

- `backend/tests/api/test_payments_api.py`
- `backend/tests/api/test_auth_platform.py`
- `seller_client/tests/test_windows_gateway_bridge.py`
- `backend/tests/services/test_swarm_manager.py`

本次本机结果：

- 支付 API：通过
- runtime session 创建 / redeem / handshake / WireGuard / renew / stop：通过
- session gateway template：通过
- seller Windows gateway bridge metadata 测试：通过

### 7.2 没有在本次重新人工跑的内容

- 没有重新手工点完整 seller UI -> platform UI -> buyer UI 闭环
- 这部分只引用仓库内现成文档：
  - `docs/completed/e2e/seller-to-buyer-ui-closed-loop-2026-04-02.md`

## 8. 最后一句话总结

当前仓库已经不是“支付缺失、gateway 纯占位、buyer 只能本地 exec”的旧状态了。

更准确的现状是：

- `卖家接入`、`买家 runtime 消费`、`gateway + WireGuard`、`CodeX 编排` 都已经有真实代码和一定测试支撑；
- `支付` 已经形成了钱包充值域；
- `下单`、`许可证`、`订单型 session` 已经连起来了；
- 但 `下单支付结算闭环`、`统一权限约束`、`seller 新镜像自动上架稳定性` 仍然没有完全做完。
