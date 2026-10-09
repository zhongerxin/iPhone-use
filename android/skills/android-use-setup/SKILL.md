---
name: android-use-setup
description: 配置 Android Use 的 ADB、Python 执行环境和 UI Automator 服务，选择真实 Android 手机，处理 USB 授权、服务恢复及 Android 11+ 无线调试配对。
---

# Android Use Setup

目标是 setup 服务就绪及 ready=true，不能将 ADB 在线直接视为可操作。

1. 调用 android_use 的 pua_setup(action=status)。查看依赖、devices、配置和 job。
2. 单台在线设备 start 可自动选定；多台设备先让用户指定，再 configure(serial=发现的实际序列号)。不使用作者的设备 ID。
3. unauthorized：请用户解锁手机允许 USB 调试。无设备：检查数据线、USB 口及开发者选项。密码、系统授权由用户亲自完成。
4. start 一次，健康服务复用；有 job_id 时轮询 status(job_id,wait_seconds=20)，不重复启动。失败查看私有 state/setup.log 并解决具体原因。
5. ready(screenshot=false)，必要时 observe both。手机锁定请用户解锁，再 ready。认证暂停不能自动恢复。
6. ready 成功会打开屏幕；当前聊天未加载新工具时明确需要重连，不声称侧边栏已打开。CLI 可继续真机验证。

依赖：macOS、Python 3.9+、Google ADB、固定 uiautomator2/adbutils/Pillow，安装入口是仓库 scripts/install_android.sh。
安装使用独立 ~/.local/share/android-use/venv，不修改 iPhone 环境。可用 ANDROID_USE_HOME 与 ANDROID_USE_ADB 覆盖路径；MCP 启动脚本同样读取 ANDROID_USE_HOME。
start 通过 uiautomator2 部署并启动固定版本随附的 u2.jar，不需要 Xcode、Android Studio、root 或手机端签名账号。
工具动作走单次 JSON-RPC，不走 uiautomator2 自动重试包装。
stop 停本地预览、移除本进程 ADB forward，并要求本安装启动的后台 worker 退出；不结束外部启动的共享服务、不卸载软件。

## 无线

Android 11+ 用户在开发者选项打开无线调试，手机显示配对地址和六位码：setup pair(address,code)，再使用调试地址 setup connect(address)，discover 后 configure 选中新的设备地址。配对端口和连接端口可能不同。
Android 9 三星仅验证 USB；不自动启用旧式无认证 tcpip 5555。
本版主要在 macOS / 三星 SM-G9500 Android 9 验证，其他厂商锁屏字段、自绘控件及系统权限仍需实测。
