# iPhone Use for 豆包工作

把 [iPhone Use](../README.md) 接入豆包工作（Doubao Work）：同一套 MCP 服务、同一套技能，让豆包工作通过 USB 操作真实 iPhone，并在侧边栏显示手机实时画面。

这个目录叠加在原版（Codex 版）和 [WorkBuddy 版](../workbuddy/README.md) 之上，不修改两者的代码。依赖与 [安装依赖](../README.md#安装依赖) 相同：macOS、完整 Xcode、USB 连接的 iPhone、Python 3.9+、Node.js 20.19+ / 22.12+ / 24+。

## 安装

```sh
python3 doubao/install.py
```

安装器把服务放到 `~/.local/share/iphone-use-doubao`，确认它能启动，然后打印下面两步要用的内容。豆包工作的连接器和技能存在账号里，只能在它的界面里添加，安装器无法代劳：

1. **新建连接器。** 「插件 · 技能 · 伙伴」→ 右上角「添加」→「新建自定义连接器」，按安装器打印的内容填写：

   | 栏目 | 填写 |
   | --- | --- |
   | 服务器名称 | `iphone_use` |
   | 传输类型 | `STDIO` |
   | 命令 | 安装器打印的 Python 绝对路径 |
   | 参数 | 添加一项：`~/.local/share/iphone-use-doubao/doubao/mcp_stdio.py` 的绝对路径 |

   环境变量不用填。保存后在「管理 > 连接器」里能看到 `iphone_use`，点开「包含的操作」应列出 `pua_observe` 等工具。
2. **上传技能。** 「添加」→「上传技能」→「选择文件夹」，分别选 `~/.local/share/iphone-use-doubao/skills/iphone-use` 和 `…/skills/iphone-use-setup`。`.local` 是隐藏目录，在文件选择框里按 ⌘⇧G 粘贴路径最快。

然后新开一个「本地电脑」任务：

> 用 iphone-use-setup 帮我配置通过 USB 连接的 iPhone，验证 READY，然后显示手机屏幕。

已经用 Codex 版或 WorkBuddy 版配置过的手机不需要重新签名和构建：几个宿主共用 `~/.local/share/iphone-use` 里的设备配置、WDA 构建和操作锁。

豆包工作自己也能操作这台 Mac 上的软件。只说「打开 QQ 音乐」时它可能当成电脑上的软件而回答不支持；说明是手机上的，或用 `/iphone-use` 点名技能。

## 与 Codex 版的差异

| | Codex | 豆包工作 |
| --- | --- | --- |
| 安装 | `sh scripts/install.sh`，经 Codex CLI 注册插件 | `python3 doubao/install.py`，再在界面里新建连接器、上传技能 |
| 工具名 | `mcp__iphone_use__pua_*` | 相同 |
| 认证接管时的提问工具 | `request_user_input_async` | `interaction.ask` |
| 截图 | 经 `functions.exec` 转发图片块 | 图片块随工具结果直接返回 |
| 手机屏幕 | 侧边栏，READY 打开并复用 | 侧边栏，`pua_screen()` 打开 |
| 面板底部的刷新 / 主屏幕 / 截图按钮 | 有 | 有 |

服务说明和技能中点名 Codex 专用工具的几段，由 [doubao_text.py](doubao_text.py) 逐段替换，替换的是 WorkBuddy 版已经列出的同一批段落；控制工具的名称、参数和行为与原版完全一致。原版改写了其中某一段，或两个版本的段落对不上时，安装器和测试会报出具体是哪一段。

## 豆包工作怎么托管这个服务

下面几条是在豆包工作 2.32.1 上实测的，入口 [mcp_stdio.py](mcp_stdio.py) 按它们来适配：

- **一个常驻进程。** 连接器启用时豆包工作启动一个服务进程，一直用到连接器关闭或应用退出，所有任务共用。进程不在沙箱里，能访问 USB、Xcode 和 `~/.local/share/iphone-use`。
- **PATH 由安装器补齐。** 连接器表单里不填环境变量。安装器把 node、npm、git、Xcode 命令所在目录和「本地回环不走代理」记在安装目录的 `doubao/host.json`，入口启动时补进环境。换了 Node 安装位置后重新运行安装器。匿名使用统计默认关闭（原版把所有事件标记为 Codex 插件来源），想保留用 `--analytics` 安装。
- **手机屏幕在侧边栏。** 豆包工作支持 MCP App，`pua_screen()` 的画面在右侧「MCP App」标签页里打开。原版让 READY 也带画面，入口去掉了这一处，并在说明里告诉模型：本对话第一次 READY 后调用一次 `pua_screen()`。
- **面板自己的工具调用会被转发。** 所以取帧和底部三个按钮都按原版的方式工作，不需要 WorkBuddy 版那套资源读取和独立画面进程。
- **画面每秒 4 次取帧，按采集节奏回放。** widget 每 250 毫秒取一次帧。入口把手机端画面流设为每秒 30 帧、半尺寸，并换用 WorkBuddy 版的回放逻辑：每次应答带回上次之后的所有帧，页面按采集时的间隔依次画出来。帧率与手机出帧一致（iPhone 17 Pro Max 上约每秒 28 帧，上限在手机端），画面比手机慢约 0.3 秒。
- **不能像 WorkBuddy 版那样直连取帧。** 豆包工作只允许 MCP App 页面访问 `https:` 和 `wss:` 来源，本机回环上的 `http` 地址不在其中，所以这里不开任何本机端口。
- **手机端服务不随应用退出。** 与 WorkBuddy 版相同：保持 WDA 运行的 worker 归属 launchd，豆包工作退出或连接器关闭后仍在，下次不用重新启动。需要停止时用 `pua_setup(action="stop")`。

使用期间手机要保持解锁亮屏；长时间使用时把自动锁定设为「永不」。

## 排查

连接器或画面不对时，可以让服务记下豆包工作向它发了什么（只记方法名、工具名和资源地址，不记参数、结果和画面）：

```sh
mkdir -p ~/.local/share/iphone-use/doubao && touch ~/.local/share/iphone-use/doubao/trace
```

把连接器关闭再打开，重现问题，然后看 `~/.local/share/iphone-use/doubao/host.log`：`start` 一行是进程启动时的环境（能否访问 USB、node 在哪），之后每行是一次请求，`pua_screen_frame` 每 200 次记一行。删除 `trace` 文件并重启连接器即停止记录。

## 更新与卸载

```sh
git pull --ff-only
python3 doubao/install.py
```

更新后把连接器关闭再打开（「管理 > 连接器」里的开关），让豆包工作换用新代码；技能内容有变化时重新上传。卸载：

```sh
python3 doubao/install.py --uninstall
```

这只移除安装目录；连接器和两个技能在豆包工作的「管理」里删除，`~/.local/share/iphone-use` 中的手机配置与构建保留。

## 测试

```sh
python3 -m unittest discover -s doubao/tests -v
```

测试在临时目录里完整安装一遍并启动服务，手机的 USB 画面流用一个替身脚本，不接触豆包工作和真实手机。
