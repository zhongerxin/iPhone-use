# MCP 故障与代码调用

优先调用封装好的工具，默认乐观执行常规动作，下一步需要信息时顺带判断上一动作。工具绑定缺失、宿主转发错误或已确认的封装问题不等于 PUA 不可用；可对同一已授权动作使用插件代码入口。代码回退保留相同的配置、会话、操作锁和明确异常处理，不新增每步验收。

普通版提供 18 个模型工具；先查看本回合实际可调用的工具绑定（支持时用 ALL_TOOLS 或工具搜索），不能只从 tools/list、文档或先前聊天推断已经绑定。0.1.6 的 install.sh 同时注册同名标准 MCP，避免 Codex 插件共享说明预算隐藏工具，并保留完整 batch schema。新版安装后重新连接聊天；实际仍缺少所需工具时才用下面的代码入口继续，不默认把已封装操作都改成 CLI。

## 先区分错误

- schema / 参数错误：按 argument_path、unknown_fields、allowed_fields 修正一次调用。action_executed=false 表示动作未发出，MCP 绑定仍不可用时可用代码入口；不要自动忽略未知参数。
- `device_busy`：另一操作持有共享锁，等待它结束，不另建 Runtime 配置或 state directory 绕过锁。
- `stale_observation`：提供的 ID 不属于当前 Runtime，或 App / 视口上下文已改变。用下一步所需的新信息定位，不为整图哈希、文本数字刷新或固定 30 秒期限增加读图；ID 是可选上下文，不是每个坐标动作的必填许可。
- 界面异常统一先看截图：selector / 焦点失败、滚动后找不到可点击目标、无进展、区域被挡、滚动上下文改变、输入不符或预期页面未出现。先查看结果里已附截图，缺图才补一次 screenshot，再选择可见目标、处理浮层或决定是否继续滚动；不先重读树、换标签或原样重试。`tap_point` / candidates 不证明未被遮挡；输入先点可见输入框再 type_text，焦点仍失败须重新选点。叠在同一位置或只有一个在屏幕内的匹配由工具自行确定。
- `input_continuation_expired`：长文本续传已失效，本次没有输入任何内容。读取字段实际文字，用 `replace=false` 只补缺少的部分。
- `occluded_target` / `offscreen_target`：目标点击没有执行，不能推断未执行点击打开了浮层。`occluded_target` 按附带截图判断：目标可见就按 `tap_point` 用坐标点击，确有遮挡先处理遮挡；`offscreen_target` 先向目标滑动。自绘半屏面板可能没有原生 Alert / Sheet 节点，按真实截图判断。
- 显式验收的 `postcondition_failed`：动作已接收但验收条件未满足，查看实际状态再决定剩余步骤，不因验收失败自动重放动作。
- `action_executed=true, action_complete=false`、`action_uncertain`、`uncertain=true` 或动作后 timeout：至少部分动作可能已生效。`action_uncertain.transport_error` 区分下面三类传输问题，按 recovery 等待或检查连接，再观察真实字段 / 记录；不能切换代码入口后盲目再做输入、发送、提交、删除或整个批次。
- `pua_timeout`：socket 等待或调用预算耗尽，不证明断连。按返回的 `retry_after_seconds` 在宿主侧等待（当前为 2 秒），再取一次 `pua_observe(mode="screenshot")`，不要立即重试整树或动作，也不用 `pua_wait`（它会新增 AX 查询）。截图仍超时最多再延迟尝试一次，之后调用 `pua_setup(action="status")` 诊断；重复超时本身不授权重启。若原调用是 READY，用相同参数延迟重试 READY，而不是换成观察、start 或 recover。
- `pua_unreachable`：连接拒绝、连接重置、管道关闭或 EOF 等 HTTP 通道连接失败。调用 `pua_setup(action="status")`，复用当前 start / recover 工作；确认服务未运行且已配置、没有活动工作、用户允许启动时才 start 一次。连接失败本身不证明物理 USB 已拔出。
- `pua_transport_error`：其他传输或协议异常，按返回细节检查 setup status / 日志，不当作超时或确定断连，也不自动重启或重放动作。
- 查询失败不证明手机动作已执行，POST 查询接口不应按 mutation 处理；查询超时不自动立即重读，遵循上述有界延迟恢复。
- `local.pid.0`、`pua_foreground_unavailable` 或 XCTest Code 41：通道故障，调用 READY 恢复；不要通过更改 selector 或重复手机按钮处理。锁屏、签名和信任问题按 setup skill；App 密码 / Face ID 按认证接管。

通过 `functions.exec` 时逐个内容块转发：文字用 `text(block.text)`，图片用 `image(block)`；不要 `text(result)` 把截图变成 base64 文本。图片无法转发时用 `view_image` 打开 `image.path` / `error.observation.image.path`。坐标按 `pixel_to_point` 换算。

普通 `verified=false` 不是错误，不要求 observe 或停止 batch。HTTP accepted 只描述请求边界；常规路径继续，最终关键状态显式验收。

Home 使用手机端专用主屏幕端点。默认不单独等待 SpringBoard；准备下一步时的观察会显示实际状态。需要确认主屏时显式 `verify=true`，它有界核对前台；通用按键接口返回 HTTP 200 而界面未变的问题不能据此推定 iOS / XCTest 的内部原因。

## READY 的有界恢复

正常任务用 `pua_ready(recover=true)` 或省略 recover。false 仅用于用户明确禁止重启或只读诊断，保留该限制。暂时失效前台只清理旧观察 / 会话并重读一次；持续故障才重启已核验归属的本插件服务，复用有效构建。不会重放导航、输入或提交。

