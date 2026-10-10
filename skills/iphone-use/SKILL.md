---
name: iphone-use
description: 操作真实 iPhone，默认开启 Midscene 单步动作与报告，不调用内部 AI；可关闭回到 PUA，或另行开启 ChatGPT 授权的 AI 自动执行。支持持久开关、初始化、实时屏幕及认证接管。
---

# 完成 iPhone 任务

每个任务先调用一次 `pua_midscene(action="settings")` 读取持久模式，不需要设备连接、Node 或授权。AI 主动判断是否值得推荐高级功能，不等用户知道功能名称后再来询问；用户接受建议、已有明确偏好或直接要求时，附加 `mode` 切换。

面向用户只解释“Midscene 开关”和高级选项“AI 自动执行”，不要求用户选择三个技术模式。以下值仅用于工具路由。

- `off`：使用 [PUA 操作](references/pua-fallback.md)，不查授权，不生成 Midscene 报告。
- `steps`（默认）：当前聊天模型看图决策，Midscene 执行明确动作并记录报告；即使已授权，也不使用 act/assert。
- `ai`：读取 [操作与授权](references/midscene.md)，检查 auth_status，授权后使用 act/assert/wait。未授权时说明单独授权和额外用量，由用户在官方页面完成；不复制宿主凭据。

“开启 Midscene”选 steps；“开启 AI 自动执行”选 ai；“关闭 AI 自动执行”选 steps；“关闭 Midscene”选 off。授权与模式独立，登录不改变模式；关闭保留登录和报告。新安装或升级未设置偏好时使用 steps，不从授权推断开启 AI；已保存的 off 或 ai 保持不变。正在执行时等待完成再切换，不重放不确定动作。

## 主动判断与渐进引导

在理解任务后，以及拿到新的页面证据后，判断 AI 自动执行是否有明确收益。不要仅因已登录、任务中出现“点击”或操作失败就建议开启。

| 任务证据 | 应采取的引导 |
|---|---|
| 单次点击、打开 App、读取当前页面，或下一步已明确 | 直接用默认单步模式完成，不插入升级问题 |
| 目标明确、完成条件可观察，需要跨页面连续导航，并根据中间页面作判断 | 主动推荐 AI 自动执行，说明本任务可由它按页面变化规划后续步骤 |
| 用户要求验证多个可见条件，或需要可回放的独立视觉断言证据 | 主动推荐 AI 视觉核验，说明真正的 aiAssert 会额外调用模型，不能保证判断无误 |
| 用户复盘、排查已完成的操作 | 直接提供默认生成的报告和截图；不把看报告包装成需要 AI 授权的能力 |
| 断连、PUA 服务失败、设备锁屏、App 登录、结果不确定 | 先恢复连接、交还认证或观察现状；不能把启用 AI 当成修复方式，不能重做不确定操作 |

推荐必须联系当前任务，使用一句收益加一句成本说明。例如：“这次需要跨几个页面寻找目标并核验结果，建议试用 AI 自动执行，让它按页面变化规划下一步，并留下可回放的检查记录。这会额外调用授权账号的模型，首次需要 ChatGPT 授权。”再通过宿主提问工具提供“本次试用（推荐）／保持当前方式／以后默认使用”的选择。没有宿主提问工具时直接简短询问。不要要求用户先提出开启，不承诺更快、更省或必然成功。

一项任务最多主动推荐一次。用户拒绝后继续当前方式，不追问；已知用户明确关闭、不希望推荐或拒绝 AI 时尊重偏好。已有 ai 偏好且授权有效时直接使用，不重复推荐或授权。等待可选建议答复时可继续不依赖 AI 的已授权工作；未答复不算同意，保持当前模式。

接受“本次试用”后保存原模式、切换 ai，检查授权；缺少授权才打开官方流程。先做一个边界清晰的子任务，再用截图和断言检验结果，沿用同一 report_id。报告如实区分成功、失败和未完成，不把用户接受建议当成验证成功。任务后恢复原模式，并给出可供用户评估的报告；不再自动追加一次推广询问。“以后默认使用”才持久保存 ai 偏好。授权失败或取消时恢复原模式，说明情况，再按最新页面继续，绝不重放不确定动作。

