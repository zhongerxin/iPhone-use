# Windows 用户上手教程

这是实验性版本：用 Windows 和 USB 控制真实 iPhone，再把控制工具接入本机 Codex。普通用户使用 Release ZIP，不需要编译 Rust，也不需要安装 CrossCode。它仍然需要 Python、Apple USB 驱动，以及你自己的 Apple 账号完成手机端签名。

## 1. 下载正确的安装包

打开本 fork 的 [Releases](https://github.com/jebhi/iPhone-use/releases)，下载 Assets 里的 `iphone-use-windows-*.zip`。不要下载 GitHub 自动生成的 `Source code`，那是给开发者的源码包，没有预编译安装器。

解压到一个固定目录，例如 `D:\iPhoneUse`，保留里面的 `server` 和 `windows` 两个文件夹，进入 `windows`。不要只把 exe 单独拖出来，也不要从 ZIP 里直接运行。之后不要搬动目录；搬动后需要重新注册 MCP。

## 2. 装好电脑和手机前置条件

1. 电脑安装完整的 [Python 3.12.9](https://www.python.org/downloads/release/python-3129/) Windows 64 位版本。安装时保留 Python Launcher 和 Tcl/Tk；不要使用不带 Tk 的嵌入版。已安装其他 Python 也可以并存。
2. 从微软商店安装 Apple 的“Apple 设备”应用，或已有能识别 iPhone 的 Apple USB 驱动。先确认 Apple 设备应用能看到手机。[Apple 官方说明](https://support.apple.com/zh-cn/108794)。
3. 手机用支持数据传输的 USB 线连接电脑，解锁并点“信任此电脑”。一次只连接一台 iPhone。
4. 在 iPhone“设置 → 隐私与安全性 → 开发者模式”打开开发者模式，按提示重启并确认。如果该选项尚未出现，先尝试下一步安装；不能出现或不能开启时，不代表所有手机都能直接跳过这一步。
5. 本机需有能执行 `codex mcp` 命令的 Codex 安装。首次安装/配置参考 [Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。使用托管 ChatGPT 的用户不能仅靠本机注册连接，需要该客户端支持本地 stdio MCP。

## 3. 按编号运行

### 01：初始化环境

双击 `01-setup.cmd`。它会建立项目 Python 环境、安装依赖、从 Appium 官方下载未签名 WDA、核对固定 SHA-256、准备待签名包并检查 MCP 能否启动。第一次需要联网，可能花几分钟。

看到 `Setup complete` 才继续。如果失败，窗口会显示原因；修复后重跑即可。脚本不会覆盖已经准备好的签名包。

### 02：签名并安装到手机

保持手机解锁，双击 `02-install-wda.cmd`，在弹出的本地窗口输入自己的 Apple 账号和密码；收到验证码时按提示输入。凭据交给本机签名进程，不写进项目日志。签名服务使用 SideStore Anisette，实际是否成功受服务可用性和 Apple 账号限制影响。

如果 Rust 安装路径失败但已生成签名包，脚本会自动改用 pymobiledevice3 重试，不需要再次登录。完成后，它会查询手机安装记录确认 WDA 存在。看到成功信息才算完成。

手机出现开发者信任提示时，在“设置 → 通用 → VPN 与设备管理”找到自己的开发者证书并信任。免费账号签名通常需要定期更新，到期后要从原始未签名包重新签名；不要直接修改已经签名的 Info.plist。

### 03：把工具接入 Codex

双击 `03-register-mcp.cmd`。看到 `Registered iphone-use-windows` 后，关闭并新开 Codex 会话，或重新加载相关扩展，让新工具加载。

它会把当前目录写入本机 Codex 配置。原配置备份只留在 `runtime`。不用手改配置，也不要自己在后台启动 `iphone_mcp.py`；MCP 客户端会管理它的进程。

### 04：启动手机控制服务

双击 `04-start-wda.cmd`。它启动 XCTest runner 和本机 USB 转发。保持 USB 连接和手机解锁。

如果提示端口已被占用，可能已有服务在运行。先让 Codex 调用 `pua_ready` 检查，不必反复双击启动脚本。仅看到进程启动，不代表 WDA 已就绪；以 `pua_ready` 返回 `ready=true` 为准。

## 4. 开始使用

在已加载 MCP 的 Codex 会话里说：

> 用 iPhone MCP 检查连接，再截一张手机画面。

确认画面正常后，可以说：

> 打开手机 Chrome，搜索 iphone。

或：

> 打开某个 App，找到某个页面，先给我看截图。

操作会实际发生在手机上。密码、验证码、Face ID、解锁由你自己完成。聊天发送需要你指定收件人或打开正确会话，并授权发送内容。你也在手动输入时，让模型先停，避免混入同一条草稿。

## 5. 常见问题

| 现象 | 怎么处理 |
| --- | --- |
| 电脑找不到手机 | 换数据线/USB 口，解锁手机并信任，确认 Apple 设备应用能识别；运行 `doctor.cmd`。 |
| 没有 Python 3.12 / `py` 找不到 | 安装完整 Python 3.12 和 Launcher，再运行 01。 |
| pip 或 WDA 下载失败 | 检查网络，保留错误信息后重跑 01；校验不符时不要继续安装。 |
| Apple 登录/签名失败 | 在本机窗口处理验证码，检查账号和签名服务；不要把密码贴到聊天里。 |
| 约 80% 安装失败 | 脚本会尝试复用签名包走 Python 安装；仍失败时保存错误信息，检查设备信任和账号应用限制。 |
| Codex 没有 `pua_*` 工具 | 重跑 03，新开会话；终端用 `codex mcp get iphone-use-windows --json` 检查注册。 |
| `pua_unreachable` | 手机解锁并重新插好 USB，让模型重新调用 `pua_ready`；必要时重新启动 runner。 |
| 签名到期 | 保留现有运行目录备份，从 `downloads` 的原始未签名 WDA 重新准备并签名。 |
| 输入多行文字时提前发送 | 某些聊天 App 把换行当发送；先确认该 App 行为，不要默认多行就是草稿。 |

Release 不包含任何 Apple 账号、设备 ID、私钥、签名描述文件或手机截图。WDA 由初始化脚本从官方来源下载。安装器 exe 没有 Windows Authenticode 签名；如果系统拦截，请先核对 Release 的 SHA-256 校验文件和来源，不要全局关闭系统防护。

## 开发者与已知限制

源码构建见 [英文 README](README.md)。当前没有手机实时侧栏和完全免配置 GUI。预编译安装器只是省去 Rust/GCC/OpenSSL 编译步骤，仍需要 Apple USB 驱动、Python 和本人签名。这个 fork 的 Release 与原项目是否接受 PR 是两件独立的事；上游尚未合并时，请使用这里的 Windows 发行包。
