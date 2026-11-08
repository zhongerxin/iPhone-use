# 启动与恢复

READY 没有返回 `ready=true` 时按返回的状态继续，不把这次结果当作整个任务失败，也不重放可能已经生效的业务动作。只读、禁止启动 / 重启等用户限制始终保留。

## 本对话首次使用

本对话尚未使用 iPhone Use、没有有效 READY 时，先直接调用 `pua_setup(action="status")`。服务健康则直接 READY；有活动工作则等待同一 job；已有配置且没有服务或活动工作才 start 一次。不要先调用 READY、等失败再 setup。本对话已有健康 READY 则直接继续任务。

start 默认最多等待 20 秒，服务就绪或工作结束时提前返回，不固定 sleep。返回 `service.ready=true` 则直接 READY，无需再查 status；否则记录 job_id，调用 `pua_setup(action="status", job_id=..., wait_seconds=20)` 等待同一工作。超时不会取消工作，也不授权重复 start；失败则读具体日志。recover 需到 `recovery_phase="serving"` 才验 READY。setup 的服务探测不替代完整 READY，用户的只读 / 禁止启动限制始终保留。

## 超时不等于断连

`pua_timeout` 表示 socket 等待或调用预算耗尽，手机端工作可能仍在执行，不能推断 USB 拔出或服务停止，也不立即重启、重读整树或重放动作。

- READY 超时：按返回的 `retry_after_seconds` 在宿主侧等待（当前为 2 秒），再用相同参数重试 READY。最多两次延迟重试；仍超时才调用 `pua_setup(action="status")` 查看服务、工作与日志，不直接 start / recover。
- 普通读取或动作后的观察超时：宿主侧等待后只取一次 `pua_observe(mode="screenshot")`；仍超时最多再延迟尝试一次，再用 setup status 诊断。不要用 `pua_wait` 等待，它会新增控件查询；不要同时启动其他手机请求。
- `action_uncertain` 或 `action_executed=true, action_complete=false`：按 `transport_error` 和 recovery 先等待或检查连接，然后观察真实状态，只继续未完成的部分。不能因超时重复点击、输入、发送、支付或整个批次。

重复超时本身不授权恢复或重启；setup 诊断确认服务未运行后才按下面的启动流程处理。`pua_transport_error` 是其他传输 / 协议异常，保留诊断信息，不按断连自动重启。`pua_unreachable` 表示 HTTP 通道连接拒绝、重置、管道关闭或 EOF 等连接失败，进入 setup 检查；它仍不证明物理 USB 已拔出。

## 服务未启动

`recover=true` 可恢复已核验归属的失效 XCTest 通道，但不自动完成冷启动。READY 返回 `pua_unreachable`、连接拒绝、`not_ready` 或明确服务未启动时，先检查连接与服务。`initialization_required=true` 及 recovery 的 next_tool / next_arguments 指向初始化下一步，不表示已自动启动；不要把 `pua_timeout` 或 `pua_transport_error` 直接归入这个分支：

1. 调用 `pua_setup(action="status")` 查看 `configured`、`service` 和 `jobs`。
2. 已有与当前配置 / endpoint 对应的 start / recover 工作为 queued 或 running 时，记录其 `id`，用 `pua_setup(action="status", job_id=..., wait_seconds=20)` 查询同一工作；从返回的 `jobs` 数组匹配 id，不复用旧的无关工作。恢复到 `recovery_phase="serving"` 或启动服务达到 `service.ready=true` 后重新调用 READY，长期 Runner 可以保持 running，不等 succeeded，也不再 start。若对应 fetch / build 工作正在运行，先复用并查询该工作，再继续缺失步骤。
3. `configured=true`、确认服务未运行且没有可复用的活动工作时，调用一次 `pua_setup(action="start")`，复用现有签名构建。探测超时或单独的 `service.ready=false` 不足以证明服务未运行，先按返回诊断和日志确认。根据实际返回记录新工作的 `job_id`，或 `already_running=true` 时的 `job.id`，再查询同一工作。只在返回明确提示源代码 / 构建缺失或失效时按 `iphone-use-setup` 补 fetch / build；不因服务未启动先重建、重新签名或重装。若 `service.ready=true`，直接重验 READY，无需 start。
4. `configured=false` 时读取 `iphone-use-setup`，按实际缺项完成 doctor / discover、fetch、configure、build / start。真实 USB、Xcode、信任、开发者模式或签名阻塞才转入对应处理；用户本人需解锁、登录或确认时用 [认证接管](authentication.md) 的提问流程。
5. READY 返回 `ready=true` 后复用它的 observation 继续原任务。缺项需用户完成或存在真实启动失败时才报告准确阻塞。

服务还未启动时 widget 可能暂时空白，打开或留空既不证明 READY，也不是整项任务失败。

## 通道恢复中

- `ready=false, state="recovering"`：按 recovery 的 job_id 和 status 参数查询同一工作。从返回的 `jobs` 数组找到该工作，recovery_phase=serving 后重验 READY；长期 Runner 可以保持 running，不等 succeeded，不重复 start。
- `ready=false, state="recovery_required", reason="recovery_disabled"`：仅在用户指令允许恢复时按 next_tool / next_arguments 调用 recover=true；明确禁止重启则保留限制并报告阻塞。

两种状态没有 error、MCP isError=false，仍不表示手机可操作。实际恢复拒绝、冷却、锁屏等按返回原因处理；按工作状态与返回的 retry_after_seconds 查询，不用固定长 sleep 或固定次数空轮询。

## 屏幕预览

widget 通过独立 USB MJPEG 通道显示最新手机画面，界面最多每秒读取 4 次服务端缓存，不轮询截图或 XML，也不占手机操作锁；长时间的手机操作期间预览照常刷新。首次操作后边缘渐变持续显示，认证暂停、断开 / 换流、关闭 / 重新加载后清除；圆形 cursor 标记实际点击位置或拖动路径，二者都不证明动作成功。widget 隐藏 / 关闭后停止轮询，预览租约 5 秒到期；认证暂停会停止采集，widget 保留暂停前的画面并叠加半透明接管遮罩，恢复必须等用户明确确认。更新插件后，已有聊天可能仍运行旧 MCP 进程和旧页面；重连聊天并重新打开 widget 加载新版，不能靠重复 READY 刷新缓存。`paused=true` 时显示「请在手机上接管操作」，只有用户确认完成后才 resume。
