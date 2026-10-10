---
name: iphone-use
description: 通过 PUA（Phone Use Agent）MCP 工具高效操作真实 iPhone；本对话首次使用先直接 setup 检查并启动服务，再取得 READY，服务未启动时沿 setup 启动流程继续。默认乐观执行导航、点击、输入和滚动，在下一步观察时顺带判断进度，只对关键最终结果显式验收；指导 App 查找、列表采集、手机屏幕侧边栏及密码或 Face ID 认证接管。
---

# 用 PUA 完成 iPhone 任务

新对话没有本对话的有效 READY 证明时，先执行下面的初始化流程，再开始手机任务；此前聊天成功、安装了插件、打开 widget 或看到 Runner 图标都不替代本对话 READY。PUA 服务尚未启动是需要继续处理的启动步骤，不能据此提前结束用户任务。已有配置默认复用，只启动缺失服务，不整套重装。

先确定用户要交付的结果和完成条件。默认把一次正常返回的动作当作已按计划执行，继续准备下一步；不要为每次点击、切页、Home、启动或输入专门调用观察来证明成功。下一步需要页面信息时，读取一次并顺带判断上一步是否生效；明确未生效再调整或重做。最终关键操作、发送或提交结果、关键文本以及交付文件需要有实际结果证据。

工具的 `ok`、`verified`、`complete` 及宿主的 completed 只描述对应调用，不代表整个任务完成。`action_complete` 表示该命令处理完毕，不是业务结果已验收。正常返回的 `verified=false` 表示没有单独验收，不是失败，也不要求停下。按已知路径继续，直到全部完成条件满足、关键结果已核对并完成交付；不能用“接下来读取……”提前结束。较长任务持续给出简短进度。

## READY 与认证

正常任务在本对话首次使用手机时，先直接调用 `pua_setup(action="status")`，按 [启动与恢复](references/startup.md) 复用健康服务或活动工作，缺少服务才 start 一次；服务就绪后调用 `pua_ready(recover=true, screenshot=false)`，不要先等 READY 失败才 setup；文字任务保留 status / session / tree / viewport / 解锁检查，无需额外截图。已有本对话 READY 且通道未失效则直接复用，不为每步重验。`recover=false` 仅用于用户明确禁止重启或明确要求只读诊断，不能因谨慎主动设置或自动覆盖用户限制。

- `ready=true, state="ready"`：通道已可用。直接复用 READY 中的 `observation` 准备下一步，不立即重复 observe，也不再加一轮 doctor、观察或导航预检。
- `ready=false, state="recovering"` 或 `state="recovery_required"`：没有 error、MCP isError=false，仍不表示手机可操作。按 [启动与恢复](references/startup.md) 查询同一工作或按用户限制处理。
- READY 返回 `pua_unreachable`、连接拒绝、`not_ready` 或明确服务未启动：这是启动分支，不是整个任务失败。`recover=true` 不会自动冷启动；调用 `pua_setup(action="status")`，复用活动中的 start / recover 工作，或在已配置且没有活动工作时 start 一次，服务就绪后重验 READY。逐步做法见 [启动与恢复](references/startup.md)，缺少配置 / 源码 / 构建时读取 `iphone-use-setup`。

只读、禁止启动 / 重启等用户限制始终保留。恢复后复用 READY 的新观察了解原任务进度，不能重放可能已经生效的业务动作。

READY 关联手机屏幕侧边栏，宿主支持时默认打开或复用本聊天已有面板；重复 setup / 恢复通道、暂停 / 恢复预览沿用同一个 widget。已有面板时直接继续，不为刷新再调用屏幕打开工具；需要重新打开已关闭的面板时调用一次 `pua_screen()`，不要为打开画面重复 READY。用户要求“先打开 widget 让我看”时先打开，再继续初始化与已授权任务；只有明确要求等他确认再操作时才等待。widget 顶部显示机型和 Live 状态，底部的刷新 / 主屏幕 / 截图三个按钮只供用户自己点击，不是模型的工具：不要调用仅供 App 使用的 `pua_screen_frame` 和 `pua_screen_action`，不要为刷新预览增加轮询、截图或 observe。用户点过主屏幕后页面会变，按下一次观察到的实际状态继续。画面留空、光效或 cursor 都不证明 READY、动作成功或任务完成，也不是模型观察。预览问题按 [屏幕通道说明](references/screen.md) 排查，不为它反复恢复控制通道。

