"""Text that differs between Codex and WorkBuddy: instructions, skills and the widget page.

Upstream text names Codex-only tools (functions.exec, request_user_input_async,
view_image), its own installer and a side panel that READY opens. Each entry replaces
one exact upstream passage.
A passage that is no longer found means upstream reworded it: the installer and the
tests stop there, so the entry gets a fresh look instead of silently going stale.
"""
import hashlib
import re


class Drift(Exception):
    """An upstream passage this port rewrites is no longer present."""


INSTRUCTIONS = [
    ("A screenshot arrives as an image in the same result; through functions.exec forward each image block with image(block) and text blocks with text(block.text), never text(the whole result) or base64. If image forwarding is unavailable, use view_image on image.path or error.observation.image.path. ",
     "A screenshot arrives as an image in the same result. If no image is visible in the result, open image.path or error.observation.image.path with the host's file reading tool; image metadata or base64 text is not a viewed screenshot. "),
    ("The live iPhone screen opens or reuses the same side panel with READY; setup/recovery and preview pause/resume keep the existing panel. Use pua_screen to reopen a closed panel, not to refresh an already open one. ",
     "WorkBuddy shows the live iPhone screen in its side panel, opened by pua_screen(): READY does not open it. After the first READY in a chat call pua_screen() once so the user can watch; later pua_screen calls reuse the same panel, and an open panel never needs refreshing. "),
    ("use the available host question tool (request_user_input_async in Default), first option exactly 已完成继续. Wait for the actual user answer; async return or preselection is not confirmation.",
     "use the host question tool (AskUserQuestion in WorkBuddy), first option exactly 已完成继续. Wait for the actual user answer; a skipped, timed-out or preselected question is not confirmation."),
]

TOOL_DESCRIPTIONS = {
    "pua_screen": [
        ("Open or reuse the live iPhone screen in the Codex side panel.",
         "Open or reuse the live iPhone screen in the WorkBuddy side panel."),
        ("READY also opens or reuses this view by default.", "READY does not open it: call this once after the first READY in a chat."),
    ],
}

