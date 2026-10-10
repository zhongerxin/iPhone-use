"""Text and widget page for Doubao Work, told from what the WorkBuddy port already rewrites.

The upstream passages that name Codex-only tools are the ones workbuddy/host_text.py lists.
Each list here gives, for the same passages in the same order, what holds in Doubao Work.
A list that falls out of step with WorkBuddy's stops the installer and the tests, like a
passage upstream reworded.
"""
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workbuddy"))
import host_text as base

Drift = base.Drift


def retell(entries, told, where):
    """Pair the upstream passages of a WorkBuddy list with this host's replacements."""
    if len(entries) != len(told):
        raise Drift(f"{where}: WorkBuddy rewrites {len(entries)} passages, Doubao Work has text for {len(told)}")
    return [(old, new) for (old, _), new in zip(entries, told)]


INSTRUCTIONS = retell(base.INSTRUCTIONS, [
    base.INSTRUCTIONS[0][1],
    "Doubao Work shows the live iPhone screen in its side panel, opened by pua_screen(): READY does not open it. After the first READY in a chat call pua_screen() once so the user can watch; an open panel follows the phone by itself, so do not call pua_screen again to refresh it. ",
    "use the host question tool (interaction.ask in Doubao Work; without one, ask in your reply and stop), first option exactly 已完成继续. Wait for the actual user answer; a skipped, timed-out or preselected question is not confirmation.",
], "INSTRUCTIONS")

TOOL_DESCRIPTIONS = {
    "pua_screen": retell(base.TOOL_DESCRIPTIONS["pua_screen"], [
        "Open the live iPhone screen in the Doubao Work side panel.",
        "READY does not open it: call this once after the first READY in a chat.",
    ], "pua_screen"),
}

ASK = "宿主提问工具（豆包工作中为 `interaction.ask`；本对话没有提问工具时在回复里直接问并停下）"
SKILLS = {
    "skills/iphone-use/SKILL.md": retell(base.SKILLS["skills/iphone-use/SKILL.md"], [
        "豆包工作把手机屏幕显示在侧边栏，由 `pua_screen()` 打开，READY 不会打开它。本对话第一次 READY 成功后调用一次 `pua_screen()` 让用户观看；面板打开后自己跟随手机，暂停 / 恢复预览沿用同一个面板。已有面板时直接继续，不为刷新再调用 `pua_screen()`，也不要为打开画面重复 READY。",
        f"必须调用{ASK}，首个选项固定「已完成继续」，第二个可为「暂时无法完成」。提问被跳过、超时或预选不是用户答复；",
        "豆包工作中直接调用 `mcp__iphone_use__pua_*` 工具，截图作为图片块随同一结果返回；点击、输入等失败结果同样附带图片块。如果结果里没有看到图片，用读取文件的工具打开返回的 `image.path` / `error.observation.image.path`；不能把图片元数据或 base64 当成看过截图。",
    ], "skills/iphone-use/SKILL.md") + [
        # Doubao Work can also operate apps on the Mac. The description is what tells a phone task apart.
        ("description: 通过 PUA（Phone Use Agent）MCP 工具高效操作真实 iPhone；",
         "description: 用户要在手机 / iPhone 上打开 App、搜索、点按、输入、发消息或读取内容时使用。通过 PUA（Phone Use Agent）MCP 工具高效操作真实 iPhone；"),
    ],
    "skills/iphone-use/references/authentication.md": retell(base.SKILLS["skills/iphone-use/references/authentication.md"], [
        """在豆包工作中调用 `interaction.ask`，按该工具当前的实际 schema 填写；本对话没有提问工具时在回复里直接问并停下。例如当前明确显示 Face ID 时：

- 问题：当前 App 需要 Face ID 验证。请在 iPhone 上完成认证并停留在目标页面；完成后选择「已完成继续」，我会接着读取剩余明细。
- 选项：「已完成继续」、「暂时无法完成」""",
        "以用户实际提交的选项或明确回复为准；提问被跳过、超时、没有答复，或工具在用户作答前就返回，都不能当作完成。",
    ], "skills/iphone-use/references/authentication.md"),
    "skills/iphone-use/references/tool-fallback.md": retell(base.SKILLS["skills/iphone-use/references/tool-fallback.md"], [
        "普通版提供 17 个模型工具，豆包工作中名为 `mcp__iphone_use__pua_*`；先查看本回合实际可调用的工具绑定，不能只从 tools/list、文档或先前聊天推断已经绑定。豆包工作版由 `doubao/install.py` 安装到本机，再在「插件 · 技能 · 伙伴」里新建 STDIO 自定义连接器 `iphone_use` 接入；安装或更新后需要把该连接器关闭再打开，并新开任务；",
        "截图作为图片块随结果返回；结果里没有看到图片时，用读取文件的工具打开 `image.path` / `error.observation.image.path`，不要把 base64 文本当成截图。",
        "豆包工作版的 `<PLUGIN_ROOT>` 是安装目录，默认 `~/.local/share/iphone-use-doubao`；以自定义连接器 `iphone_use` 的参数里 `doubao/mcp_stdio.py` 所在的实际路径为准。",
    ], "skills/iphone-use/references/tool-fallback.md"),
    "skills/iphone-use-setup/SKILL.md": retell(base.SKILLS["skills/iphone-use-setup/SKILL.md"], [
        "豆包工作中 READY 不会打开手机屏幕；取得 READY 后调用一次 `pua_screen()` 在侧边栏打开，已有面板时不再调用，也不要重复 READY。",
        f"并必须调用可用的{ASK}，",
    ], "skills/iphone-use-setup/SKILL.md"),
}