App 实际要求密码、PIN、验证码、Face ID / Touch ID，或手机需要用户解锁时，按 [认证接管与恢复](references/authentication.md) 等用户完成。App 认证先 `pua_screen(action="pause")`；`phone_locked` 已自动暂停为 `device_locked`，不要再用显式 pause 覆盖原因。必须调用宿主提问工具（Default 优先 `functions.request_user_input_async`），首个选项固定「已完成继续」，第二个可为「暂时无法完成」。异步返回 / 预选不是用户答复；接管期间暂停手机动作、读取和截图，不索取凭据。实际完成通知后，App 认证或旧版未知暂停先 `pua_screen(action="resume")` 再取新观察；设备解锁则重验 READY，成功时仅自动解除同一次锁屏暂停。READY 的 `preview.paused` / `pause_reason` 说明预览状态；不能把 READY 成功当成 App 认证已完成。根据新状态继续剩余工作。

## 按下一步需要选择观察

已知唯一语义目标和已知导航路径使用默认 `observe="none"`，可以连续执行或合并为 batch。下一步需要辨认未知页面、选区域或读取内容时，在本次动作直接设置 `observe="tree" / "screenshot" / "both"`，用返回的嵌套 `observation` 同时规划下一步和顺带判断上一动作。不要先用 none，再专门 observe 作验证而增加一个模型回合。独立读取用 `pua_observe(mode=...)`，不要混用 mode 与 observe。

树的每个节点只保留一份事实：`type` 不带 `XCUIElementType` 前缀，`rect` 是 `[x, y, width, height]`（iPhone 点）；没有 `name` 表示它与 `label` 相同，没有 `value` 表示它与文字相同，没有 `enabled` / `visible` / `in_viewport` 表示为 true。保持 `include_invisible=false`、`max_nodes=100`、`expensive_visibility=false`；只查一个目标用 `pua_find` / `pua_wait`，不重复获取整树。树截断或节点在视口内不能证明内容全量或目标未被遮挡，固定表头和浮层可能盖住它。

自绘内容、遮挡或缺失标签需要视觉判断时才取截图。截图随同一结果以图片返回，已缩放到适合阅读的尺寸：图像像素乘以 `image.pixel_to_point` 的 `[x, y]` 得到 iPhone 点，不要按原始分辨率或 Mac 屏幕换算。screenshot 跳过 XML；both 同时提供树和图。若 App 出现分享浮层，处理当前状态，不循环重复同一路径截图。

## 界面异常时先看截图，再决定操作

截图是界面异常的通用兜底。selector / 焦点失败、滚动后仍找不到可点击目标、滚动无进展、区域被遮挡或上下文改变、输入回读不符、预期页面没出现时，先查看结果里的当前截图，再决定下一次操作；没有可用截图时取一次 `pua_observe(mode="screenshot")`。不要先重读整树、换标签写法、增加滚动次数或原样重试。树被截断、坐标点击后明显没生效、页面与预期不一致时也按此流程处理。普通 `verified=false` 仍不是异常，不要求每步截图。

截图先判断当前页面、首尾行是否移动、末尾控件 / 空白、弹窗 / 固定表头和实际可滚动区域。已见目标或任务范围边界就停止寻找；画面不再移动先处理边界或遮挡，再决定是否需要另一区域或方向。`direction` 始终是手指移动方向，up 通常查看下面的内容。用户侧栏的实时预览不替代模型实际查看工具图片。