SKILLS = {
    "skills/iphone-use/SKILL.md": [
        ("READY 关联手机屏幕侧边栏，宿主支持时默认打开或复用本聊天已有面板；重复 setup / 恢复通道、暂停 / 恢复预览沿用同一个 widget。已有面板时直接继续，不为刷新再调用屏幕打开工具；需要重新打开已关闭的面板时调用一次 `pua_screen()`，不要为打开画面重复 READY。",
         "WorkBuddy 把手机屏幕显示在侧边栏，由 `pua_screen()` 打开，READY 不会打开它。本对话第一次 READY 成功后调用一次 `pua_screen()` 让用户观看；之后的暂停 / 恢复预览沿用同一个面板。已有面板时直接继续，不为刷新再调用 `pua_screen()`，也不要为打开画面重复 READY。"),
        ("必须调用宿主提问工具（Default 优先 `functions.request_user_input_async`），首个选项固定「已完成继续」，第二个可为「暂时无法完成」。异步返回 / 预选不是用户答复；",
         "必须调用宿主提问工具（WorkBuddy 中为 `AskUserQuestion`），首个选项固定「已完成继续」，第二个可为「暂时无法完成」。提问被跳过、超时或预选不是用户答复；"),
        ("""通过 `functions.exec` 调工具时必须把图片真正转发给模型，不能 `text(result)` 输出整份含 base64 的结果：

```javascript
const result = await tools.mcp__iphone_use__pua_observe({mode: "screenshot"});
for (const block of result.content ?? []) {
  if (block.type === "text") text(block.text);
  else if (block.type === "image") image(block);
}
```

点击、输入等失败结果同样转发图片块。如果图片没转发成功，用 `view_image` 打开返回的 `image.path` / `error.observation.image.path`；不能把图片元数据或 base64 当成看过截图。""",
         "WorkBuddy 中直接调用 `mcp__iphone_use__pua_*` 工具，截图作为图片块随同一结果返回；点击、输入等失败结果同样附带图片块。如果结果里没有看到图片，用读取文件的工具打开返回的 `image.path` / `error.observation.image.path`；不能把图片元数据或 base64 当成看过截图。"),
    ],
    "skills/iphone-use/references/authentication.md": [
        ("""在 Codex Default 模式优先调用 `functions.request_user_input_async`，例如当前明确显示 Face ID 时：

```json
{
  "questions": [{
    "title": "当前 App 需要 Face ID 验证。请在 iPhone 上完成认证并停留在目标页面；完成后选择「已完成继续」，我会接着读取剩余明细。",
    "options": ["已完成继续", "暂时无法完成"]
  }]
}
```""",
         """在 WorkBuddy 中调用 `AskUserQuestion`，按该工具当前的实际 schema 填写。例如当前明确显示 Face ID 时：

- 问题：当前 App 需要 Face ID 验证。请在 iPhone 上完成认证并停留在目标页面；完成后选择「已完成继续」，我会接着读取剩余明细。
- 选项：「已完成继续」、「暂时无法完成」"""),
        ("异步提问会立即返回，这不是用户完成通知。保持问题待答，等待用户实际提交选项或明确回复；预选项、工具返回、等待时间经过和没有答复都不能当作完成。",
         "以用户实际提交的选项或明确回复为准；提问被跳过、超时、没有答复，或工具在用户作答前就返回，都不能当作完成。"),
    ],
    "skills/iphone-use/references/tool-fallback.md": [
        ("普通版提供 17 个模型工具；先查看本回合实际可调用的工具绑定（支持时用 ALL_TOOLS 或工具搜索），不能只从 tools/list、文档或先前聊天推断已经绑定。0.1.6 的 install.sh 同时注册同名标准 MCP，避免 Codex 插件共享说明预算隐藏工具，并保留完整 batch schema。新版安装后重新连接聊天；",
         "普通版提供 17 个模型工具，WorkBuddy 中名为 `mcp__iphone_use__pua_*`；先查看本回合实际可调用的工具绑定，不能只从 tools/list、文档或先前聊天推断已经绑定。WorkBuddy 版由 `workbuddy/install.py` 把标准 MCP `iphone_use` 写入 `~/.workbuddy/mcp.json`；安装或更新后需要在「连接器管理」信任该服务并新开会话；"),
        ("通过 `functions.exec` 时逐个内容块转发：文字用 `text(block.text)`，图片用 `image(block)`；不要 `text(result)` 把截图变成 base64 文本。图片无法转发时用 `view_image` 打开 `image.path` / `error.observation.image.path`。",
         "截图作为图片块随结果返回；结果里没有看到图片时，用读取文件的工具打开 `image.path` / `error.observation.image.path`，不要把 base64 文本当成截图。"),
        ("从当前项目或已安装插件确定 `<PLUGIN_ROOT>`，不要照抄某人的版本 cache 路径。",
         "WorkBuddy 版的 `<PLUGIN_ROOT>` 是安装目录，默认 `~/.workbuddy/mcp-servers/iphone-use`；以 `~/.workbuddy/mcp.json` 中 `iphone_use` 的实际路径为准。"),
    ],
    "skills/iphone-use-setup/SKILL.md": [
        ("READY 同时关联手机屏幕侧边栏，宿主支持时默认打开；已有通道要重新打开画面用 `pua_screen()`，不要重复 READY。",
         "WorkBuddy 中 READY 不会打开手机屏幕；取得 READY 后调用一次 `pua_screen()` 在侧边栏打开，已有面板时不再调用，也不要重复 READY。"),
        ("并必须调用可用的宿主提问工具（Default 优先 `functions.request_user_input_async`），",
         "并必须调用可用的宿主提问工具（WorkBuddy 中为 `AskUserQuestion`），"),
    ],
}

# Appended to the skill so the model knows what is specific to this host.
SKILL_NOTES = {
    "skills/iphone-use/SKILL.md": """
## 在 WorkBuddy 中使用

- 工具名为 `mcp__iphone_use__pua_*`，由 `~/.workbuddy/mcp.json` 中的标准 MCP `iphone_use` 提供。本回合没有这些工具时，请用户在「连接器管理」信任 `iphone_use` 并新开会话。
- 手机屏幕在 WorkBuddy 侧边栏，由 `pua_screen()` 打开并复用。面板底部没有刷新 / 主屏幕 / 截图按钮：WorkBuddy 不放行面板自己发起的工具调用，预览恢复只能由 `pua_screen(action="resume")` 或解锁后的 READY 完成，不要让用户去点刷新。面板没有画面不影响控制通道，也不是任务失败；按工具返回的观察继续。
- 运行数据在 `~/.local/share/iphone-use`，与 Codex 版共用同一份配置、构建和操作锁；返回 `device_busy` 时可能是另一个宿主正在操作手机。
""",
    "skills/iphone-use-setup/SKILL.md": """
## 在 WorkBuddy 中使用

- 工具名为 `mcp__iphone_use__pua_*`。本回合没有这些工具时，请用户在「连接器管理」信任 `iphone_use` 并新开会话。
- WorkBuddy 是 GUI 应用，启动 MCP 时 PATH 不完整；安装器已把 node、npm、git 和 Xcode 命令所在目录写进 `~/.workbuddy/mcp.json` 中 `iphone_use` 的 `env.PATH`。`pua_doctor` 报告缺少 node / npm 而本机实际已安装时，在源码目录重新运行 `python3 workbuddy/install.py` 刷新路径，不要重装 Node。
- 运行数据在 `~/.local/share/iphone-use`；Codex 版已经配置并构建过时直接复用，不重新 fetch / configure / build。
""",
}


