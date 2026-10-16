---
name: iphone-use-claude
description: 在 Claude Code 中使用 iPhone Use（PUA，pua_ 前缀工具）时与 iphone-use、iphone-use-setup 一起读取；说明 Claude Code 下的工具名、截图查看、认证接管提问与手机屏幕预览差异，覆盖原技能中针对 Codex 宿主的写法。
---

# Claude Code 宿主差异

`iphone-use` 与 `iphone-use-setup` 的流程、READY 规则和失败语义不变；只有下面涉及宿主的写法按本节替换。

## 工具名

插件提供的 MCP 工具在 Claude Code 中名为 `mcp__plugin_iphone-use_iphone_use__<工具>`，例如 `mcp__plugin_iphone-use_iphone_use__pua_ready`。原技能示例中的 `tools.mcp__iphone_use__...` 是 Codex 的调用写法，不要照抄。工具未出现在当前可用列表时，先用工具搜索查找 `pua_`，仍缺失才按 `tool-fallback.md` 使用脚本入口。

## 截图

截图随工具结果以图片内容块直接到达模型，无需 `functions.exec`、`image(block)` 或 `text(block.text)` 转发。图片未显示时，用 Read 打开结果中的 `image.path` 或 `error.observation.image.path`；不要把 base64 或图片元数据当作已查看截图。坐标换算仍按 `image.pixel_to_point`。

## 认证接管

原技能要求的宿主提问工具在 Claude Code 中使用 AskUserQuestion：问题说明实际 App、认证方式和完成后继续的工作，第一个选项固定为「已完成继续」，第二个为「暂时无法完成」。AskUserQuestion 会等待用户实际作答，作答前不要恢复手机调用；暂停与恢复预览仍按原流程调用 `pua_screen(action="pause")` / `pua_screen(action="resume")`。

## 手机屏幕预览

Claude Code 不渲染 MCP Apps，READY 和 `pua_screen` 不会自动打开画面，这不是故障。实时手机屏幕改由本插件的查看服务托管：它在 127.0.0.1 上加载与 Codex 相同的屏幕 widget，在右侧内置浏览器面板中显示画面、点击光效与刷新 / 主屏幕 / 截图按钮。

桌面端 Code 标签页中，插件钩子会在 `pua_ready` 返回 `ready=true` 或 `pua_screen()` 打开后自动完成下面第 1、2 步：启动查看服务，在右侧浏览器面板打开画面或切换到已有的预览标签页，并在工具结果后附一条以「iPhone Use：」开头的说明。看到这条说明时按它的提示处理（面板收起时提醒用户展开），不要再手动 start 或打开标签页。用户要求看画面而 READY 已经取得时，调用一次 `pua_screen()` 即可触发自动打开。只有没有收到这条说明时（旧版 Claude Code 不加载钩子模块），才手动执行第 1、2 步。

1. 运行一次 `python3 <PLUGIN_ROOT>/viewer/screen_viewer.py start`。返回 JSON 的 `url` 是预览地址；已有实例时返回同一地址（`reused=true`），不会重复启动。
2. 有内置浏览器面板工具（`mcp__Claude_Browser__*`）时，先用 `tabs_context` 查看是否已有同一 `http://127.0.0.1:<端口>` 的标签页：已有则用 `tabs_select` 切到它，不再打开；有指向其他 127.0.0.1 端口（已退出的旧实例）的预览标签页时用 `navigate` 改到新地址；都没有才用 `preview_start` 传入该 `url`。`preview_start` 每次都会新建标签页，不要重复调用。`tabs_context` 提示面板处于隐藏状态时，告诉用户展开右侧浏览器面板即可看到画面；隐藏期间 widget 自动停止拉帧。没有浏览器面板（如终端中的 Claude Code）时，把地址交给用户在本机浏览器打开。
3. 打开后由 widget 自行刷新画面：不要为刷新重复 start、截图、observe 或 `pua_screen()`（它只用于重新打开已关闭的画面），不要通过浏览器工具点击预览页面来操作手机，手机操作仍只用 pua_ 工具；也不要调用仅供 widget 使用的 `pua_screen_frame` 与 `pua_screen_action`。
4. 预览只供用户观看。判断页面、定位目标与最终验收仍以工具返回的截图或控件树为准，面板画面不替代模型观察。
5. 认证接管仍按原流程 `pua_screen(action="pause")` / `pua_screen(action="resume")`；暂停状态与 MCP 服务共享，面板会同步显示。
6. 查看服务 10 分钟没有请求会自动退出；用户关闭面板后再次要求查看时调用一次 `pua_screen()`，没有自动打开说明时再手动执行第 1 步。需要手动停止时运行同一脚本的 `stop`。
7. start 失败时说明原因并继续手机任务，预览不可用不阻塞 READY 之后的工作。

## 脚本入口

本技能与 `tool-fallback.md` 中的 `<PLUGIN_ROOT>` 是本技能目录向上两级的插件根目录（含 `server/`、`viewer/` 与 `scripts/phone.py`）。脚本输出中的图片同样用 Read 打开 `image.path` 查看。