- 点击：`tap_point`、candidates 的 `tap` 是元素位置（iPhone 点），不证明它可点击。优惠券、广告、菜单或登录浮层可能盖住后台控件；先从截图找到实际可见的关闭 / 取消按钮，处理遮挡，再点目标。
- 输入：先点截图中实际可见的输入框，再调用不带 selector 的 `pua_type_text(text=...)`。`no_focused_field` 表示没有输入；上一个点击可能被浮层拦截或落在错误位置，不能原样重复那组坐标和输入。按当前截图重新选点，下一步需要信息时让动作返回截图，以便判断页面与焦点。
- 目标在屏幕外：先向目标滑动，再按随后截图点击。等待标签或滚动查找失败也按真实画面继续，不循环相同标签。

截图像素点 `(px, py)` 换算为 `x=px*image.pixel_to_point[0]`、`y=py*image.pixel_to_point[1]` 后传给 `pua_tap`。x/y 接受 iPhone 点，不能直接传截图像素或 Mac widget 坐标。

通过 `functions.exec` 调工具时必须把图片真正转发给模型，不能 `text(result)` 输出整份含 base64 的结果：

```javascript
const result = await tools.mcp__iphone_use__pua_observe({mode: "screenshot"});
for (const block of result.content ?? []) {
  if (block.type === "text") text(block.text);
  else if (block.type === "image") image(block);
}
```

点击、输入等失败结果同样转发图片块。如果图片没转发成功，用 `view_image` 打开返回的 `image.path` / `error.observation.image.path`；不能把图片元数据或 base64 当成看过截图。

以上用于可恢复的界面定位问题。参数错误按 schema 修正，通道故障恢复 READY，锁屏 / 认证等用户完成。动作是否生效不确定时先读真实状态，不能重放输入、发送、下单或整个批次；截图足以处理的常规遮挡应继续尝试，不提前把任务交还用户。

## App 与常规动作

需要把手机中的图片、视频或其他文件传回 Mac 使用时，参照 [AirDrop 文件传回 Mac](references/airdrop.md)，发送后到下载目录找到文件即可使用。

已知并核验过的 bundle ID 可直接 launch；需要离线查常用 App 时用 `pua_apps(source="catalog", query=...)` 或 [常用 App 目录](references/apps.md)。未知或同名 App 用 `source="auto"` 查本机候选，仍无结果才查 Apple。不要连续猜 ID；招商银行主应用为 `com.cmbchina.MPBBank`。商店或目录记录不证明本机安装，`installed_verified=true` 才是安装证据。启动后准备下一步的观察会同时显示实际前台，不额外增加默认前台验收。

- `pua_launch_app`：激活一次后继续；默认 `verify=false, observe="none"`。下一步需要新页面信息时设置 observe；关键入口确需证明目标 App / 页面时显式 `verify=true` 或传 `expect`。
- `pua_tap`：优先使用 `selector`，从当前节点照抄 label / name / value / type，可加 enabled；标签很长或含会变化的时间、数字时用 `label_contains`。predicate 单独使用，保留实际标签里的换行、引号和反斜线；rect、visible、in_viewport 不是 selector 字段。同名匹配叠在同一位置（如 Cell 与其中的文字），或只有一个在屏幕内时，工具自行取该目标并在结果的 `target` 中说明；多个分开的匹配返回 `ambiguous_target` 和带 `index`、类型、位置、`tap` 坐标、`hittable` 的 candidates，按其中目标的 `tap` 坐标点击（或用同一 selector 加 `index`），不随意选第一项，也不必为此再观察。selector 失败或语义不足时用 iPhone 点坐标 x/y；observation_id 可选，提供时必须来自同一 Runtime，并仅核对 App / 视口上下文。已有页面信息足以定位时不额外读树或截图；切页、用户接管或旋转后按当前信息重新定位。
- `pua_press_button`：仅用 schema 中支持的按钮。Home 使用专用 homescreen 路径，默认 `verify=false`，正常返回即可准备下一步；明确需要主屏状态时显式 `verify=true`。MCP 绑定不可用时按 [工具故障与代码调用](references/tool-fallback.md) 继续同一已授权动作。
- `pua_swipe`：方向为手指移动方向，up 通常浏览后续内容。默认 `verify=false, observe="none"`，只执行一次手势，不读取 XML 验进展。可按已知列表传 region，不强制 observation_id 或 tree。下一步读取列表内容时顺带判断是否移动；明确没动先看截图中的入口、边界、浮层或区域。显式 `verify=true` 只检查一次手势的几何变化；无进展或上下文变化即附截图交回模型，即使传入旧参数 `max_attempts=2` 也不自动换手势重试。
- `pua_wait`：确实依赖控件出现时使用有界 timeout；无需固定长 sleep，也不为所有导航补一个 wait。`pua_scroll_find`：目标已可点击就直接返回；遮挡或歧义立刻附截图停止。一次调用最多滑一次，滚动后仍找不到可点击目标时先查看返回截图，再判断是否继续滑；`max_swipes=0` 只查找，正数即使大于 1 也不连续盲滑。找到后继续下一步，不再次重复查找同一结果。