# Appended to the skill so the model knows what is specific to this host.
SKILL_NOTES = {
    "skills/iphone-use/SKILL.md": """
## 在豆包工作中使用

- 工具名为 `mcp__iphone_use__pua_*`，由自定义连接器 `iphone_use` 提供，只在「本地电脑」环境的任务里可用。本回合没有这些工具时，请用户到「插件 · 技能 · 伙伴」的「管理 > 连接器」确认 `iphone_use` 已启用，然后新开任务。
- 豆包工作自己也能操作这台 Mac 上的软件。用户说的是手机上的 App（或只说了 App 名而本机没有对应软件）时，用这里的 `pua_*` 工具在 iPhone 上完成，不要回答不支持该软件。
- 手机屏幕在豆包工作侧边栏，由 `pua_screen()` 打开。面板没有画面不影响控制通道，也不是任务失败；按工具返回的观察继续。
- 运行数据在 `~/.local/share/iphone-use`，与 Codex 版、WorkBuddy 版共用同一份配置、构建和操作锁；返回 `device_busy` 时可能是另一个宿主正在操作手机。
""",
    "skills/iphone-use-setup/SKILL.md": """
## 在豆包工作中使用

- 工具名为 `mcp__iphone_use__pua_*`，由自定义连接器 `iphone_use` 提供。本回合没有这些工具时，请用户到「插件 · 技能 · 伙伴」的「管理 > 连接器」确认 `iphone_use` 已启用并新开任务。
- 安装器已把 node、npm、git 和 Xcode 命令所在目录记在安装目录的 `doubao/host.json` 里，服务启动时补进 PATH。`pua_doctor` 报告缺少 node / npm 而本机实际已安装时，在源码目录重新运行 `python3 doubao/install.py` 刷新路径，再把连接器关闭后重新打开，不要重装 Node。
- 运行数据在 `~/.local/share/iphone-use`；Codex 版或 WorkBuddy 版已经配置并构建过时直接复用，不重新 fetch / configure / build。
""",
}

# The widget page. Doubao Work forwards a view's tool calls to the server, so the widget's
# frame poll and its toolbar work as upstream wrote them. What is kept from WorkBuddy's
# version of the page (host_text.SCREEN_SCRIPT) is the playback: the widget asks four times
# a second, each answer carries the frames captured since the last one, and they are drawn
# at the pace they were captured. The read that WorkBuddy needs in place of the call goes
# back to being the call, and the toolbar stays.
# The page cannot ask the frame source directly as it does in WorkBuddy: Doubao Work admits
# only https: and wss: origins to a view's connect-src, which a loopback listener is not.
FRAME_READ = """    return app.readServerResource({uri: "%s" + after + "/" + seen}, options).then(function (read) {
      var preview = JSON.parse(read.contents[0].text);
""" % base.FRAME_URI
FRAME_CALL = """    globalThis.__hostCall = true;
    try { var called = app.callServerTool(call, options); } finally { globalThis.__hostCall = false; }
    return called.then(function (result) {
      var preview = result.structuredContent;
      if (result.isError || !preview) throw new Error("The preview frame is unavailable.");
"""
PAGE = [
    ("<style>#toolbar{display:none!important}</style>\n", ""),
    (FRAME_READ, FRAME_CALL),
]
SCREEN_SCRIPT = base.rewrite(base.SCREEN_SCRIPT, PAGE, "workbuddy/host_text.py SCREEN_SCRIPT")
# The widget's frame poll goes to the script above, except the call that script makes itself.
SDK_FRAME_CALL = r'\g<0>if(\1&&\1.name==="pua_screen_frame"&&!globalThis.__hostCall)return globalThis.__workbuddyFrame(this,\1,\2);'


def screen_uri(upstream):
    """A host may cache the page by URI; a changed page needs a new one."""
    revision = hashlib.sha1((SCREEN_SCRIPT + SDK_FRAME_CALL).encode()).hexdigest()[:8]
    return upstream.replace(".html", f"-doubao-{revision}.html")


def screen_html(text, strict=True):
    """The widget page as Doubao Work should load it."""
    if "<head>\n" not in text or len(base.SDK_CALL.findall(text)) != 1:
        if strict:
            raise Drift("assets/phone-screen.html: <head> or the SDK's callServerTool not found exactly once")
        return text
    return base.SDK_CALL.sub(SDK_FRAME_CALL, text).replace("<head>\n", "<head>\n" + SCREEN_SCRIPT, 1)


def skill_text(relative, text, strict=True):
    """A packaged skill file as Doubao Work should read it; other files pass through."""
    text = base.rewrite(text, SKILLS.get(relative, ()), relative, strict)
    return text.rstrip("\n") + "\n" + SKILL_NOTES[relative] if relative in SKILL_NOTES else text


def adapt_server(module, strict=True):
    """Apply the host wording to an imported upstream iphone_use module."""
    module.INSTRUCTIONS = base.rewrite(module.INSTRUCTIONS, INSTRUCTIONS, "INSTRUCTIONS", strict)
    for tool in module.TOOLS:
        if tool["name"] in TOOL_DESCRIPTIONS:
            tool["description"] = base.rewrite(tool["description"], TOOL_DESCRIPTIONS[tool["name"]], tool["name"], strict)