用户明确要求“仅这次”时，保存原模式，临时切换；任务成功、失败或取消后恢复原模式。中断后发现未恢复时，先告知并恢复。普通切换持久保存。

## READY 与认证

本对话首次使用先调用 `pua_setup(action="status")`，复用已就绪服务或活动工作；缺少服务时按已有配置启动一次。start 最多等待 20 秒，未就绪时用 `status(job_id, wait_seconds=20)` 继续查询，随后调用 `pua_ready(recover=true, screenshot=false)`。只有 ready=true 才能执行手机任务；已健康初始化的通道直接复用。尊重用户明确的禁止启动或重启指令。

- `ready=true, state="ready"`：通道已可用。off 复用 READY 观察；steps 取一次 Midscene screenshot；ai 按任务调用 act/assert/wait。不额外调用 doctor。
- `ready=false, state="recovering"` 或 `state="recovery_required"`：没有 error、MCP isError=false，仍不表示手机可操作。按 [启动与恢复](references/startup.md) 查询同一工作或按用户限制处理。
- READY 返回 `pua_unreachable`、连接拒绝、`not_ready` 或明确服务未启动：这是启动分支，不是整个任务失败。`recover=true` 不会自动冷启动；调用 `pua_setup(action="status")`，复用活动中的 start / recover 工作，或在已配置且没有活动工作时 start 一次，服务就绪后重验 READY。逐步做法见 [启动与恢复](references/startup.md)，缺少配置 / 源码 / 构建时读取 `iphone-use-setup`。

只读、禁止启动 / 重启等用户限制始终保留。恢复后复用 READY 的新观察了解原任务进度，不能重放可能已经生效的业务动作。

READY 关联手机屏幕侧边栏，宿主支持时默认打开或复用本聊天已有面板；重复 setup / 恢复通道、暂停 / 恢复预览沿用同一个 widget。已有面板时直接继续，不为刷新再调用屏幕打开工具；需要重新打开已关闭的面板时调用一次 `pua_screen()`，不要为打开画面重复 READY。用户要求“先打开 widget 让我看”时先打开，再继续初始化与已授权任务；只有明确要求等他确认再操作时才等待。widget 顶部显示机型和 Live 状态，底部的刷新 / 主屏幕 / 截图三个按钮只供用户自己点击，不是模型的工具：不要调用仅供 App 使用的 `pua_screen_frame` 和 `pua_screen_action`，不要为刷新预览增加轮询、截图或 observe。用户点过主屏幕后页面会变，按下一次观察到的实际状态继续。画面留空、光效或 cursor 都不证明 READY、动作成功或任务完成，也不是模型观察。预览问题按 [屏幕通道说明](references/screen.md) 排查，不为它反复恢复控制通道。

App 实际要求密码、PIN、验证码、Face ID / Touch ID，或手机需要用户解锁时，按 [认证接管与恢复](references/authentication.md) 等用户完成。App 认证先 `pua_screen(action="pause")`；`phone_locked` 已自动暂停为 `device_locked`，不要再用显式 pause 覆盖原因。必须调用宿主提问工具（Default 优先 `functions.request_user_input_async`），首个选项固定「已完成继续」，第二个可为「暂时无法完成」。异步返回 / 预选不是用户答复；接管期间暂停手机动作、读取和截图，不索取凭据。实际完成通知后，App 认证或旧版未知暂停先 `pua_screen(action="resume")` 再取新观察；设备解锁则重验 READY，成功时仅自动解除同一次锁屏暂停。READY 的 `preview.paused` / `pause_reason` 说明预览状态；不能把 READY 成功当成 App 认证已完成。根据新状态继续剩余工作。

## Midscene 执行与报告（仅 steps / ai）