`expect` 和 `verify=true` 是显式验收选项，用于最终关键状态或实际依赖，不是每步必填。HTTP accepted 与业务成功是不同事实；常规任务乐观继续，最终结论只依据关键结果。明确 error、输入 / 提交 uncertain 或动作部分完成时先查看实际状态，再决定剩余步骤。

## 输入与发送

直接用 `pua_type_text(selector, text)` 一次给出用户需要的完整内容，不先写测试短文本或 ASCII，也不自行拆段。selector 找不到输入框时不换写法重试：用坐标点中输入框，再调用不带 selector 的 `pua_type_text(text=...)` 向当前焦点输入。默认 `replace=true, allow_newlines=false, submit=false, verify=false, observe="none"`；普通搜索、筛选等输入后可接着做下一步，未知下一页面时在本次动作返回观察并顺带判断。需要保留已有草稿时按当前内容决定替换或追加；关键最终文本可显式 `verify=true` 核对完整字段。密码、手机解锁码和验证码由用户输入。

长文本由工具分段输入。结果为 `input_complete=false` 时文本还没输完：只带返回的 `continue_token` 再调用一次 `pua_type_text`，不重发文本、不附其他参数，期间不要点击、滑动或切页；全部输完后原调用里的 verify / submit / expect / observe 才执行。续传失效（`input_continuation_expired`）时不会输入任何内容，先读取字段的实际文字，再用 `replace=false` 只补缺少的部分。输入中途报错附带 `characters_confirmed`，同样先回读，不整段重输。

多行内容可能在聊天控件里触发 Return 发送。只有确知当前 TextView 是合适的多行编辑器时才设置 `allow_newlines=true`；不能暗中把用户要求的格式改成单行。允许换行不等于授权发送。用户已授权发送时，在发送前的一次观察或显式输入验收中核对目标会话和完整草稿，再发送一次；发送后核对最终内容和发送次数。`submit=true` 不证明提交结果，提交后以真实结果页 / 记录验收。明确未发送才补做，不根据 timeout、未单独验证或普通 `verified=false` 自动重发。

微信聊天消息尽量整理成不含换行的单条文本，再一次输入并发送，避免聊天框把回车解释为发送。确实需要多行排版时，不要把含 `\n` 的整段文本直接交给 `pua_type_text`：先把内容输入为草稿并确认焦点在微信输入框，在输入框内空白处点一下调出文本编辑 toolbar，再点击 toolbar 中的「换行」来插入换行；按需重复，完成后核对完整草稿，再单独点击一次「发送」。toolbar 未出现或没有「换行」选项时，停止并根据当前界面重新定位，不用键盘 Return 代替。此操作依赖微信当前版本和输入框实际显示的 toolbar；不能仅凭 `allow_newlines=true` 推断换行已安全插入。

## Jev 快速导航

