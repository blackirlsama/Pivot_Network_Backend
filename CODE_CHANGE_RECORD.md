# 代码修改记录

## 1. 平台前端：补充注册密码提示与前端校验

### 涉及文件
- `frontend/index.html`
- `frontend/styles.css`
- `frontend/app.js`

### 修改前
- 注册表单里的密码输入框没有明确提示“密码至少 8 位”。
- HTML 输入框本身没有声明最小长度。
- 前端点击注册后直接请求 `/api/v1/auth/register`。
- 当密码少于 8 位时，用户只能在提交后通过后端 `422 Unprocessable Entity` 才知道失败原因。
- 页面没有专门的注册反馈区域来显示成功或校验失败信息。

### 修改后
- 在密码输入框上增加了 `minlength="8"` 和 `autocomplete="new-password"`。
- 在密码输入框下方增加了“注册密码至少 8 位”的静态提示。
- 在认证区域增加了 `#auth_feedback` 反馈节点。
- 新增 `.field-note`、`.feedback`、`.feedback.info`、`.feedback.error` 样式。
- 在前端脚本中新增 `setAuthFeedback()`。
- 在调用后端注册接口前增加前端校验：
  - 邮箱为空时直接拦截
  - 密码长度小于 8 时直接拦截
- 注册成功和登录成功后会显示明确提示。
- 退出登录后会把提示恢复为默认密码规则说明。

### 修改原因
- 后端真实约束就是密码长度至少 8 位。
- 前端原先没有把这条规则展示给用户，导致用户只能通过后端报错倒推原因。
- 这次修改的目的，是把最常见的注册失败前移到前端，并在提交前就把规则讲清楚。

## 2. Seller 前端：修复脚本语法错误导致按钮失效

### 涉及文件
- `seller_client/web/index.html`

### 修改前
- 页面内联脚本里有一段正则：

```js
/connection refused|timed out|i\\/o timeout|no such host/i
```

- 这段内容在当前 HTML 内联脚本上下文里会触发 JavaScript 解析错误。
- 因为脚本在加载阶段就报错，页面里的函数没有正常注册，所以 seller 页面上的按钮点击后看起来“没有任何反应”。

### 修改后
- 将该正则改为：

```js
/connection refused|timed out|i\/o timeout|no such host/i
```

- 页面脚本可以正常解析并执行，按钮绑定的函数也能正常工作。

### 修改原因
- seller 页面上的按钮依赖整段前端脚本成功加载。
- 原先是脚本语法错误导致整页交互失效，不是按钮逻辑本身没有写。

## 3. Seller Client：让主机磁盘信息采集在 Windows 下避开 `psutil.disk_usage()` 崩溃

### 涉及文件
- `seller_client/agent_mcp.py`
- `seller_client/tests/test_agent_mcp.py`

### 修改前
- `host_summary()` 中原本使用：

```python
disk_root = Path.cwd().anchor or str(Path.cwd())
disk_usage = psutil.disk_usage(disk_root)
```

- 在当前 Windows 环境里，这里会抛出：

```python
SystemError: argument 1 (impossible<bad format char>)
```

- seller onboarding 在注册节点能力时会调用 `host_summary()`，因此会直接在接入流程里返回 `500`。
- 原先没有测试覆盖这种 Windows 环境下的异常行为。

### 修改后
- 改成：

```python
disk_path = str(Path.cwd())
try:
    disk_usage = psutil.disk_usage(disk_path)
except Exception:
    disk_usage = shutil.disk_usage(disk_path)
```

- 新增回归测试 `test_host_summary_falls_back_to_shutil_disk_usage(...)`。

### 修改原因
- 在当前环境里，`psutil.disk_usage()` 对实际路径不可用，但 `shutil.disk_usage()` 可以正常工作。
- 这类磁盘 API 的环境兼容问题不应该把 seller onboarding 整体打崩。

