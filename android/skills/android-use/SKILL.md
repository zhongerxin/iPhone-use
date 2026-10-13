---
name: android-use
description: 通过 Android Use MCP 控制 USB 或已配对无线连接的 Android 手机，读取页面、点击、滑动、输入中文、启动应用、采集列表及显示手机屏幕。
---

# Android Use

使用 android_use 命名空间的 pua_* 工具，不要误调用 iPhone 的同名工具。
首次使用先读 android-use-setup，setup status，按需 configure/start 并等待同一 job，ready=true 后操作。
READY 默认对只读通道故障执行有次数限制的恢复；recover=false 仅诊断。recovering 时等待同一 setup job，再 ready，不重放手机操作。已有健康连接继续复用。不因一次任务而重新安装或重启。

## 观察与操作

- observe(mode=tree/both/screenshot) 返回新观察。节点字段为 text、resource_id、content_desc、class_name、package、rect 和布尔状态。
- 坐标是 Android 显示像素。截图可能缩小，图像坐标乘 image.pixel_to_display 后用于 tap。旋转后重新观察。
- selector 使用新节点的原生字段。多个匹配先查看候选，再加字段或 index。坐标点击仍可能被浮层遮挡。
- 普通动作默认乐观执行，observe 供下一步决策；expect/verify 才表示指定检查。点击成功不代表最终业务成功。
- selector、输入、滚动或页面期望失败后先看错误截图；缺图时 observe screenshot。不要盲目重放动作，也不要反复换 selector。
- tap selector 失败可按新截图坐标点击，再 type_text 省略 selector 操作已聚焦输入框。
- type_text 一次传完整 Unicode 文本（最多 10000 字符），使用单次 setText。默认 replace=true、submit=false；verify=true 精确读回，submit 前始终检查文字。多行必须 allow_newlines=true。没有续传或静默重试。
- 自绘编辑器不一定暴露 EditText；若 not_editable，不用猜测输入法或剪贴板绕过，先观察并说明适配限制。
- password/验证码等认证交用户，screen pause 后接管。只有用户确认完成才能 resume。锁屏由用户解锁，ready 仅自动解除 device_locked 暂停。
- apps 只返回设备实际安装的 package。支持包名子串和小型别名表；未知中文名通过桌面图标/页面证据定位，不能猜包名。
- swipe 一次一个手势；verify 使用同名文字锚点的移动证据。未证明移动不等于列表结束。
- collect_list 必须提供 row_selector，只采集可访问性节点，重复内容可能合并。核对数量/结束标志，不能宣称无条件完整。
- batch 预校验所有步骤；遇到错误、未核实提交或时间预算即停止。stopped_at 是下一个/未解决步骤的零基索引；先核实不确定动作，不重新执行之前成功的步骤。

## 屏幕与恢复

ready 和 screen 使用同一个 Android 屏幕 widget，支持刷新、主页和复制截图。预览优先使用 scrcpy 连续采集和最新帧传输，缺依赖时回退截图；显示帧率受宿主传输影响，不代表模型已观察。
连接断开时 setup status/start 恢复通道，再 ready；恢复不会重做之前的点击、输入或提交。
当前聊天未加载 MCP 时，可使用已安装目录 server/android_use.py --tool pua_* --arguments JSON，解释需重连聊天才能加载原生工具和侧边栏。
截图响应可能包含 base64，仅显示 image content 或本地 path，勿将整个结果打印成文本。