# What WorkBuddy does differently with the widget page, handled by one insertion at the top
# of the page and one line added to the bundled SDK's callServerTool.
#
# Frames. A view's tools/call to a local server is answered by WorkBuddy's agent with
# "Sensitive MCP egress review is unavailable." and never reaches the server: the egress
# review needs a chat session and this path has none. Reading a resource is forwarded, so
# the widget's frame poll reads the frame as a resource; mcp_server.py serves it.
#
# Frame rate. Each such read costs WorkBuddy a new server process, so four a second is all
# there is. The answer says where the frame source listens on loopback (preview.py), and from
# then on the page asks it directly: one request held open per frame, drawn as it arrives,
# while the widget's own poll is answered from what those requests last saw. The widget
# sizes and reveals the picture only for a frame it is handed itself, so it is handed one
# whenever it has none or the size changes. If the page cannot reach the source, reads keep
# going through WorkBuddy; each then carries the frames since the previous read, the widget
# is handed the oldest and told it has seen the newest, and the ones in between are drawn
# here at the pace they were captured.
#
# Toolbar. Refresh, Home and screenshot are tool calls too and cannot work. A button that
# does nothing is worse than no button; they are not rerouted, because Home and screenshot
# act on the phone and the Mac, and the host means such calls to pass its review.
#
# Height. Opened from chat history WorkBuddy shows the view inline, at a small default height
# that changes only when the view reports a size. The widget lays itself out from its
# container and never reports one, so it asks once for room for the phone.
FRAME_URI = "ui://iphone-use/frame/"
SCREEN_HEIGHT = 680
# Lets the page's fetch reach the frame source; pictures are drawn from data: URLs as before.
SCREEN_CONNECT = ["http://127.0.0.1:*"]
SCREEN_SCRIPT = """<style>#toolbar{display:none!important}</style>
<script>
(function () {
  var pending = [], direct = null, blocked = false, proven = false, pulling = false, misses = 0;
  var latest = null, newest = null, given = null;
  function picture() { return document.getElementById("image"); }
  function show(frame) {
    var image = picture();
    if (image && !image.hidden) image.src = "data:" + frame.mimeType + ";base64," + frame.data;
  }
  function settle() {
    pending.forEach(clearTimeout);
    pending = [];
  }
  function pull() {
    if (!direct || document.hidden) { pulling = false; return; }
    pulling = true;
    var stop = new AbortController(), giveUp = setTimeout(function () { stop.abort(); }, 3000);
    fetch(direct + "/frame/" + (newest ? newest.seq : 0) + "/0?wait=250", {cache: "no-store", signal: stop.signal}).then(function (reply) {
      if (!reply.ok) throw new Error("frame source");
      return reply.json();
    }).then(function (preview) {
      clearTimeout(giveUp);
      settle();
      proven = true; misses = 0; latest = preview;
      if (preview.paused) { newest = given = null; }
      if (preview.frame) {
        newest = preview.frame;
        if (given && given.width === newest.width && given.height === newest.height) show(newest);
      }
      if (preview.frame) pull(); else setTimeout(pull, 30);
    }).catch(function () {
      clearTimeout(giveUp);
      if (++misses < 4) { setTimeout(pull, 250); return; }
      blocked = !proven; direct = latest = null; pulling = false; misses = 0;
    });
  }
  window.__workbuddyFrame = function (app, call, options) {
    var cursor = call.arguments || {}, after = Number(cursor.after_seq) || 0, seen = Number(cursor.last_event_id) || 0;
    if (direct && latest) {
      if (!pulling) pull();
      var answer = {}, image = picture();
      for (var key in latest) if (key !== "frame" && key !== "more" && key !== "direct") answer[key] = latest[key];
      var hand = newest && newest.seq > after && (!given || image.hidden || !image.getAttribute("src") || given.width !== newest.width || given.height !== newest.height);
      answer.frame = hand ? newest : null;
      if (hand) given = {width: newest.width, height: newest.height};
      answer.events = (latest.events || []).filter(function (event) { return event.id > seen; });
      return Promise.resolve({content: [], structuredContent: answer, isError: false});
    }
    return app.readServerResource({uri: "__FRAME__" + after + "/" + seen}, options).then(function (read) {
      var preview = JSON.parse(read.contents[0].text);
      var frames = (preview.more || []).concat(preview.frame ? [preview.frame] : []);
      var first = frames[0], last = frames[frames.length - 1];
      if (last) { newest = last; given = {width: last.width, height: last.height}; }
      if (preview.direct && !blocked && !direct) { direct = preview.direct; pull(); }
      delete preview.more;
      delete preview.direct;
      settle();
      if (frames.length > 1 && first.width === last.width && first.height === last.height) {
        preview.frame = {seq: last.seq, data: first.data, mimeType: first.mimeType, width: first.width, height: first.height};
        frames.slice(1).forEach(function (frame) {
          pending.push(setTimeout(function () { show(frame); }, Math.max(0, frame.at - first.at)));
        });
      }
      return {content: [], structuredContent: preview, isError: false};
    });
  };
})();
addEventListener("message", function ask(event) {
  var reply = event.data && event.data.jsonrpc === "2.0" && event.data.result;
  if (!reply || !reply.hostContext) return;
  removeEventListener("message", ask);
  if ((reply.hostContext.displayMode || "inline") !== "inline") return;
  setTimeout(function () {
    event.source.postMessage({jsonrpc: "2.0", method: "ui/notifications/size-changed", params: {height: __HEIGHT__}}, "*");
  }, 0);
});
</script>
""".replace("__FRAME__", FRAME_URI).replace("__HEIGHT__", str(SCREEN_HEIGHT))
# The method name is the SDK's public API and survives minification; its argument names do not.
SDK_CALL = re.compile(r"async callServerTool\((\w+),(\w+)\)\{")
SDK_FRAME_CALL = r'\g<0>if(\1&&\1.name==="pua_screen_frame")return globalThis.__workbuddyFrame(this,\1,\2);'