## 4. Seller Client：允许 WireGuard 提权 helper 在清理旧结果文件失败时继续执行

### 涉及文件
- `seller_client/agent_mcp.py`
- `seller_client/tests/test_agent_mcp.py`

### 修改前
- `_run_windows_wireguard_helper()` 在写完请求后会执行：

```python
if result_path.exists():
    result_path.unlink()
```

- 当前 Windows 环境下，删除：

```text
C:\ProgramData\PivotSeller\wireguard-elevated\result.json
```

会抛出 `PermissionError: [WinError 5] Access is denied`。
- 这个异常发生在真正调用提权任务之前，所以 seller onboarding 会直接返回 `500`。
- 原先没有针对这个权限场景的回归测试。

### 修改后
- 将旧结果文件清理改成“尽力执行，失败只记 warning，不中断流程”：

```python
cleanup_warning: dict[str, Any] | None = None
if result_path.exists():
    try:
        result_path.unlink()
    except OSError as exc:
        cleanup_warning = {
            "warning": "wireguard_helper_result_cleanup_failed",
            "result_path": str(result_path),
            "error": str(exc),
        }
```

- 在轮询读取结果文件时，也允许 `OSError` 和 JSON 读取异常继续重试。
- helper 成功时，返回值里会携带 `cleanup_warning`，而不是直接失败。
- helper 超时时，返回值里也保留 `cleanup_warning`。
- 新增回归测试 `test_windows_wireguard_helper_tolerates_result_cleanup_permission_error(...)`。

### 修改原因
- 删除旧 `result.json` 只是清理动作，不是执行提权 helper 的必要前置条件。
- 原先因为清理失败就整个中断，是把“非关键步骤”错误地当成了“主流程失败”。

## 5. 后端：保留 Swarm 任务的真实错误信息，增强镜像自动上架失败诊断

### 涉及文件
- `backend/app/services/swarm_manager.py`
- `backend/tests/services/test_swarm_manager.py`
- `backend/tests/api/test_auth_platform.py`

### 修改前
- 在 Swarm 远端校验和 probe 逻辑里，任务失败时只抛出任务状态字符串：

```python
if "Failed" in last or "Rejected" in last:
    raise RuntimeError(last)
```

- 所以当远端任务被拒绝时，后端报错通常只剩下这种模糊内容：

```text
Rejected 2 seconds ago
```

- `docker service ps` 里真正的 `Error` 字段内容被丢掉了。
- 结果就是本地镜像推送虽然成功，但 `/api/v1/platform/images/report` 返回 `502` 时，诊断信息不够，无法直接看出真正拒绝原因。
- 原先没有专门的测试保证这类错误详情会被保留下来。

### 修改后
- 新增辅助函数：

```python
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
```

- 在校验和 probe 的失败路径里，改为同时拼接 `CurrentState` 和 `Error`：

```python
error = (current.get("Error") or "").strip()
detail = f"{last} | {error}" if error else last
raise RuntimeError(detail)
```

- `inspect_swarm_service()` 的返回结果中新增：

```python
"current_task_error_detail": _task_failure_detail(current) if current else ""
```

- 新增回归测试 `test_task_failure_detail_includes_service_error_text(...)`。
- 新增 API 回归测试 `test_image_report_persists_image_when_auto_publish_probe_fails(...)`，验证：
  - `/api/v1/platform/images/report` 在自动 probe 失败时返回 `502`
  - 响应里保留真实拒绝原因
  - 即使自动上架失败，镜像记录本身仍然已落库并可查询

### 修改原因
- 实际失败发生在“镜像已推送成功之后”的后端自动 probe 阶段，而不是 push 阶段。
- 如果只保留 `Rejected 2 seconds ago` 这种模糊状态，后续定位是缺镜像、拉取失败、权限问题还是网络问题都很困难。
- 这次修改的目的，是把 Swarm 真实拒绝原因直接保留下来，减少后续排查成本。
