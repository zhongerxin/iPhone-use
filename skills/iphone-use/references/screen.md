# 手机屏幕预览与恢复

widget 为用户显示手机画面，模型只使用工具实际返回的观察。Live、缓存画面、光效或手势指示都不替代 READY，也不证明任务完成。打开、暂停、恢复及重新连接复用本聊天的同一个面板；已有面板时直接继续，已关闭时调用一次 `pua_screen()`，不要为了刷新重复 READY 或打开新面板。

## 没有画面或显示暂停

- 首帧尚未到达时，`frame_available=false` 不证明连接失败。预览使用独立 MJPEG 通道（优先 USB，不可用时使用已配对设备的 CoreDevice 网络隧道），与 PUA 控制通道分开；控制工具可用而预览缺帧时，不为此反复重启 PUA、新建 session 或循环截图。
- 没有任何有效画面时保留 iPhone 外壳与黑色屏幕，中央只显示对应的连接、锁屏、认证、暂停或画面不可用图标。短暂断流保留最后一帧；缓存不是当前手机状态的证据。
- `preview.paused=true` 时先查看 `pause_reason`。`device_locked` 表示等待用户解锁，成功 READY 只恢复同一次锁屏暂停；`authentication` 或旧版 `unknown` 按 [认证接管](authentication.md) 等待用户实际完成，再显式 resume。
- 用户可以点击底部刷新按钮重新连接预览并检查解锁；成功时恢复点击时的暂停，检查过程中发生的新暂停继续保留。刷新不重启 PUA、不新建控制会话、不发送手机手势。模型不能代调 App 专用按钮工具。
- 更新插件后，旧聊天可能仍连接旧进程或 UI 资源。重新连接聊天，关闭旧屏幕页后调用一次 `pua_screen()` 加载新资源；不必仅为更新画面重新安装或重启 PUA。

控制工具本身返回连接故障时按 [启动与恢复](startup.md) 处理；界面定位失败时按主技能的截图与坐标操作流程继续。

## 认证与工具边界

App 要求密码、PIN、验证码或 Face ID / Touch ID 时，按 [认证接管](authentication.md) 暂停预览、请求用户在手机上完成，并暂停手机动作、读取与截图。打开 widget 或 READY 成功不表示认证已完成；实际完成通知后获取新观察再继续，不能沿用旧坐标或重放可能已生效的业务操作。

`pua_screen_frame` 与 `pua_screen_action` 仅供 App 使用，不是模型的观察或控制工具。底部刷新、主屏幕与截图按钮由用户自己点击；认证暂停期间主屏幕和截图不可用。页面隐藏或关闭会停止帧轮询，认证暂停会立即停止采集并清除实际画面。

## 随插件保留的 widget

插件同时包含 `assets/phone-screen.html` 自包含页面，以及完整的 `ui/` 源码、样式、依赖锁文件、构建配置和 widget 测试。安装和普通使用直接加载已构建页面；需要重建时，在插件目录运行：

```sh
npm ci --prefix ui --no-audit --no-fund
npm run build --prefix ui
python3 scripts/check_screen_ui.py
```

UI 不依赖 CDN 或远程字体；服务端预览依赖的设备流脚本与 `tooling/` 依赖清单也随插件提供。`docs/` 与 `evals/` 为本地开发资料，不是运行时依赖。
