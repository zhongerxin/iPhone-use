# 先读这里

这是 Windows x64 实验性预发布包，已包含 WDA 安装器 exe 和 GNU 运行库。没有签好的手机 App，也没有 Apple 账号信息。

请先看 [完整中文教程](windows/QUICKSTART.zh-CN.md)，然后进入 `windows` 文件夹，按编号运行：

1. `01-setup.cmd`：建立 Python 环境，下载和校验官方 WDA。
2. `02-install-wda.cmd`：用自己的 Apple 账号签名并安装到手机。
3. `03-register-mcp.cmd`：把控制工具注册到本机 Codex，然后新开 Codex 会话。
4. `04-start-wda.cmd`：启动 runner，再让 Codex 检查 `pua_ready`。

电脑需要完整 Python 3.12、Apple USB 驱动和已安装的 Codex；手机需要数据线连接、解锁、信任电脑及开发者模式。保持两个文件夹的相对位置，不要只复制 exe。

本包经过原生编译、MCP 和初始化流程检查。通用 WDA 标识的实际 Apple 签名安装仍待用户验证，因此标记为预发布。具体限制见 [版本状态](windows/STATUS.md)。
