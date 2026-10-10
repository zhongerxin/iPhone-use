---
name: iphone-use-setup
description: 配置 PUA（Phone Use Agent），在用户自己的 iPhone 上安装、签名并启动手机端执行服务；本对话首次使用先直接 setup，再验 READY，服务未启动时复用配置与构建启动，诊断 USB 和 Xcode 连接并打开手机屏幕侧边栏；适用于首次配置、冷启动、断线恢复或签名过期。
---

# 把 iPhone 配置为 READY

使用本插件的 MCP 工具完成可重复的探测、构建、启动和连接；不要在每次任务中临时生成 Python 客户端。已有可用 PUA 时先复用，避免每个任务都重建或重装。

## 先确定当前状态

本对话首次使用手机、尚未取得 READY 时，先直接调用 `pua_setup(action="status")`，按下节复用健康服务或活动工作，缺少服务才 start 一次；服务就绪后调用 `pua_ready(recover=true, screenshot=false)`，再执行用户手机任务。不要先 READY 失败再 setup；不以之前聊天的 READY 或已安装插件代替当前证明。本对话已 READY 且通道未失效时直接继续，不为每项任务重复 doctor、discover、build 和 start。首次配置或 READY 返回具体缺项时，使用 `pua_doctor` 与 `pua_setup(action="discover")` 读取 Xcode、已连接设备、签名、端口及进程状态。多台 iPhone 按用户提供的设备选择；只有一台符合条件时可以直接使用。设备 UDID、Team ID、日志和签名配置保存在用户本机，不写进源码或 Git。

正常任务调用 `pua_ready(recover=true)` 或省略 recover 使用默认 true，不因预检或谨慎主动关闭恢复。只读诊断或用户明确禁止重启时传 `recover=false`，保留限制。常规任务无需截图时可传 `screenshot=false`；截图能力待实际需要截图时再使用。返回 `ready=true, state="ready"` 的 proof 包含 PUA status、可用会话、真实前台 App、设备视口、解锁状态与当前观察；直接用嵌套 observation 准备下一步，不立即重复 observe 或再做一轮导航测试。若镜像占用且控件树为空，工具返回 `mirroring_conflict`，退出镜像后重验 READY。Runner 图标、BUILD SUCCEEDED 或端口开放不足以声明 READY。READY 同时关联手机屏幕侧边栏，宿主支持时默认打开；已有通道要重新打开画面用 `pua_screen()`，不要重复 READY。预览走独立 USB MJPEG 通道（默认设备端口 9100），不使用 XML 或截图轮询；预览不可用本身不否定控制通道 READY，也不要求循环重启。READY 证明控制通道可用；常规 App 操作默认乐观执行，下一步所需观察顺带判断进度，最终关键结果才显式验收。

`phone_locked` 会自动暂停预览并记录 `device_locked`，无需再显式 pause。等待用户实际解锁通知后重验 READY；成功时仅解除同一次锁屏暂停，App 认证与旧版未知暂停保持显式恢复。READY 的 `preview` 返回暂停原因；不要把预览空白当成控制通道失效。

## 服务未启动时继续初始化

`recover=true` 对持久 `local.pid.0` / XCTest 故障可排队恢复已核验归属的服务；它不代表自动完成首次配置或冷启动。READY 的 `pua_unreachable`、连接拒绝、`not_ready` 等未启动结果，应进入以下启动分支，不能直接 final 宣布手机任务失败：