def screen_uri(upstream):
    """WorkBuddy caches the page by URI across chats; a changed page needs a new one."""
    revision = hashlib.sha1((SCREEN_SCRIPT + SDK_FRAME_CALL + " ".join(SCREEN_CONNECT)).encode()).hexdigest()[:8]
    return upstream.replace(".html", f"-workbuddy-{revision}.html")


def screen_html(text, strict=True):
    """The widget page as WorkBuddy should load it."""
    if "<head>\n" not in text or len(SDK_CALL.findall(text)) != 1:
        if strict:
            raise Drift("assets/phone-screen.html: <head> or the SDK's callServerTool not found exactly once")
        return text
    return SDK_CALL.sub(SDK_FRAME_CALL, text).replace("<head>\n", "<head>\n" + SCREEN_SCRIPT, 1)


def rewrite(text, replacements, where="", strict=True):
    for old, new in replacements:
        if old not in text:
            if strict:
                raise Drift(f"{where}: upstream passage not found: {old[:48]!r}")
            continue
        text = text.replace(old, new)
    return text


def skill_text(relative, text, strict=True):
    """A packaged skill file as WorkBuddy should read it; other files pass through."""
    text = rewrite(text, SKILLS.get(relative, ()), relative, strict)
    return text.rstrip("\n") + "\n" + SKILL_NOTES[relative] if relative in SKILL_NOTES else text


def adapt_server(module, strict=True):
    """Apply the host wording to an imported upstream iphone_use module."""
    module.INSTRUCTIONS = rewrite(module.INSTRUCTIONS, INSTRUCTIONS, "INSTRUCTIONS", strict)
    for tool in module.TOOLS:
        if tool["name"] in TOOL_DESCRIPTIONS:
            tool["description"] = rewrite(tool["description"], TOOL_DESCRIPTIONS[tool["name"]], tool["name"], strict)