已经配置 Jev 密钥且本对话已 READY 时，可用 `pua_jev` 执行有明确结束条件的导航、搜索与草稿任务，减少宿主模型回合。传入 goal、有限 max_steps 和需要的完整单行 texts；最终重要状态传 expect。未知页面由 Jev 在同一次调用内选择动作与当前编号控件。控件文字与目标会发送至 TypeSafe。先看 [Jev 接入说明](references/jev.md)；错误、认证、缺少输入文字、无进展及视觉兜底均交回宿主继续处理，不能重复整个已经执行的循环。发送、付款及删除由宿主按用户授权单独处理。

## 连贯执行与列表采集

用 `pua_batch` 合并已知短路径，最多 20 步；常规 tap、launch、Home、输入和滚动无需 expect，普通 `verified=false` 不阻断后续步骤。可以在计划末尾放一次 observe，或只在下一步需要信息的位置观察。明确错误、不确定动作或未验收的 submit 会停止；长文本未输完（`stop_reason="input_continues"`）或达到单次调用时间预算（`"time_budget"`）也会停止。按 completed_steps / stopped_at 和每步结果继续剩余步骤，不重放整个批次。未知页面、认证及动态弹窗需要新信息时再分段，不把未授权发送混入导航。

寻找未知位置的记录时，不预先把多次同方向 swipe 塞进 batch。先滑一步并读取下一步需要的页面；树截断看不见末尾或页面没有进展时取截图，再判断是否继续。已知短路径仍可合并，任务范围或列表末尾已出现就停止滚动。

`pua_collect_list(row_type="Cell", max_pages=6)` 有界采集最多 10 页，每次只滑一次并直接采集新页，复用完整树和 viewport；按目标行判断重复页，不因虚拟化列表标签全换而丢弃新页，也不额外尝试备用手势。返回 rows、pages、stop_reason，complete 始终为 false；相同 type/name/label/value 会去重，实际相同显示的记录可能被折叠。可传 end_selector 作为覆盖证据，最终按用户要求核对范围、条数、总额、日期和缺失字段；达到上限或无进展不能认定已全量。详情字段不足时进入详情读取，不要求每次进入详情都另验一次。具体关键验收见 [采集与输入验收](references/verification.md)。

## 明确异常时调整

`occluded_target` 在 click 前失败，没有发出点击，也不能推断失败点击打开了浮层。看错误附带的截图：目标可见就直接按 `tap_point` 用坐标点击；确有浮层、选择器或固定表头盖住时先处理它（真实关闭入口或面板外可安全关闭的位置），再继续。`offscreen_target` 先把目标滑入屏幕。自定义半屏面板可能没有原生 Alert / Sheet 节点，以截图为准，不根据背景树仍有按钮就认为无遮挡。

显式滚动验证返回 `no_scroll_progress` 只表示已执行手势但没有证明列表移动，不证明空列表或到底。该异常及 `scroll_context_changed`、区域被浮层阻挡都会附截图，即使请求 `observe="none" / "tree"`；已有结果截图直接复用。列表采集因重复页停止也附截图，保留已采集行和覆盖限制。先看画面再处理边界、浮层或区域，不盲目增加次数；普通手势仍不为验证而额外读树。

`action_executed=true, action_complete=false` 或 `uncertain=true` 表示动作或部分步骤可能已生效；输入、发送、支付、下单等先读实际内容 / 记录，不能重放整项操作。纯查询的短暂失效可由工具有界重读，它不等同于执行了手机动作。`local.pid.0`、`pua_foreground_unavailable` 或 XCTest Code 41 是通道故障，按 READY 指引恢复，复用新观察继续剩余任务；服务恢复不重放业务动作。

工具绑定故障时可用插件的代码入口，复用同一配置、会话和操作锁；不因 MCP 不可用就放弃，也不另建并行控制会话。`pua_metrics` 给出 HTTP 与工具耗时、每个工具返回的字节数，以及两次工具调用之间的等待（宿主、模型与用户时间之和）；优化结果以实际任务耗时和交付完整度衡量，不把省略每步验收说成业务已被证明。