1. 调用 `pua_setup(action="status")`。返回 `configured=true` 时保留现有设备、签名和端口配置；`configured=false` 才按下节首次安装流程补实际缺项。
2. 检查返回 `jobs` 数组。已有与当前配置 / endpoint 对应的 start / recover 工作为 queued 或 running 时按其 `id` 查询同一工作，不重复 start，也不等待旧的无关工作；有对应 fetch / build 工作正在运行时也先复用它。`pua_setup(action="status", job_id=...)` 仍返回 `jobs` 数组，不能假定返回单个 job。
3. `service.ready=true` 表示服务可探测，仍需重新 READY 验证 session / 前台 / 视口等完整通道。start 工作仍 running 且服务可用即可验 READY；recover 工作到 `recovery_phase="serving"` 后验 READY，长期 Runner 不等 succeeded。
4. 已配置、服务未运行且没有对应活动工作时，调用一次 `pua_setup(action="start")` 复用有效构建。start 默认有界等待最多 20 秒，服务就绪或工作结束时提前返回；不是固定 sleep。返回 `service.ready=true` 时直接 READY，无需再查 status。尚未就绪时用 `pua_setup(action="status", job_id=..., wait_seconds=20)` 等待同一工作，超时只表示仍在启动，不能再次 start。新工作记录返回的 `job_id`；`already_running=true` 时记录 `job.id`，需要等待时用 `pua_setup(action="status", job_id=..., wait_seconds=20)` 查询同一工作。start 明确报告源代码缺失时 fetch，明确报告有效构建缺失、签名过期或二进制不兼容时 build，然后继续 start；不因冷启动先完整重装或重新签名。工作失败时查看该工作准确日志和 next_steps，再处理真实缺项，不能循环 start。
5. 取得 `ready=true` 后复用当前 observation，继续原任务。只有真实 USB / Xcode / 签名 / 权限阻塞才需要对应处理；必须用户本人解锁、信任、登录或确认时，用首个选项「已完成继续」的宿主提问流程。用户明确要求只读、禁止启动 / 重启时保留限制，不通过初始化绕过。

本聊天已有 widget 时，重跑 setup、启动 / 恢复通道和再次 READY 均复用已有面板，不再额外调用 `pua_screen()` 打开新标签。用户只要求“先打开 widget 让我看”且面板尚未打开时，调用 `pua_screen()` 打开预览后继续初始化与已授权任务，不添加等待批准的关卡；用户明确要求先等确认则照做。未启动时预览暂时空白不证明 READY，也不表示整个任务已失败。

## 首次安装

1. 根据 doctor 的具体缺项引导用户准备匹配 iOS 的完整 Xcode、接受首次启动许可，并在 Xcode 的账户设置登录自己的 Apple Account。账户登录、密码、验证码和设备解锁由用户在系统界面完成，不索取凭据。
2. 用数据线连接 iPhone；首次配对在手机上点“信任此电脑”。从 Xcode 的 Devices and Simulators 或 discover 返回确认设备已被识别；只有 Finder 可见不代表已完成开发配对。
3. 根据实际系统提示开启“设置 → 隐私与安全性 → 开发者模式”，完成重启后的确认。若没有该入口，先让 Xcode 完成设备配对和开发准备，再重新检查；不要把缺少入口判断为永久不支持。需要时检查“设置 → 开发者 → 启用 UI 自动化”。
4. 用 `pua_setup(action="fetch")` 获取 PUA 使用的固定版本手机端执行服务，然后 `configure` 配置用户的 `udid`、`team_id` 和可签名 `bundle_id`；可选 `source_dir`、`local_port`、`device_port`，默认本机转发端口 18100、设备端口 8100。本机运行数据默认在 `~/.local/share/iphone-use`。不要照搬作者的 UDID、Team ID 或签名。自动签名失败时按返回日志查看 Runner target 的 Signing & Capabilities，选择用户自己的 Team；只有错误要求时才调整 bundle ID 或 provisioning。
5. 用 `build` 构建，然后 `start` 安装并启动 Runner 测试服务及本机 USB 转发。fetch/build/start 是后台工作；记录 job_id，用 `pua_setup(action="status", job_id=...)` 查询已有工作，从返回 `jobs` 数组按 id 找到它，不假定 status 返回单个 job。fetch / build 看终态，失败时读准确日志；start 是长期 Runner，工作仍 running 且 `service.ready=true` 时即可调用 READY，不等 succeeded、不重复 start。按状态、日志及返回的 retry_after_seconds 查询，不固定长间隔或固定次数盲等。start 使用当前配置的成功构建产物，默认保留数据线并保持设备可供 UI 测试使用。
6. 若手机或 Xcode 报开发者不受信任，按当前设备的“设置 → 通用 → VPN 与设备管理”及其开发者条目完成验证；以当前提示为准，不把企业 App 的流程套用于所有开发签名。再执行 `status` 和 `pua_ready`。

