# iPhone Use for WorkBuddy

把 [iPhone Use](../README.md) 接入腾讯 WorkBuddy：同一套 MCP 服务、同一套技能，让 WorkBuddy 通过 USB 操作真实 iPhone。

这个目录是叠加在原版（Codex 版）之上的适配层，不修改仓库里已有的任何文件。依赖与 [安装依赖](../README.md#安装依赖) 相同：macOS、完整 Xcode、USB 连接的 iPhone、Python 3.9+、Node.js 20.19+ / 22.12+ / 24+。

## 安装

```sh
python3 workbuddy/install.py
```

然后在 WorkBuddy 里完成两步，安装器无法代劳：

1. 打开「连接器管理」，在自定义连接器里找到 `iphone_use`，点「信任」。
2. 新开一个会话，工具和技能在新会话才加载。

在新会话里输入：

> 用 iphone-use-setup 帮我配置通过 USB 连接的 iPhone，验证 READY，然后显示手机屏幕。

已经用 Codex 版配置过的手机不需要重新签名和构建：两个宿主共用 `~/.local/share/iphone-use` 里的设备配置、WDA 构建和操作锁，WorkBuddy 会直接复用。

## 安装器做了什么

WorkBuddy 没有插件命令行，安装器按它自己放本地 MCP 的约定落三处：

| 位置 | 内容 |
| --- | --- |
| `~/.workbuddy/mcp-servers/iphone-use/` | 原版安装包（服务、USB 工具、屏幕 widget）加 WorkBuddy 入口 `workbuddy/mcp_server.py` |
| `~/.workbuddy/mcp.json` | 在 `mcpServers` 里合并一项 `iphone_use`，其他服务原样保留，改动前留一份 `mcp.json.bak-*` |
| `~/.workbuddy/skills/` | `iphone-use` 与 `iphone-use-setup` 两个技能 |

写入前会用 WorkBuddy 实际使用的命令和环境启动一次服务，确认 `initialize` 与 `tools/list` 正常，失败则不改动任何文件。已存在但不是本安装器写入的同名服务或目录不会被覆盖，除非加 `--force`。

注册项里有三处是针对 WorkBuddy 的：

- **解释器和 PATH 用绝对路径。** GUI 应用启动子进程时 PATH 只有系统目录，找不到 Homebrew 或 nvm 的 node。安装器把 node、npm、git、Xcode 命令所在目录写进 `env.PATH`。换了 Node 安装位置后重新运行安装器即可。
- **本地回环不走代理。** `NO_PROXY` 包含 `127.0.0.1,localhost,::1`。
- **匿名使用统计默认关闭。** 原版把所有事件标记为 Codex 插件来源，移植版不应混入这份统计。想保留原版默认行为，用 `--analytics` 安装。

## 与 Codex 版的差异

| | Codex | WorkBuddy |
| --- | --- | --- |
| 安装 | `sh scripts/install.sh`，经 Codex CLI 注册插件 | `python3 workbuddy/install.py`，写入 `~/.workbuddy` |
| 认证接管时的提问工具 | `request_user_input_async` | `AskUserQuestion` |
| 截图 | 经 `functions.exec` 转发图片块 | 图片块随工具结果直接返回 |
| 手机屏幕 | 侧边栏，READY 打开并复用 | 侧边栏，`pua_screen()` 打开并复用 |
| 面板底部的刷新 / 主屏幕 / 截图按钮 | 有 | 没有，见下 |

服务说明和技能中点名 Codex 专用工具、或假定 READY 会打开面板的几段，由 [host_text.py](host_text.py) 逐段替换；控制工具的名称、参数和行为与原版完全一致。原版改写了其中某一段时，安装器和测试会报出具体是哪一段，不会悄悄失效。

### 手机屏幕

实时画面仍是原版的 widget，走标准 MCP App。WorkBuddy 托管它的方式有六处不同，由入口 [mcp_server.py](mcp_server.py)、画面进程 [preview.py](preview.py) 和安装时对页面的改动来适配：

- **在侧边栏打开。** WorkBuddy 默认把面板嵌在对话里，每次工具调用新增一个。工具声明里加上 `_meta.workbuddy.ui.launchSurface = "panel"` 后改在侧边栏打开，同一对话里重复调用会复用同一个面板。
- **只有 `pua_screen` 打开面板。** WorkBuddy 把每个带界面的工具当成一个独立的 App，原版让 `pua_ready` 和 `pua_screen` 都带界面，就会出现两个面板。入口去掉了 `pua_ready` 上的界面声明，并在说明里告诉模型：本对话第一次 READY 后调用一次 `pua_screen()`。
- **画面按资源读取。** widget 每 250 毫秒用工具调用 `pua_screen_frame` 取一帧。WorkBuddy 的 agent 在转发面板发起的工具调用前要做「敏感数据外发审查」，这条路径没有会话信息，审查直接失败，返回一句 `Sensitive MCP egress review is unavailable.`，请求到不了服务，面板会一直停在「连接中」。面板发起的资源读取则会被转发。所以页面里取帧的那一次调用改成读取 `ui://iphone-use/frame/<after_seq>/<last_event_id>`，入口把这个读取交给原来的取帧逻辑。只有这一个只读的取帧走这条路。
- **画面流由一个常驻进程持有。** 面板的每一次资源读取，WorkBuddy 都新起一个服务进程来回答，读完即退。原版把 USB 画面流和最新一帧放在服务进程的内存里，在这里每次读取都是一个刚启动、还没有画面的进程，面板只会显示「未连接」。所以画面流交给 [preview.py](preview.py)：它脱离服务进程运行，各次读取通过状态目录里的 Unix socket 向它要最新一帧。画面仍只在内存里，只有面板在读时才采集，30 秒没人读就退出。
- **面板直接向画面进程取帧。** WorkBuddy 每次读取都要启动一个进程，每秒只能读约 4 次。所以 [preview.py](preview.py) 另外在本机回环地址上开了一个 HTTP 端：面板第一次经 WorkBuddy 读取时得知它的地址，之后页面自己向它请求，每个请求挂起到下一帧出现为止，拿到就画。此后不再经过 WorkBuddy，画面跟手机几乎同步。入口同时把手机端画面流设为每秒 30 帧、半尺寸（原版默认 10 帧、原尺寸；面板里的手机只有三百点宽，半尺寸不损失清晰度）。
  帧率的上限在手机端：WDA 每帧都要截一次全分辨率的屏。iPhone 17 Pro Max 上实测一帧约 36 毫秒，即每秒约 28 帧；把设置提到 60、调低画质或尺寸，出帧都不会更快。
  这个 HTTP 端只监听 `127.0.0.1`，端口和路径里的令牌每次启动随机生成，令牌只通过 MCP 的应答交给面板；请求的 `Host` 不是它自己的地址一律拒绝。页面的安全策略里只为此放行了对 `http://127.0.0.1:*` 的 `fetch`，图片和脚本的来源没有放宽。
  页面连不上这个 HTTP 端时自动退回经 WorkBuddy 读取：每次读取把上次之后的帧一起带回，页面按采集时的间隔依次画出来，帧率不变，画面比手机慢约 0.3 秒。
- **去掉底部三个按钮。** 刷新、主屏幕、截图同样是面板发起的工具调用，在 WorkBuddy 里不会生效。主屏幕和截图会操作手机和剪贴板，不适合绕开宿主的审查改走资源读取，所以直接隐藏。认证暂停后的恢复由模型调用 `pua_screen(action="resume")` 或解锁后的 READY 完成。

从历史记录重新打开对话时，WorkBuddy 会把面板嵌在对话里显示，默认高度很小；页面此时会申请 680 像素高。

画面不对时先看 `~/.workbuddy/logs/mcp-apps-diag.log`：`proxyAppResourceRead.start` 里的 `uri` 在递增、`proxyAppResourceRead.ok` 的 `textLen` 有二十万左右，说明画面已经送到面板；`textLen` 只有两百多说明读取到达了但服务没有画面，这时看 `preview.py` 和 `screen-stream.mjs` 两个进程是否在运行。手机锁屏时没有画面是正常的，WDA 日志里会出现 `Not authorized for performing UI testing actions`。

### 手机端服务

原版把保持 WDA 运行的 worker 作为 MCP 服务的子进程启动。Codex 的服务进程常驻，WorkBuddy 则是每个对话启动一个，对话结束时 worker 被一并终止，下一个对话要等手机端服务重新启动。入口让 worker 经一个立即退出的启动器启动，归属 launchd，不随服务进程结束；设备配置、构建和任务记录都不变，需要停止时用 `pua_setup(action="stop")`。

使用期间手机要保持解锁亮屏。手机闲置一段时间后，XCTest 会拒绝操作和截图（任务日志里是 `Not authorized for performing UI testing actions`），这时重新启动服务也会失败（`Authentication canceled`），需要先在手机上解锁、按提示完成认证。长时间使用时把自动锁定设为「永不」。

## 更新与卸载

```sh
git pull --ff-only
python3 workbuddy/install.py
```

更新后新开会话。卸载会移除注册项、安装目录和两个技能，保留 `~/.local/share/iphone-use` 中的手机配置与构建：

```sh
python3 workbuddy/install.py --uninstall
```

## 测试

```sh
python3 -m unittest discover -s workbuddy/tests -v
```

测试在临时目录里完整安装一遍并启动服务，worker 用一个替身脚本，不接触真实的 `~/.workbuddy` 和手机。