ai 模式且已授权时，在 READY 后用 `act, text="具体任务、约束和可观察的完成条件"` 执行；保持同一 report_id。所有 act 任务默认使用 SDK fast 与简短执行记忆；省略 planning 即可，也可显式传 `planning="compact"`。不按任务类型自动改用 balanced；仅在用户明确指定或对照测试时传 `planning="balanced"`。不向用户新增配置问题；失败后先观察现状，不切换规划后重放整个任务。优先复用返回的 `completion.summary`、`image` 和 `report`：说明覆盖完成条件且与返回截图一致时，直接汇报，不再固定调用 screenshot、observe 或 assert。说明为空、只说“完成”、缺少关键条件、与截图冲突或工具失败时，才针对缺口补查；不重放整个任务。用户明确要求独立视觉断言时仍使用 assert。completion 是执行模型的判断，不能称为独立 aiAssert。aiAct 内的输入可用 replace（替换）、clear（清空，value 为空）和 typeOnly（追加），只改任务要求的字段，不自动提交。若任务明确要等待异步页面条件，在已授权的 ai 模式中调用 `action="wait", text="可观察条件", timeout_ms=15000`，沿用同一 report_id；它调用真正的 aiWaitFor，会额外请求模型，不给每步固定追加等待。超时仅表示未在预算内确认条件。以下单步规则仅用于 steps 模式。

READY 后读取 [Midscene 操作参数](references/midscene.md)。先调用 `pua_midscene(action="screenshot")`，保存返回的 `report_id`。同一任务后续每次调用都传这个 ID，新任务使用新 ID。每次只执行一个动作，查看返回截图后再决定下一步。查询页面和断言由当前聊天模型根据截图完成；最终使用 `action="record", text="实际观察及结论", passed=true/false` 记录验收。不得为了得到成功报告而将未确认的结果记为通过。

PUA 回退的普通观察默认返回最多 200 个过滤后节点，max_nodes 可显式设置，最大 500；树被截断时按需查看截图或缩小查询范围。

坐标使用 iPhone 点：截图像素乘以返回的 `image.pixel_to_point`，不能直接使用 Mac widget 坐标。搜索栏、标签栏或键盘后可见的文字可能被遮挡，先滑动到无遮挡区域再点；页面未变化时重新观察，不反复点击同一位置。输入仅追加单行文本，不清空、不自动提交；需要替换时先通过可见控件处理原内容。发送、购买等有外部影响的操作仍需用户任务授权，不能把输入成功当成提交成功。

`ok` 或 `action_complete` 只表示单次操作完成。完整任务需要确认最终页面和实际结果。失败先查看返回的最新截图，缺失时取一次新的 `screenshot`；不因超时重放动作。用户完成认证后从实际状态继续，不能重放整个任务。steps 报告记录宿主决策，不代表独立 AI 验收；ai 模式的 assert 才是真正的 SDK AI 断言。

最终说明完成结果和未完成项，给出 `report_id` 与 HTML `report` 路径。多个独立进程的记录可累积到同一报告；普通 PUA 调用不计入该报告。保持简短进度，完成所有已授权步骤后再结束。

需要把手机文件传回 Mac 时，参照 [AirDrop 文件传回 Mac](references/airdrop.md)。

## 安装、连接和回退

`pua_ready`、`pua_setup`、`pua_apps`、`pua_screen` 继续负责连接、安装、查找 App 和预览，不参与动作规划。未知 bundle ID 用 `pua_apps` 查询，不能猜测。SDK 缺失时按指南安装，无需模型密钥。预览说明见 [屏幕通道](references/screen.md)。

off 模式直接使用 [PUA 操作](references/pua-fallback.md)。其他模式下，用户指定 PUA 或 Midscene 不支持所需操作时，说明原因再回退。不要静默切换，也不要在不确定动作后换工具重做。工具绑定不可用时按 [工具故障与代码调用](references/tool-fallback.md) 调用当前模式对应入口，保留设备锁和报告 ID。