账号登录、密码、设备解锁、手机确认或 Xcode 安装确实需要用户完成时，说明准确页面、阻塞原因和完成后继续的步骤，并必须调用可用的宿主提问工具（Default 优先 `functions.request_user_input_async`），首个选项固定「已完成继续」，第二个可为「暂时无法完成」。异步返回、预选或经过一段时间不代表用户完成；收到用户实际选择「已完成继续」或明确完成回复才继续依赖该步骤的配置。只有宿主没有提问能力时才用文字等待说明。其余独立配置可以继续执行。不要为已授权的本机安装再加入统一确认关卡。不要删除用户已有开发 App、吊销共享证书或付费升级账户来绕过错误。

## READY 验收与恢复

READY 已返回设备、会话、视口和当前 observation，复用这些信息准备下一步。不要紧接着重复读树或为每项任务增加导航预检；只有具体诊断确实需要时才在无副作用页面测试。常规导航默认 observe=none、verify=false；下一步需要未知页面信息时，在本次动作返回 tree / screenshot / both，不另加验证回合。最终关键操作显式 expect / verify 或一次终态读取。

通道 READY 后，目标 App 仍可能要求密码、验证码或 Face ID。按 [认证接管与恢复](../iphone-use/references/authentication.md)，先调用 `pua_screen(action="pause")` 停止预览并清空画面，再按该参考调用提问工具提示用户在 iPhone 上完成，首个选项「已完成继续」；接管期间暂停手机调用。收到用户实际选择「已完成继续」或明确完成通知后调用 `pua_screen(action="resume")`，重新观察 App 和目标页。App 认证不是 PUA 故障，不为此重复 build / start / recover；手机真正锁屏或通道中断时才恢复 READY。

断线、重启、停止测试进程或 USB 转发退出后，按上面的未启动流程查询 status 并复用已有工作 / 构建；只有具体缺项需要时才调用 doctor，按缺失层恢复连接或 start，最后重新 READY。用户接管手机后重新观察当前 App，不继续使用接管前的坐标或猜测原页面。

`XCTDaemonErrorDomain Code=41`、`local.pid.0` 或 `pua_foreground_unavailable` 是 PUA / XCTest 通道故障，不能靠改 selector 或把 `status.ready=true` 当作可操作证明来解决：调用 `pua_ready(screenshot=false)`。它先重建会话重读一次，持续故障才后台恢复经过配置、endpoint、worker 与监听端口归属核验的本插件服务，不重放失败的导航、输入或提交。`state="recovering"` 时按 recovery.job_id 查询同一工作，recovery_phase 到 serving 后重验 READY，不重复 start、不等 succeeded；`state="recovery_required"` 表示本次关闭了恢复，仅在用户指令允许时再用 recover=true；`pua_recovery_required` 错误按其中的冷却、归属或构建原因处理，不能按端口杀进程。两种未就绪状态都没有 error，仍不表示 READY。各状态的完整处理见 [通道恢复](references/recovery.md)。

按实际日志处理签名、容量、连接、开发模式和版本问题，读取 [故障排查](references/troubleshooting.md)。恢复时复用可用的构建与 PUA；只有签名过期、二进制不兼容或构建失效时才重新 build。输出 READY 或 NEEDS_USER_ACTION，并附已经验证的层、准确缺项及下一步；没有验证完就保持未就绪。

## 官方依据

Apple 的 [开发者账户说明](https://developer.apple.com/help/account/basics/about-your-developer-account)说明 Personal Team 最多可安装 3 个 App / 设备，provisioning profile 自签发起 7 天过期，届时需要重建重装。免费账户可用于个人设备测试，不能据此承诺永久运行。

[Apple 开发者模式](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device)与 [Appium 真机准备](https://appium.github.io/appium-xcuitest-driver/latest/getting-started/device-setup/)提供配对、开发模式与签名要求。流程以当前 Xcode/iOS 的实际提示和工具诊断为准；本插件不要求越狱。

## Midscene 执行依赖

默认操作路径需要 Node.js 22.19+ 与插件目录的 Midscene SDK。`scripts/install.sh` 会安装 SDK；若工具提示缺少依赖，执行 `npm ci --prefix <plugin-root>/server/midscene` 后继续。无需配置外部模型或 API key，当前聊天模型负责看图与决策。
