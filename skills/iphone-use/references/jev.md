# Jev 快速导航与测量

`pua_jev` 把原本需要宿主逐步选择的导航、搜索和草稿操作移到本机有界循环。先完成本对话的 setup / READY，再提供具体目标、结束条件和必要的单行输入文字。它会将可见控件文字、任务目标和最近操作发送给 TypeSafe；普通 PUA 工具不因此增加云端调用。

## 配置与使用

密钥从 `TYPESAFE_API_KEY` 读取，或存放在当前运行目录的 `jev.json`：

```json
{"api_key":"你的 TypeSafe API key"}
```

文件必须属于当前用户、权限为 `600`，不能是符号链接。运行目录沿用本机现有配置；新安装通常是 `~/.local/share/iphone-use`，升级可能继续使用旧目录。密钥不要放进 server、skills、源码或提交。环境变量优先；模型默认 `jev-latest`，设置 `TYPESAFE_MODEL` 可固定版本。此实现仅连接官方 HTTPS endpoint，不接受自定义密钥目标地址。

```sh
python3 scripts/phone.py pua_jev '{"goal":"打开关于本机页面，不更改任何设置","max_steps":8,"timeout_seconds":30,"expect":{"label":"序列号"}}'
```

目标应描述结果，页面入口以实际前台观察为准。需要先打开 App 时，沿用已核验 bundle ID 的 `pua_launch_app`；不要在 goal 中猜 App ID。

```json
{
  "goal":"在当前搜索页面输入北京大学，打开搜索结果，不收藏、不预订",
  "texts":[{"selector":{"type":"SearchField","label":"搜索"},"text":"北京大学"}],
  "max_steps":8,
  "timeout_seconds":30
}
```

`texts` 是调用者提供的完整单行文字；selector 匹配实际观察中的编辑字段。Jev 不生成文字。缺少文字时会交回宿主补充，而不猜测。`dry_run=true` 只读取当前页面和选择一次动作，不点击、输入或滚动。

## 为什么能够减少等待

每轮重新编号当前可见控件；一次请求同时选择 operation 与兼容的 target，代码只执行所选 operation 对应的答案。模型不返回可执行代码、选择器或坐标。点击继续使用真实控件查询和可点击性检查，持有现有设备操作锁；动作后的新观察直接供下一轮使用。

循环使用更短的页面稳定等待，结束后恢复原设置。API 复用 HTTPS 连接，正常循环不传截图，也不再等待宿主模型逐步发下一条工具调用。已知固定路径仍适合 `pua_batch`，无需付出 Jev 预测开销。

接口给出总耗时、模型请求耗时、页面观察、动作耗时、步数和停止原因；分类次数和包含重连的 HTTP 请求次数分别记录。显式 expect 可由新观察中的实际控件直接满足，省去最后一次模型调用。250 毫秒的转场等待和有界刷新避免把旧页面当作下一步输入。真机速度还取决于 App 动画、控件查询、手机通道以及到 TypeSafe 的网络距离。低模型推理耗时不能等同于完整手机任务耗时。

## 停止后怎样继续

达到步数或时间上限、页面无进展、空树或截断、认证、缺少文字、视觉兜底、部分输入和动作不确定时停止，把当前观察、已执行步骤及错误交回宿主。不要重新运行整段 goal 来重放已经执行的操作；先读结果并从实际状态继续。密码、验证码和 Face ID 仍按认证接管流程处理。

`DONE` 只是模型的停止选择。没有显式 expect 的结果不能声称业务已完成；expect 成功也只证明控件存在，关键内容、金额、接收人和实际记录仍按任务要求验收。发送、付款及删除交回宿主依据用户授权单独执行。

## 调研依据

[jev-ultrafast](https://github.com/browser-use/jev-ultrafast) 使用可见 DOM 控件表和并行 operation / target 问题，其浏览器执行层不能直接操作 iPhone。本项目借鉴其决策循环，使用 PUA 原生控件树与执行器。

上游 [性能记录](https://github.com/browser-use/jev-ultrafast/blob/main/docs/performance.md) 报告六次交替浏览器运行的任务中位数由 9.450 秒降到 7.092 秒，且每版各三次成功。样本只有一个任务与一个浏览器配置；初始化和最终独立验证边界应按原报告解释，不能推断 iPhone 也会提速 25%。

[TypeSafe API](https://docs.typesafe.ai/api) 的 `/v1/systemone` 接收结构化 state 和具名 Choice 问题，每题最多 255 个选项；返回 choice、probabilities 和 confidence。[并行问题说明](https://docs.typesafe.ai/patterns/fan-out) 强调问题独立求值，由代码选取相关答案。[模型说明](https://docs.typesafe.ai/models) 列出 Jev 为纯文本输入、支持 CJK，但英文准确性较强。当前 `jev-latest` 指向 `jev-1.13.0`，实际结果记录响应中的版本。

此接入使用 Python 标准库，不依赖浏览器框架或额外文本模型；HTTP / 响应异常和未知目标不会执行手机动作，分类传输失败可在原有总时限内重连一次；网络错误不会自动重放手机命令。

## 本机复现

```sh
# 使用公开合成中文控件测 API；不会操作手机，默认 3 个样本
python3 scripts/benchmark_jev.py --samples 3
# 已 READY、从设置首页开始的一次完整导航；只保存耗时与操作种类
python3 scripts/benchmark_jev.py --goal 'Open General (通用), then AirDrop (隔空投送). Only view; do not change settings.' --expect '{"type":"Button","label":"接收关闭"}' --output outputs/jev-navigation.json
```

循环计时包含页面读取、Jev 和手机动作，排除 setup / READY、外层锁屏预检与宿主模型时间。多次任务测量需要先恢复相同起始页面；已处在终态的运行不能与完整导航比较。API 样本和真机运行均会产生模型请求。

2026-10-10 本机真机试验中，从设置首页进入通用、隔空投送的两次成功运行分别约 5.96 和 9.66 秒，均为两次 Jev 分类加两次点击，并以实际「接收关闭」控件验收；期间另有一次无进展停止，以及通知横幅造成的遮挡回退。这是开发样本，不是稳定成功率或提速基线。真实请求包含网络与连接重建，不能直接比较上游推理毫秒数。
