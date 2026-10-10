# iPhone Use

**中文** · [English](README.en.md)

> Windows 用户：此 fork 提供实验性 [Windows USB + MCP 适配](windows/README.md)。原版 macOS 插件保留在仓库根目录。

![iPhone Use 在 Codex 中操作真实 iPhone 并实时展示手机屏幕](assets/iphone-use-demo.png)

让 Codex 通过 USB 操作你的真实 iPhone。用自然语言描述任务，Codex 就能打开 App、读取页面、点击、滚动、输入文字、整理列表，并在侧边栏展示手机屏幕。

iPhone Use 使用 [WebDriverAgent](https://github.com/appium/WebDriverAgent)（WDA）与 iPhone 通信，包含本地 MCP 服务、安装与使用技能，以及实时屏幕 widget。它优先复用现有连接与构建；控件定位失败时，指导模型查看截图并尝试坐标点击。

插件默认向独立的 PostHog 项目发送匿名使用统计，包括启动、工具调用、连接状态、耗时和错误类别。使用随机安装标识，不上传手机画面、输入内容或设备标识。设置 `IPHONE_USE_ANALYTICS=0` 或 `DO_NOT_TRACK=1` 并重启 MCP 服务可关闭。详见 [埋点与分析说明](ANALYTICS.md)。

**使用前，需要先在你自己的 iPhone 上安装、签名并启动 WDA Runner。** WDA 是运行在手机上的执行服务；下面的提示词和 setup 流程可以让 Codex 协助完成首次安装，已有健康的 WDA 可直接复用。

## 用一段提示词让 Codex 安装

将下面这段话复制到本机 Codex：

```text
请帮我安装和配置 iPhone Use：
https://github.com/zhongerxin/iPhone-use

先读取仓库 README 和安装脚本，检查本机 Codex CLI、Python、Node.js、npm、
完整 Xcode，以及通过 USB 连接的 iPhone。把项目放到合适的本机目录，
运行 sh scripts/install.sh 安装插件。

如果当前聊天还没有加载新工具，明确告诉我重连或新开聊天后继续。
工具可用后读取 iphone-use-setup 技能，检查现有配置，优先复用已有 WDA。
首次配置时发现我的设备，使用我自己的 Apple 开发团队和可签名 bundle ID，
获取固定版本 WDA、配置签名、构建并启动，直到 pua_ready 返回 ready=true，
然后打开手机屏幕。不要照搬作者的设备标识或签名信息。

缺少依赖时说明具体缺项并帮助安装。Apple 账号登录、设备信任、开发者模式
或解锁需要我操作时，告诉我明确步骤，等我完成再继续。
```

安装插件后，当前聊天可能需要重新连接才能加载工具，流程可在重连后继续。Apple 账号登录、设备信任、开发者模式及手机解锁仍需本人在系统界面完成。

仓库目前为私有，需要有 GitHub 访问权限，或者使用已取得的源代码包。

## 安装依赖

| 依赖 | 用途 |
| --- | --- |
| macOS 与 Codex 桌面端 / CLI | 安装本地插件、运行 MCP 服务与屏幕侧边栏 |
| 完整 Xcode，已完成首次启动配置 | 编译、签名、部署和运行 WDA；仅 Command Line Tools 不够 |
| 可在 Xcode 中使用的 Apple 账号和开发团队 | 签名手机端 WDA Runner |
| USB 连接的真实 iPhone | 信任此 Mac，开启设备要求的开发者模式，安装与启动期间保持解锁 |
| Python 3.9+ | 运行 MCP 服务；Python 端使用标准库 |
| Node.js 20.19+、22.12+ 或 24+，npm 10+ | USB 转发与屏幕流；支持范围以项目 engines 和 doctor 检查为准 |

Xcode 需要支持手机当前的 iOS 版本。无需越狱，也无需单独启动 Appium Server。WDA 固定使用已验证的 16.14.0 提交，下载、依赖安装、签名和构建由 setup 流程管理。

### 先在自己的 iPhone 上安装并启动 WDA

手机端使用 [Appium 维护的 WebDriverAgent](https://github.com/appium/WebDriverAgent)。首次使用时，须通过 Xcode 用你自己的 Apple 账号与开发团队签名、安装并启动 `WebDriverAgentRunner`。

推荐使用上面的安装提示词和下面的 `iphone-use-setup` 流程：Codex 获取本项目固定版本的 WDA，配置你的设备和签名，再完成构建、部署与启动；需要你完成 Apple 登录、设备信任、开发者模式或解锁时，会提示具体步骤。

如需在 Xcode 手动处理：

1. 用 USB 连接自己的 iPhone，信任这台 Mac，按系统要求开启开发者模式，并在 Xcode 中配置自己的 Apple 账号。
2. 打开获取的 WDA 源码中的 `WebDriverAgent.xcodeproj`，选择 `WebDriverAgentRunner` scheme 和自己的 iPhone；在 Runner target 的 **Signing & Capabilities** 中选择自己的 Team 与可签名的 Bundle Identifier。
3. 使用 **Product → Test** 构建、安装并运行 WDA Runner，按手机上的实际提示完成信任。运行测试会启动 WDA 服务；安装后仍需要该服务处于运行状态。

本对话首次使用先调用 `pua_setup(action="status")`，复用健康服务或活动工作；缺少服务才 start 一次。start 默认最多等待 20 秒，超时后按同一 job 查询，不重复启动。服务就绪后，以 `pua_ready` 返回 `ready=true` 为准，再开始手机任务。设备与签名要求可参考 [Appium 真机准备说明](https://appium.github.io/appium-xcuitest-driver/latest/getting-started/device-setup/)。

### 安装插件

```sh
git clone https://github.com/zhongerxin/iPhone-use.git
cd iPhone-use
sh scripts/install.sh
```

脚本验证并暂存源码，通过 Codex CLI 注册本地 marketplace、安装插件和技能，并将安装缓存中的服务注册为标准 MCP `iphone_use`。插件与标准配置使用同一命名空间，避免重复工具注册。

源代码包解压后，也可以在包目录运行同一个安装脚本。安装完成后重连或新开 Codex 聊天。

### 连接自己的 iPhone

在新聊天中启用 **iPhone Use**，输入：

> 用 iphone-use-setup 帮我配置通过 USB 连接的 iPhone，安装并启动 WDA，验证 READY，然后显示手机屏幕。

技能引导 Codex 完成诊断、设备发现、签名配置、获取 WDA、后台构建与启动。已配置过的设备会复用配置和构建，正常任务不需要每次重装。`pua_ready` 返回 `ready=true` 后才开始手机任务。

## 可以做什么

| 功能 | 示例 |
| --- | --- |
| App 导航与操作 | 打开目标 App，进入搜索、详情、设置或草稿页面 |
| 页面读取 | 获取紧凑控件树、文字、位置和实际截图 |
| 点击与手势 | 按控件或坐标点击，滑动和拖动，等待页面目标出现 |
| 中文与长文本输入 | 一次提供完整 Unicode 文字，长文本分段输入并支持续传 |
| 连续任务 | 将已知点击、输入、等待等步骤组成一次 batch，减少模型往返 |
| 列表搜索与采集 | 有界滚动查找、重叠读取与去重，返回覆盖边界 |
| 可见失败恢复 | 控件被遮挡、未聚焦或树不完整时，查看新截图并重新选择点击位置 |
| 实时手机屏幕 | iPhone 外壳、设备状态、操作光效与清楚可见的点击 / 拖动提示 |
| 连接恢复 | 复用后台启动工作、重建失效会话；用户可点击刷新重新连接预览 |

示例提示词：

```text
用 iPhone Use 打开备忘录，新建一条草稿，输入下面的中文内容，核对文字后保留。

打开目标 App，搜索这个名称，读取前几项结果并整理给我。

读取这个列表，分段滚动并去重，说明已经覆盖的范围和还未确认的部分。
```

输入与提交分开，工具默认不提交文字。模型需要核对关键页面、接收人、数量和最终结果。密码、验证码、Face ID 等认证交给用户完成，接管期间可以暂停预览。

屏幕预览在同一聊天中复用已有 widget。底部提供刷新、主屏幕和截图按钮；无图像时保留黑色屏幕的 iPhone 外壳，屏幕内仅显示对应状态图标，顶部显示连接或暂停状态。预览供用户观看，模型定位仍以工具返回的实际图像或控件为依据。

## 技术亮点

- **本地 USB 通道。** Python MCP 服务通过本机 loopback 转发访问 WDA，运行数据和签名构建保留在本机。
- **减少重复工作。** 复用 HTTP 连接、WDA session、健康服务与已有构建；首次任务确认 READY，后续沿健康通道继续。
- **紧凑观察与组合动作。** 控件树省略重复字段；仅需截图时不生成 XML；batch、滚动查找和列表采集减少工具回合。
- **安装任务异步执行。** 下载、构建与启动返回可查询的工作 ID，重复 setup 优先复用正在进行的工作。
- **截图回退。** 元素定位失败时返回截图与处理指引；图像像素乘以 `image.pixel_to_point` 转为 iPhone 点坐标，再交给 `pua_tap`。
- **明确失败语义。** 多进程共享操作锁；动作超时或断线可能标记不确定，先读实际状态，避免盲目重放点击、输入或提交。
- **实时预览与暂停恢复。** 屏幕流不落盘；区分锁屏与主动暂停，解锁后的 READY 可恢复锁屏预览，刷新可主动重连。

这些优化主要减少重复请求与模型往返，完整任务速度仍取决于 App、USB / WDA 状态和模型响应。工程回归与真机验收分别记录在本地开发资料中。

## 工具概览

模型可用 17 个工具，另有 2 个仅供屏幕 widget 使用的工具。

| 工具 | 用途 |
| --- | --- |
| `pua_doctor`、`pua_setup`、`pua_ready` | 环境诊断、设备配置、后台安装 / 启动与就绪检查 |
| `pua_observe`、`pua_find` | 控件树、截图与目标查询 |
| `pua_apps`、`pua_launch_app` | 查询 App 标识、读取安装证据和启动 App |
| `pua_tap`、`pua_swipe`、`pua_press_button` | 点击、滑动、主屏幕等操作 |
| `pua_type_text`、`pua_wait` | Unicode 输入与有界等待 |
| `pua_batch`、`pua_scroll_find`、`pua_collect_list` | 组合动作、滚动查找与列表采集 |
| `pua_screen`、`pua_metrics` | 预览开关与有界耗时统计 |

界面异常时先返回截图，再由模型判断下一步：滚动查找一次最多滑一次，仍找不到可点击目标就暂停；遮挡、滚动无进展、输入不符或预期页面未出现也走截图兜底。已有截图直接复用，不自动继续盲滑或重放操作。

## 本机数据与升级

新安装默认使用 `~/.local/share/iphone-use/`，可通过 `IPHONE_USE_STATE_DIR` 指定外部目录；原 `WDA_STATE_DIR` 仍兼容。升级时若新目录不存在，会继续使用检测到的旧配置目录，保留设备、签名和构建。不要为了改名删除运行数据。

运行目录权限为 700，私有配置和图像文件为 600。明确调用的截图有保留上限，预览流不落盘。签名材料、设备数据、运行日志与依赖目录均不进入源代码发布包。`WDA_URL` 可指定本机地址，默认转发端口为 18100，远程地址会被拒绝。

更新已克隆的仓库：

```sh
git pull --ff-only
sh scripts/install.sh
```

重新连接聊天以加载更新后的工具、技能和屏幕资源。若手机连接失败，先检查 USB、设备解锁、开发者模式、Xcode 签名与 setup 返回的具体错误；已有启动工作应继续查询，避免反复重启。

## 开发与文档

```sh
npm ci --prefix ui --no-audit --no-fund
npm run build --prefix ui
sh scripts/check.sh
python3 scripts/package.py
```

构建结果是自包含屏幕 HTML，源代码包位于 `dist/iphone-use-<版本>-source.zip`。安装和普通使用不需要重新构建 UI。安装后的插件仍完整保留 widget 的源码、样式、构建脚本、配置、依赖锁文件和测试，便于本地维护。

- [屏幕预览与恢复](skills/iphone-use/references/screen.md)
- [App 标识参考](skills/iphone-use/references/apps.md)
- [安装与连接故障排查](skills/iphone-use-setup/references/troubleshooting.md)
- [更新记录](CHANGELOG.md)

## 依赖与致谢

本项目依赖 [Appium](https://github.com/appium/appium) 生态与 [WebDriverAgent](https://github.com/appium/WebDriverAgent)：WDA 提供手机端的自动化执行服务，[appium-ios-device](https://github.com/appium/appium-ios-device) 提供 USB 设备通信、端口转发与屏幕流连接能力。iPhone Use 在这些基础上提供 Codex 插件、MCP 工具、安装引导与屏幕 widget，无需单独运行 Appium Server。

感谢 Appium、WebDriverAgent 及相关项目的维护者和贡献者，让真实 iPhone 的自动化操作成为可能。

MIT License。第三方组件说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