READY 的 `pua_timeout` 不进入重启流程：按 `retry_after_seconds` 在宿主侧等待，再用相同参数重试，最多两次延迟尝试；仍超时只查询 setup status 与日志。不得用 `pua_wait` 代替宿主等待，不因累计超时自动 start / recover。`pua_unreachable` 才进入连接 / 启动检查；`pua_transport_error` 按具体诊断处理。

`ready=false, state="recovering"` 携带 recovery.job_id 和查询参数。status 返回 `jobs` 数组，找到 id 与 job_id 相等的工作；检查 recovery_phase，从 stopping / starting 到 serving 后重验 READY。Runner 可以长期 running，不等 succeeded、不重复 start。按阶段、日志和 retry_after_seconds 查询，不写固定 15 秒 × 20 次的等待循环，也不能读取不存在的单个 job 并吞掉解析异常。

`ready=false, state="recovery_required", reason="recovery_disabled"` 时，用户指令允许才按 next_tool / next_arguments 调用 recover=true；明确禁止则说明阻塞。正常未就绪状态没有 error、MCP isError=false，仍不能继续手机动作。达到 `ready=true` 后直接复用其中的 observation 准备下一步，不紧接着重新 observe。

恢复要求配置、endpoint、worker 和监听端口归属匹配，归属不明、外部 PUA、端口冲突或缺少构建按返回步骤处理，不按端口杀进程。120 秒冷却按 retry_after_seconds 诊断，不连续重启。信任、解锁和 UI 自动化确认由用户在手机完成。

## 直接执行提供的脚本

从当前项目或已安装插件确定 `<PLUGIN_ROOT>`，不要照抄某人的版本 cache 路径。常规 Home：

```sh
python3 <PLUGIN_ROOT>/scripts/phone.py pua_press_button '{"name":"home","observe":"none","verify":false}'
```

脚本使用默认 `~/.local/share/iphone-use` 的共享会话与操作锁，返回结构化结果。下一步需要未知页面信息时在本次动作显式选择 observe；需要关键验收时显式 verify=true。命令输出只有 JSON：截图位于 `image.path`（已缩放的 JPEG，同名 `.png` 是原始截图），需要查看时用可用的图片工具打开，像素乘以 `image.pixel_to_point` 得到 iPhone 点。

只有原通道确实使用非默认目录 / URL 才传匹配的 --state-dir / --url；不得为避开 busy 换目录或并行控制同一手机。

## 写最小调用代码

宿主无法调用脚本时可导入相同 Runtime。下例保留默认配置和结构化错误：

```python
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).expanduser().resolve()
sys.path.insert(0, str(root / "server"))
from iphone_use import PUAError, Runtime

runtime = Runtime()
try:
    result = runtime.call("pua_press_button", {"name": "home", "observe": "none", "verify": False})
    print(json.dumps(result, ensure_ascii=False))
except PUAError as error:
    print(json.dumps({"error": error.as_dict()}, ensure_ascii=False))
    raise SystemExit(1)
finally:
    runtime.close()
```

两个入口绕过 MCP 绑定，仍经过共享实现。observation ID 不跨进程共享；若选择使用 ID，在同一 Runtime 中取得观察并传给动作。已知坐标或 region 可不提供 ID，不为获得许可额外读图或树。使用 iPhone 点坐标，不使用 Mac 屏幕坐标或截图像素。共享实现自身故障不会因换入口而修复，按实际异常调整；不要另建外部 PUA session 或盲目重复 mutation。

## 滚动与批次

默认 swipe 为 `verify=false, observe="none"`，执行一手势，不读 XML 验进展或自动 fallback。已知 region 可直接使用，不强制树、锚点或 ID。下一步本就需要读取列表时，在该次动作直接返回所需观察并顺带判断是否移动；默认未验证返回不等于没有移动。

显式 `verify=true` 才检查滚动进展、原生浮层和变化上下文；需要调试时在该次调用选 tree / both，复用返回观察，不重复另读。进展以列表几何变化判断，数字或轮播文本刷新不应单独作为滚动成功 / 拒绝动作的依据。原生浮层的验证保护不能识别所有自绘面板，明确遮挡后按真实状态选区域或关闭面板。

`verify=true` 发现第一次手势无进展就附截图停止，不自动换手势；旧参数 `max_attempts=2` 仍可传入但不导致自动重试。`scroll_find` 每次最多滑一次，仍找不到可点击目标就先返回截图；遮挡和歧义在滑动前即停止。先实际查看图片再判断继续、区域、方向和目标，不直接增加 `max_swipes` 重跑。

`no_scroll_progress` 不证明空列表或到底；异常结果即使请求 none/tree 也附截图，检查入口、边界、浮层及自绘内容。`scroll_context_changed` 表示手势后上下文变化，先看截图再处理；已执行部分不能当作未执行而重放。截图读取失败保留原错误与执行证据，并提示单独取图，不自行继续动作。

batch 可连续执行普通 unverified 导航和输入；明确 error、uncertain 或未验收的 submit 才停止，按 completed_steps / stopped_at 继续剩余步骤。launch 默认只激活一次；显式 verify=true / expect 才有界等前台 / 页面。关键验收失败先看实际状态，不自动再次 activate。

代码调用成功、Home 完成、某 App 读完或一个 batch 完成都只是阶段结果；继续剩余工作，最终关键结果与交付完成后才结束整项任务。
