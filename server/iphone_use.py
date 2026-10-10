#!/usr/bin/env python3
"""Dependency-free local MCP stdio entrypoint for iPhone Use."""
import argparse
import base64
import collections
import fcntl
import inspect
import json
import math
import os
import queue
import re
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from wda_client import WDAClient, WDAError
from wda_controller import CALL_BUDGET, DEFAULT_MAX_NODES, PhoneController, TYPING_FREQUENCY
from wda_setup import SetupManager, state_directory
from wda_apps import AppCatalog
from wda_screen import ScreenHub
import wda_image
from analytics import Analytics
from phone_protocol import obj, string, num, BOOL, validate, result_content, copy_png, SCREEN_META, PROTOCOLS

PUAError=WDAError
VERSION="0.3.9"
SCREEN_URI="ui://iphone-use/phone-0.3.9.html"
# Codex scopes reuse to the host, chat, server and UI resource. A stable result
# ID keeps repeated READY/open/pause/resume calls in that chat on one panel,
# including after the MCP process reconnects; no device identifiers are needed.
SCREEN_SESSION_ID="iphone-use-screen"
# Seconds PUA may wait for animations to end before a post-action tree read; WDA_SETTLE_SECONDS overrides it.
SETTLE_SECONDS=0.8


SEL=obj({
 "label":string("Exact accessibility label copied from fresh nodes; real newlines and punctuation are preserved automatically.",max_length=1000),
 "label_contains":string("Substring of the label, for long or changing labels. Case-sensitive, encoded safely.",max_length=1000),
 "name":string("Exact accessibility identifier/name. Nodes omit name when it equals label.",max_length=1000),
 "value":string("Exact current accessibility value; not the text to enter. Omit values that change asynchronously.",max_length=1000),
 "type":string("Exact element type as nodes show it, e.g. Button. Not an application bundle ID.",max_length=1000),
 "enabled":{"oneOf":[{"type":"boolean"},{"type":"string","enum":["true","false"]}],"description":"Optional exact enabled filter. This does not prove hittability."},
 "index":{**num(0,199,"integer"),"description":"0-based position among the matches in tree order, as listed by pua_find or an ambiguous_target error. Only to choose between several matches."},
 "predicate":string("Advanced NSPredicate query used alone (index may accompany it). Prefer the exact fields so text is safely encoded.",max_length=2000)})
SEL["description"]="Exact label/name/value/type/enabled fields or label_contains, or a standalone predicate. rect, visible and in_viewport are observation fields, not selector fields. Matches nested at one place, or with only one on screen, resolve to that element; several separate matches return candidates with tap points and an index."
SEL["examples"]=[{"label":"返回","type":"Button"}]
SEL["minProperties"]=1
OBS=string("Post-action output, default none. Use tree/both when the next decision needs the resulting page; screenshot returns an image. This is next-step context, not proof of this action; expect/verify control verification.",enum=["none","tree","screenshot","both"])
OBS["default"]="none"
EXPECT={"expect":SEL,"observe":OBS}
VERIFY={"type":"boolean","default":False,"description":"Opt in to this operation's result check. Default false executes once and defers checking to the next required observation or final key checkpoint. Never blindly replay an uncertain mutation."}
REGION=obj({k:num(0 if k in ("x","y") else 1,10000) for k in ("x","y","width","height")},("x","y","width","height"))
REGION["description"]="Scroll rectangle in iPhone points, not screenshot pixels. observation_id is optional. Omit region for the central area, or use the actual list bounds. With verify=true, the region must fit current viewport and native modal bounds."
SCHEMAS={
 "observe":obj({"mode":string("Standalone observation output, default tree. Use mode here; observe is a post-action option on mutation tools. none is not a standalone observation mode.",enum=["tree","screenshot","both"]),"include_invisible":BOOL,"max_nodes":{**num(1,500,"integer"),"default":DEFAULT_MAX_NODES},"expensive_visibility":BOOL}),
 "find":obj({"selector":SEL,"limit":num(1,30,"integer")},("selector",)),
 "tap":obj({"selector":SEL,"x":num(0,10000),"y":num(0,10000),"observation_id":string("Optional ID from this Runtime. Checks app/viewport context, not whole-page pixel equality or age."),**EXPECT}),
 "swipe":obj({"direction":string("Finger movement; up usually reveals later rows. Default up.",enum=["up","down","left","right"]),"region":REGION,"observation_id":string("Optional ID from this Runtime; checks app/viewport context, not numeric/carousel text changes or age."),"expect":SEL,"verify":{**VERIFY,"description":"False: one gesture, no XML checks. True: check anchor movement once; failure returns a screenshot before another action. Numeric refresh is not progress."},"max_attempts":{**num(1,2,"integer"),"default":1,"description":"Legacy limit; even 2 stops after the first unproven gesture for screenshot inspection."},"observe":OBS}),
 "type_text":obj({"selector":SEL,"text":string("Full text to enter. Required with selector unless continue_token is given.",max_length=10000),"allow_newlines":BOOL,"submit":BOOL,"replace":BOOL,"verify":{**VERIFY,"description":"Default false enters the full intended text once without value readback. True checks exact value and stops before submit on mismatch. Secure fields require user takeover."},**EXPECT,"continue_token":string("Token from a result with input_complete=false. Pass it alone to type the rest of that text with its original options.",max_length=64)}),
 "press_button":obj({"name":string(enum=["home","volumeup","volumedown"]),"verify":VERIFY,**EXPECT},("name",)),
 "launch_app":obj({"bundle_id":string(),"verify":VERIFY,**EXPECT},("bundle_id",)),
 "wait":obj({"selector":SEL,"timeout_seconds":num(0,20)},("selector",)),
 "scroll_find":obj({"selector":SEL,"direction":string("Finger movement; up usually reveals later rows.",enum=["up","down","left","right"]),"max_swipes":{**num(0,10,"integer"),"default":1,"description":"0 only queries; positive legacy limits allow at most one swipe, then an unresolved target returns a screenshot for the next decision."}},("selector",)),
 "collect_list":obj({"row_type":string(),"max_pages":num(1,10,"integer"),"end_selector":SEL}),
 "apps":obj({"query":string(max_length=100),"country":string(max_length=2),"source":string(enum=["auto","catalog","installed","apple"]),"limit":num(1,30,"integer")},("query",)),
 "doctor":obj({}),"ready":obj({"screenshot":{"type":"boolean","default":True,"description":"Also verify screenshot; false retains status/session/source/viewport/unlock checks."},"recover":{"type":"boolean","default":True,"description":"Normal task startup: omit or set true, so a persistent local.pid/XCTest fault can queue one bounded restart of a proven owned PUA. Use false only for an explicitly requested diagnostic/no-restart check, not a routine precheck. False is respected and returns ready=false, state=recovery_required when restart is needed; queued recovery returns state=recovering. Neither state proves readiness."}}),"metrics":obj({"reset":{"type":"boolean","default":False,"description":"Return the totals, then start a new measurement window."}}),
 "setup":obj({"action":string(enum=["discover","fetch","configure","build","start","stop","status"]),"udid":string(),"team_id":string(),"bundle_id":string(),"source_dir":string(max_length=4096),"local_port":num(1024,65535,"integer"),"device_port":num(1024,65535,"integer"),"job_id":string(),"wait_seconds":{**num(0,30),"description":"Bounded wait for this job: start defaults to 20 seconds, status to 0. Use status with job_id and 20 to await an active start/recovery. Timeout keeps the job running; do not start again."}},("action",))
}
# Each batch operation carries the same closed argument schema as its standalone tool.
BATCH_OPS=["tap","swipe","type_text","launch_app","press_button","wait","observe","scroll_find"]
SCHEMAS["batch"]=obj({"steps":{"type":"array","minItems":1,"maxItems":20,"items":{"oneOf":[obj({"op":{"type":"string","const":op},"args":SCHEMAS[op]},("op","args")) for op in BATCH_OPS]}}},("steps",))
SCHEMAS["ready"]["examples"]=[{"recover":True,"screenshot":False}]
SCHEMAS["screen"]=obj({"action":string("Default open displays the live iPhone sidebar. Pause before password/Face ID takeover; resume only after the user confirms completion.",enum=["open","pause","resume"])})
SCHEMAS["screen_frame"]=obj({"after_seq":num(0,9007199254740991,"integer"),"last_event_id":num(0,9007199254740991,"integer")})
SCHEMAS["screen_action"]=obj({"action":string("refresh reconnects the preview stream, home returns the iPhone to its Home screen, screenshot copies a native capture to the Mac clipboard.",enum=["refresh","home","screenshot"])},("action",))
# Tools the preview App calls itself; the model never sees them.
APP_TOOLS=("screen_frame","screen_action")
DESCRIPTIONS={
 "doctor":"Diagnose local Xcode, USB devices, signing prerequisites and PUA health without changing the phone. Start here for setup.",
 "setup":"First phone use in this chat: call setup(status) directly before READY; reuse an active start/recovery job, or start once with the existing config/build. Start waits up to 20 seconds for service readiness; if pending, query status with the same job_id and wait_seconds=20. Then READY. Missing config/source/build uses iphone-use-setup. No blanket reinstall or extra approval for authorized startup; honor no-restart instructions. Never uninstalls apps.",
 "ready":"First phone use in this chat: setup(status), reuse/start and wait, then READY (recover=true); only ready=true permits phone tasks. Reuse the healthy channel afterward. pua_unreachable/not_ready requires setup, not task failure. recover=true handles owned runtime faults, not cold startup. For recovering/recovery_required follow guidance. Never replay phone actions.",
 "observe":"Fresh phone controls and/or a screenshot, with the iPhone point viewport and an observation_id. Nodes: type without the XCUIElementType prefix; rect=[x,y,width,height] in points; an omitted name equals label, an omitted value repeats the text, omitted enabled/visible/in_viewport are true. A listed node is not proven hittable: fixed headers and overlays can cover it. The screenshot is scaled for reading: image pixels x image.pixel_to_point [x,y] = points.",
 "find":"Query selector fields or a PUA predicate directly without a whole tree. Returns matches in tree order with index, type, texts and rect; this tool's selector documents the fields every selector accepts.",
 "tap":"Tap the element a selector resolves to after on-screen and hittable checks, or tap point coordinates with optional contextual observation_id. If the selector fails, the error returns a screenshot and tap points: tap by x/y in the next call instead of trying other selectors. Executes once optimistically; expect opts into a postcondition. Request tree/both if the next decision needs the new page.",
 "swipe":"One gesture; default verify=false/observe=none skips XML checks. verify=true checks geometry once; failure returns a screenshot even with none/tree, without another gesture. Inspect it before acting; no progress does not prove list completeness.",
 "type_text":"Enter the full intended Unicode text into an editable nonsecure field; no short-text trial or mandatory readback. Omit selector to type into the field that already has keyboard focus, which is how to continue after a selector failed: tap the field by x/y, then type. Send the whole text in one call: long text is typed in bounded requests, and a result with input_complete=false returns a continue_token to pass alone in the next call, after which verify/submit/expect/observe run. verify=true opts into exact readback before submit; expect opts into a page postcondition. Newlines need explicit intent; submit defaults false. Never replay uncertain input/submission.",
 "press_button":"Home uses the dedicated PUA homescreen endpoint once; default skips foreground polling. verify=true checks SpringBoard for Home, expect can check a page. Volume effects cannot be semantically verified.",
 "launch_app":"Activate once using a resolved bundle ID, optimistically by default. verify=true polls foreground up to five seconds; expect checks the intended page. Request observation for the next decision. Never blindly replay uncertain activation.",
 "wait":"Bounded semantic presence polling for expected target. Presence is a UI postcondition, not proof of business correctness.",
 "batch":"Up to 20 known steps in one model round trip. Routine unverified actions continue optimistically with intermediate observe=none. Stops on actual error, failed explicit check, uncertainty, submission without an explicit result expectation, unfinished long input (input_continues) or the per-call time budget (time_budget); continue from stopped_at without repeating completed steps. Observe the last step when the next decision needs page context.",
 "scroll_find":"Find a hittable target with at most one swipe. Ambiguity/occlusion stops immediately; an unresolved post-scroll query returns a screenshot. Inspect the end, region and overlays before deciding whether to swipe again; do not blindly repeat or raise the budget.",
 "collect_list":"Collect/deduplicate accessibility rows over bounded pages. Returns evidence and explicit coverage limits; always requires reconciliation before declaring business completeness.",
 "metrics":"In-process totals without text, app data or images: PUA HTTP time and bytes, tool time, response bytes per tool, and the wait between each response and the next tool request (host, model and user time). reset=true starts a new window."
}
DESCRIPTIONS["apps"]="Resolve a real bundle ID by installed-device inventory, bundled verified aliases, or Apple's Search API. Query app name before launch instead of guessing. Store metadata does not prove installation; check installed_verified and publisher/country."
READS={"doctor","observe","find","wait","metrics","apps"}
READS.update(("screen","screen_frame"))
DESCRIPTIONS["screen"]="Open or reuse the live iPhone screen in the Codex side panel. No phone actions or UI controls. Pause the preview before password/Face ID user takeover; resume after explicit completion. READY also opens or reuses this view by default."
DESCRIPTIONS["screen_frame"]="App-only cached live preview and action cursor events. Never reads XML, starts sessions or occupies the phone operation lock."
DESCRIPTIONS["screen_action"]="App-only toolbar of the live preview, pressed by the user: refresh the preview stream, send the iPhone Home, or copy a screenshot to the Mac clipboard. Refused while the preview is paused for authentication."


def undocumented(value):
    if isinstance(value,dict):return {k:undocumented(v) for k,v in value.items() if k not in ("description","examples")}
    if isinstance(value,list):return [undocumented(v) for v in value]
    return value


# Every selector has the same fields. pua_find publishes their documentation once; other
# tools publish the same closed shape with one line pointing there.
SEL_BRIEF={**undocumented(SEL),"description":"Selector; fields as documented on pua_find.selector."}
OBS_BRIEF={**undocumented(OBS),"description":"Post-action output for the next decision; default none."}


def published_schema(name):
    """Model-facing schema: same closed contract as SCHEMAS, with repeated documentation removed.

    Codex compacts a schema above 5KB and may drop argument branches, and all plugin
    tools share one description budget. Runtime validation keeps using SCHEMAS, without
    $ref parsing.
    """
    source=SCHEMAS[name]
    if name=="find":return source
    brief=name=="batch"
    counts={"selector":0,"observe":0}
    def count(value):
        if value==SEL:counts["selector"]+=1
        elif value==OBS:counts["observe"]+=1
        elif isinstance(value,dict):
            for item in value.values():count(item)
        elif isinstance(value,list):
            for item in value:count(item)
    count(source)
    shared={key for key,uses in counts.items() if uses>1}
    def publish(value):
        if value==SEL:return {"$ref":"#/$defs/selector"} if "selector" in shared else (undocumented(SEL) if brief else SEL_BRIEF)
        if value==OBS:return {"$ref":"#/$defs/observe"} if "observe" in shared else (undocumented(OBS) if brief else OBS_BRIEF)
        if isinstance(value,dict):return {k:publish(v) for k,v in value.items() if not (brief and k in ("description","examples"))}
        if isinstance(value,list):return [publish(v) for v in value]
        return value
    schema=publish(source)
    definitions={"selector":undocumented(SEL) if brief else SEL_BRIEF,"observe":undocumented(OBS) if brief else OBS_BRIEF}
    if shared:schema["$defs"]={key:definitions[key] for key in sorted(shared)}
    return schema


TOOLS=[{"name":"pua_"+name,"title":"Pua "+name.replace("_"," "),"description":DESCRIPTIONS[name],"inputSchema":published_schema(name),
        "annotations":{"readOnlyHint":name in READS,"destructiveHint":name not in READS,"idempotentHint":name in READS,"openWorldHint":name!="screen_frame"}} for name,schema in SCHEMAS.items()]
for tool in TOOLS:
    if tool["name"] in ("pua_ready","pua_screen"):
        tool["_meta"]={"ui":{"resourceUri":SCREEN_URI}}
    if tool["name"]=="pua_screen":
        tool.update(title="手机屏幕")
        tool["_meta"]["openai/ui"]={"entrypoints":[{"type":"thread"}]}
        tool["annotations"].update(readOnlyHint=False,destructiveHint=False,idempotentHint=True)
    if tool["name"][len("pua_"):] in APP_TOOLS:tool["_meta"]={"ui":{"visibility":["app"]}}
    if tool["name"]=="pua_screen_action":tool["annotations"].update(readOnlyHint=False,destructiveHint=False,idempotentHint=True)


def validate_semantics(name,args):
    from wda_controller import predicate
    for k in ("selector","expect","end_selector"):
        if k in args:predicate(args[k])
    if name=="tap":
        semantic="selector" in args
        coords=all(k in args for k in ("x","y"))
        if semantic==coords or (semantic and any(k in args for k in ("x","y"))):
            raise WDAError("invalid_argument","tap requires either selector or x/y; observation_id is optional.")
    if name=="type_text":
        if "continue_token" in args:
            if len(args)!=1:raise WDAError("invalid_argument","continue_token resumes the earlier call with its own options; pass it alone.",details={"action_executed":False})
            return
        if "text" not in args:raise WDAError("invalid_argument","type_text needs text, or continue_token alone.",details={"action_executed":False})
        if any(ord(c)<32 and c not in ("\n","\r") or ord(c)==127 for c in args["text"]):raise WDAError("invalid_argument","Control characters are not allowed in text.")
        if any(c in args["text"] for c in ("\n","\r")) and not args.get("allow_newlines",False):raise WDAError("newline_requires_intent","Line breaks require explicit multiline intent.")
    if name=="launch_app" and not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+",args["bundle_id"]):raise WDAError("invalid_argument","Use the app's verified bundle ID.")
    if name=="batch":
        for step in args["steps"]:validate_semantics(step["op"],step["args"])


def setting(name,default,low,high):
    """A bounded numeric tuning knob from the environment; anything else keeps the default."""
    try:value=float(os.environ.get(name,""))
    except ValueError:return default
    return value if math.isfinite(value) and low<=value<=high else default


class Runtime:
    def __init__(self,state_dir=None,base_url=None):
        root=state_directory(state_dir)
        self.state_dir=Path(root).expanduser().resolve();self.state_dir.mkdir(mode=0o700,parents=True,exist_ok=True);self.state_dir.chmod(0o700)
        # Configuration is private runtime data, never a project file.
        configured_url=None
        config=self.state_dir/"config.json"
        if config.is_file():
            try:configured_url="http://127.0.0.1:"+str(json.loads(config.read_text()).get("local_port",18100))
            except (ValueError,OSError):pass
        self.base_url=base_url or os.environ.get("WDA_URL") or configured_url or "http://127.0.0.1:18100"
        self.client=WDAClient(self.base_url)
        self.phone=PhoneController(self.client,self.state_dir)
        self.setup_manager=SetupManager(self.state_dir,self.base_url)
        self.apps=AppCatalog(self.state_dir,self.setup_manager)
        self.screen=ScreenHub(self.state_dir)
        self.phone.screen=self.screen
        self.phone.settle_seconds=setting("WDA_SETTLE_SECONDS",SETTLE_SECONDS,0,2)
        self.phone.call_budget=setting("WDA_CALL_BUDGET_SECONDS",CALL_BUDGET,5,240)
        self.phone.typing_frequency=int(setting("WDA_TYPING_FREQUENCY",TYPING_FREQUENCY,5,120))
        # One entry per model-facing response: size, and the idle gap before its request.
        self.responses=collections.deque(maxlen=500)
        self._replied_at=None
        self._device_lookup=None
        self.analytics=Analytics(self.state_dir,VERSION)

    def call(self,name,args):
        started=time.monotonic();data=None;error=None
        try:
            data=self._dispatch(name,args)
            return data
        except WDAError as exc:
            error=exc.code
            raise
        except Exception:
            error="internal_error"
            raise
        finally:
            try:self.analytics.tool_result(name,args,data,(time.monotonic()-started)*1000,error)
            except Exception:pass

    def _dispatch(self,name,args):
        # PUA has one active session. Serialize independent Codex MCP processes
        # sharing this runtime, and share only this plugin's session identity.
        if not isinstance(name,str) or not name.startswith("pua_") or name[4:] not in SCHEMAS:raise WDAError("unknown_tool","Unknown PUA tool.")
        validate(args,SCHEMAS[name[4:]]);validate_semantics(name[4:],args)
        if name in ("pua_screen","pua_screen_frame","pua_screen_action"):self.identify_device()
        # Cached preview polling does not share the PUA action/session lock.
        if name=="pua_screen_frame":return self.screen.frame(**args)
        if name=="pua_screen_action":return self.screen_action(args["action"])
        if name=="pua_screen":
            action=args.get("action","open")
            if action=="pause":self.screen.set_paused(True)
            elif action=="resume":self.screen.set_paused(False)
            return self.screen.start()
        lock_path=self.state_dir/"operation.lock"
        cache_path=self.state_dir/"session.json"
        with lock_path.open("a") as lock:
            lock_path.chmod(0o600)
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise WDAError("device_busy","Another iPhone Use operation is running. Wait for it to finish before continuing; no action was executed.")
            try:
                if cache_path.is_file() and hasattr(self.client,"session_id"):
                    try:
                        cached=json.loads(cache_path.read_text())
                        sid=cached.get("session_id","")
                        if cached.get("url")==self.base_url and isinstance(sid,str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}",sid):self.client.session_id=sid
                    except (ValueError,OSError,AttributeError):pass
                return self._call(name,args)
            finally:
                sid=getattr(self.client,"session_id",None)
                if sid:
                    task_temp=self.state_dir/("session-"+str(os.getpid())+".tmp")
                    try:
                        fd=os.open(task_temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                        with os.fdopen(fd,"w") as stream:json.dump({"url":self.base_url,"session_id":sid},stream)
                        os.replace(task_temp,cache_path)
                    finally:
                        if task_temp.exists():task_temp.unlink()
                elif hasattr(self.client,"session_id") and cache_path.exists():cache_path.unlink()
                fcntl.flock(lock,fcntl.LOCK_UN)

    def identify_device(self):
        """Find the phone's model name for the preview header once, away from the request path."""
        if self._device_lookup is not None:return
        def lookup():
            udid=self.setup_manager.config.get("udid")
            # Nothing is configured yet: there is no phone to name, and nothing to look up.
            if not isinstance(udid,str) or not udid:return
            cache=self.state_dir/"device.json"
            try:
                known=json.loads(cache.read_text())
                if known.get("udid")==udid and isinstance(known.get("model"),str):
                    self.screen.device={"model":known["model"][:60]};return
            except (OSError,ValueError,AttributeError):pass
            try:
                model=next((d.get("model") for d in self.setup_manager.discover().get("devices",[]) if udid and d.get("udid")==udid),None)
                if isinstance(model,str) and model:
                    self.screen.device={"model":model[:60]}
                    fd=os.open(cache,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                    with os.fdopen(fd,"w") as stream:json.dump({"udid":udid,"model":model},stream)
            except Exception:
                # The header falls back to a generic name; the preview must never fail over a label.
                pass
        self._device_lookup=threading.Thread(target=lookup,name="wda-device-model",daemon=True)
        self._device_lookup.start()

    def screen_action(self,action):
        """Toolbar of the preview App: the user's own click, outside the model's tool sequence."""
        if action=="refresh":
            pause_id=self.screen.reconnect_pause_id()
            state=self.screen.restart()
            probe=WDAClient(self.base_url,timeout=2)
            try:
                ready=(probe.request("GET","/status").get("value") or {}).get("ready") is True
                if ready:
                    if probe.request("GET","/wda/locked").get("value") is False:
                        # Clicking refresh is the user's explicit request to resume.
                        self.screen.resume_after_unlock(pause_id,explicit=True)
                    else:self.screen.set_paused(True,reason="device_locked")
            except WDAError:ready=False
            finally:probe.close()
            return {"ok":True,"action":action,"service_ready":ready,**state,**self.screen.pause_status()}
        if self.screen.paused():
            raise WDAError("preview_paused","The preview is paused while the user authenticates on the iPhone. No capture or phone action was made.",details={"action_executed":False})
        client=WDAClient(self.base_url,timeout=8)
        try:
            if action=="home":
                lock_path=self.state_dir/"operation.lock"
                with lock_path.open("a") as lock:
                    lock_path.chmod(0o600)
                    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:raise WDAError("device_busy","The iPhone is in the middle of another operation. Try again in a moment; nothing was sent.",details={"action_executed":False})
                    try:client.request("POST","/wda/homescreen",{})
                    finally:fcntl.flock(lock,fcntl.LOCK_UN)
                self.phone.external_action()
                return {"ok":True,"action":action}
            encoded=client.request("GET","/screenshot").get("value")
            try:data=base64.b64decode(encoded,validate=True)
            except (ValueError,TypeError):raise WDAError("invalid_response","Invalid screenshot encoding.")
            size=wda_image.png_size(data[:32])
            if size is None:raise WDAError("invalid_response","PUA screenshot is not PNG.")
            copy_png(self.state_dir,data)
            return {"ok":True,"action":action,"copied":True,"width":size[0],"height":size[1]}
        finally:client.close()

    @staticmethod
    def channel_fault(error):
        text=str(error).lower()
        if "not authorized for performing ui testing actions" in text or ("xctdaemonerrordomain" in text and "code=41" in text):
            return "xctest_authorization"
        if error.code=="pua_foreground_unavailable" or ("local.pid." in text and error.code=="stale element reference"):
            return "foreground_unavailable"
        return None

    def ready_once(self,screenshot):
        try:lock_pause=self.screen.locked_pause_id()
        except Exception:lock_pause=None
        status=self.client.request("GET","/status").get("value") or {}
        if status.get("ready") is not True:raise WDAError("not_ready","PUA is not accepting commands. Run pua_doctor and inspect setup status.")
        if self.client.request("GET","/wda/locked").get("value") is not False:raise WDAError("phone_locked","Unlock the iPhone yourself, keep it awake, and verify READY again.")
        sid=self.client.ensure_session()
        observation=self.phone.observe("both" if screenshot else "tree")
        if observation.get("total_nodes",0)==0 and self.setup_manager.mirroring_running():raise WDAError("mirroring_conflict","iPhone Mirroring is running and PUA exposes an empty phone tree. Quit Mirroring, unlock if needed, then verify READY again.")
        result={"ready":True,"state":"ready","proof":{"status_ready":True,"phone_unlocked":True,"session_usable":bool(sid),"foreground_resolved":True,"source_readable":True,"viewport_readable":True,"screenshot_readable":screenshot},"observation":observation}
        try:
            self.screen.resume_after_unlock(lock_pause)
            result["preview"]=self.screen.pause_status()
        except Exception:pass  # Display state must not invalidate a healthy control channel.
        return result

    def recovering_result(self,info,screenshot,recover,retried=False,cause=None):
        info=dict(info)
        job_id=info["job_id"]
        self.client.close();self.client.session_id=None;self.phone.reset()
        info.update(status_tool="pua_setup",status_arguments={"action":"status","job_id":job_id},next_tool="pua_ready",next_arguments={"screenshot":screenshot,"recover":recover},retry_after_seconds=1,replay_action=False)
        result={"ready":False,"state":"recovering","message":"Owned PUA recovery is running in the background. Poll the supplied setup job; once the service is reachable, run READY again. No phone task may proceed until ready=true. Do not replay the failed user action.","action_executed":False,"category":"channel_runtime","session_read_retried":retried,"recovery":info}
        if cause is not None:result["cause"]=cause.as_dict()
        return result

    def ready(self,screenshot=True,recover=True):
        pending=self.setup_manager.pending_recovery() if hasattr(self.setup_manager,"pending_recovery") else None
        if pending and pending.get("job_id"):
            # The old listener may still answer before the owned worker stops
            # it. Never return that soon-to-be-invalid session as READY.
            return self.recovering_result(pending,screenshot,recover)
        retried=False
        try:return self.ready_once(screenshot)
        except WDAError as error:
            original=error
        fault=self.channel_fault(original)
        if fault=="foreground_unavailable":
            # The one retry is a fresh session/source read, never a replay of
            # a click, key, submission or an element ID from an older session.
            self.client.close();self.client.session_id=None;self.phone.reset()
            retried=True
            try:
                result=self.ready_once(screenshot)
                result["recovery"]={"state":"read_recovered","session_recreated":True,"replayed_action":False}
                return result
            except WDAError as error:
                original=error;fault=self.channel_fault(error)
        if original.code=="phone_locked":raise original
        pending=self.setup_manager.pending_recovery() if hasattr(self.setup_manager,"pending_recovery") else None
        if pending and original.code in ("pua_unreachable","not_ready","invalid session id","pua_foreground_unavailable","stale element reference","unknown error","invalid argument"):
            recovery={"ok":True,"recovery":pending,"job_id":pending.get("job_id")}
        elif fault and recover:
            recovery=self.setup_manager.recover()
        else:
            if fault:
                return {"ready":False,"state":"recovery_required","reason":"recovery_disabled","message":"The PUA/XCTest channel needs recovery, but this call explicitly disabled service restart. No recovery was started. Resume with recover=true only when allowed by the current user instructions.","action_executed":False,"category":"channel_runtime","session_read_retried":retried,"cause":original.as_dict(),"recovery":{"state":"disabled","next_tool":"pua_ready","next_arguments":{"screenshot":screenshot,"recover":True},"permission_note":"Honor any user instruction forbidding restart; do not automatically override it.","replay_action":False}}
            if original.code in ("pua_unreachable","not_ready"):
                # A stopped service needs setup/start, not an owned-listener restart.
                # Keep the failure truthful, but give the next read-only diagnostic.
                original.details.update(ready=False,action_executed=False,initialization_required=True,
                    recovery={"next_tool":"pua_setup","next_arguments":{"action":"status"},"replay_action":False,
                              "next_step":"Continue initialization; do not end the phone task solely because PUA is not started. Inspect configured/jobs/service: reuse an active start/recovery job, or start once from the existing configuration/build if permitted, then verify READY. Missing prerequisites use iphone-use-setup. Honor explicit no-restart or read-only instructions."})
            raise original
        info=dict(recovery.get("recovery") or {})
        job_id=recovery.get("job_id") or info.get("job_id")
        if recovery.get("ok") and job_id:
            info["job_id"]=job_id
            return self.recovering_result(info,screenshot,recover,retried,original)
        info.update(next_steps=recovery.get("next_steps",[]),replay_action=False)
        raise WDAError("pua_recovery_required","Automatic PUA recovery was not started: "+str(recovery.get("error","service ownership could not be proven")),details={"ready":False,"action_executed":False,"category":"channel_runtime","session_read_retried":retried,"cause":original.as_dict(),"recovery":info})

    def _call(self,name,args):
        if not isinstance(name,str) or not name.startswith("pua_") or name[4:] not in SCHEMAS:raise WDAError("unknown_tool","Unknown PUA tool.")
        op=name[4:];validate(args,SCHEMAS[op]);validate_semantics(op,args)
        start=time.monotonic();error=None;activity=None
        try:
            if op not in ("doctor","apps","setup","metrics"):
                try:activity=self.screen.begin(op)
                except Exception:pass
            if op=="doctor":return self.setup_manager.doctor()
            if op=="apps":return self.apps.lookup(**args)
            if op=="setup":
                manager=self.setup_manager
                candidate_url=self.base_url
                if args["action"]=="configure" and "local_port" in args:
                    candidate_url="http://127.0.0.1:"+str(args["local_port"])
                    manager=SetupManager(self.state_dir,candidate_url)
                result=manager.setup(**args)
                if result.get("ok") and args["action"] in ("stop","configure","start"):
                    self.client.close();self.client.session_id=None;self.phone.reset()
                if result.get("ok") and args["action"]=="configure" and "local_port" in args:
                    self.base_url=candidate_url;self.setup_manager=manager
                    self.apps.setup_manager=manager
                    self.client=WDAClient(self.base_url);self.phone.client=self.client
                return result
            if op=="ready":
                try:self.screen.start()
                except Exception:pass
                return self.ready(**args)
            if op=="metrics":
                result=self.metrics()
                if args.get("reset"):
                    self.client.clear_metrics();self.phone.tool_records.clear();self.responses.clear();self._replied_at=None
                return result
            if op in ("tap","swipe","type_text","launch_app","press_button","batch","scroll_find","collect_list"):
                if self.client.request("GET","/wda/locked").get("value") is not False:raise WDAError("phone_locked","Unlock the iPhone yourself before operations; observe again afterward.")
            return getattr(self.phone,op)(**args)
        except WDAError as exc:
            error=exc.code
            if exc.code=="phone_locked" or (exc.code=="not_editable" and exc.details.get("secure_field")):
                try:self.screen.set_paused(True,reason="device_locked" if exc.code=="phone_locked" else "authentication")
                except Exception:pass
            if op in READS:
                exc.details.setdefault("action_executed",False)
            if self.channel_fault(exc):
                exc.details.update(category="channel_runtime",recovery={"tool":"pua_ready","arguments":{"screenshot":False},"replay_action":False})
            raise
        except (ValueError,TypeError) as exc:error="invalid_argument";raise WDAError(error,str(exc)) from exc
        finally:
            if activity is not None:
                try:self.screen.end(activity)
                except Exception:pass
            self.phone.tool_records.append({"tool":name,"seconds":round(time.monotonic()-start,4),"error":error})

    def metrics(self):
        records=list(self.phone.tool_records);responses=list(self.responses)
        def grouped(items,fields):
            groups={}
            for item in items:
                entry=groups.setdefault(item["tool"],{"count":0,**{field:0 for field in fields}})
                entry["count"]+=1
                for field in fields:entry[field]=round(entry[field]+(item[field] or 0),4)
            return groups
        waits=[item["wait_seconds"] for item in responses if item["wait_seconds"] is not None]
        return {**self.client.metrics(),
                "tools":{"count":len(records),"seconds":round(sum(r["seconds"] for r in records),4),"errors":sum(bool(r["error"]) for r in records),
                         "by_tool":grouped([{**r,"errors":int(bool(r["error"]))} for r in records],("seconds","errors"))},
                "responses":{"count":len(responses),"text_bytes":sum(r["text_bytes"] for r in responses),"image_bytes":sum(r["image_bytes"] for r in responses),
                             "by_tool":grouped(responses,("text_bytes","image_bytes"))},
                "rounds":{"count":len(responses),"waits":len(waits),"wait_seconds":round(sum(waits),3),
                          "median_wait_seconds":round(statistics.median(waits),3) if waits else None,"max_wait_seconds":round(max(waits),3) if waits else None,
                          "scope":"Time from one tool response to the next tool request: host, model and user time together. A long wait may be the user, not the model."},
                "latency_scope":"HTTP, tool time and response sizes are measured here. The wait between calls is observed, not controlled; this plugin cannot split it into model, host and user time."}

    def note_response(self,name,result,arrived):
        """Record what one model-facing response cost and how long the previous one waited for it."""
        if name=="pua_screen_frame":return
        content=result.get("content",[])
        self.responses.append({"tool":name if isinstance(name,str) else "invalid",
            "text_bytes":sum(len(item["text"].encode()) for item in content if item.get("type")=="text"),
            "image_bytes":sum(len(item["data"]) for item in content if item.get("type")=="image"),
            "wait_seconds":None if self._replied_at is None else max(0.0,arrived-self._replied_at)})

    def replied(self):self._replied_at=time.monotonic()

    def close(self):
        try:self.screen.close();self.client.close()
        finally:self.analytics.close()


def tool_result(runtime,params):
    name=params.get("name")
    try:
        data=runtime.call(name,params.get("arguments",{}))
        # The preview App reads structuredContent; frames never become model text.
        if name=="pua_screen_frame":return {"content":[],"structuredContent":data,"isError":False}
        result=result_content(data,structured=name in ("pua_screen","pua_screen_action"))
        if name in ("pua_ready","pua_screen"):
            result["_meta"]={"openai/widgetSessionId":SCREEN_SESSION_ID}
        return result
    except WDAError as exc:return result_content({"error":exc.as_dict()},structured=name=="pua_screen_action")
    except Exception as exc:
        print("iphone-use tool failure: "+type(exc).__name__,file=sys.stderr)
        return result_content({"error":{"code":"internal_error","message":"Local tool failed; inspect setup status or local stderr.","uncertain":True}})


INSTRUCTIONS=(
 "PUA means Phone Use Agent; all iPhone Use tools use the pua_ prefix. "
 "Read iphone-use-setup before setup and iphone-use for tasks. First phone use in this chat: pua_setup(action=status) before any READY; reuse a ready service or active job, otherwise start once with the existing build. start waits up to 20 seconds; pending jobs use status(job_id, wait_seconds=20), then pua_ready(recover=true, screenshot=false); only ready=true permits phone tasks, then reuse READY's observation and the healthy channel. "
 "If READY fails with pua_unreachable/not_ready, continue initialization rather than end the task: pua_setup(action=status), reuse an active start/recovery job or start once from the existing config/build, poll that job until service.ready=true, then READY again. Missing config/source/build uses the setup skill. "
 "recover=true is runtime recovery, not cold startup; for state=recovering follow its setup job until the service is ready, then READY again. Honor explicit diagnostic/no-start/no-restart instructions. "
 "The live iPhone screen opens or reuses the same side panel with READY; setup/recovery and preview pause/resume keep the existing panel. Use pua_screen to reopen a closed panel, not to refresh an already open one. Opening it does not prove readiness or require an extra user confirmation, and widget frames never substitute for a model observation or final verification. "
 "Results are one compact JSON text. Tree nodes give type without the XCUIElementType prefix and rect=[x,y,width,height] in iPhone points; an omitted name equals label, an omitted value repeats the text, omitted enabled/visible/in_viewport are true. A listed node is not proven hittable: fixed headers and overlays can cover it. "
 "A screenshot arrives as an image in the same result; through functions.exec forward each image block with image(block) and text blocks with text(block.text), never text(the whole result) or base64. If image forwarding is unavailable, use view_image on image.path or error.observation.image.path. It is scaled for reading: image pixels x image.pixel_to_point [x,y] = iPhone points. Standalone observation uses mode, mutation output uses observe. "
 "Selectors copy label/name/value/type from fresh nodes; use label_contains for long or changing labels. Matches nested at one place, or with only one on screen, resolve by themselves. "
 "Screenshot inspection is the fallback for abnormal UI state: selector/focus failure, unresolved scroll search, no scroll progress, changed/blocked scroll context, input mismatch or a failed page expectation. Inspect the attached screenshot FIRST before any further mutation; if no usable image is attached take one pua_observe(mode=screenshot). Decide from visible state whether to stop, handle a popup, change the region/direction, tap by x/y or continue missing work. scroll_find never chains another swipe after an unresolved post-scroll query. tap_point/candidates locate elements but do not prove they are unobstructed; close a visible popup before tapping a covered background target. For input tap the visible editable field, then type_text with text and no selector. If a coordinate tap or focus failed, choose a new target from a fresh screenshot rather than repeat the same point or hand routine UI trouble to the user. Do not try other selector spellings or read the tree again first. Correct schema/channel/authentication errors by their own recovery; never blindly replay uncertain actions. Resolve unknown bundle IDs with pua_apps. "
 "Execute routine actions optimistically: observe=none and verify=false are defaults, verified=false/verification_deferred=true is normal and does not require a separate verification call. If the next decision needs the resulting page, request observe=tree/both in the action and inspect previous success while planning that next step. "
 "Chain known steps in batch; do not batch speculative repeated swipes toward an unknown target. Inspect the next page first, using a screenshot when tree truncation hides the boundary. explicit expect/verify opts into checking key outcomes. Send long text whole: when type_text returns input_complete=false, call it again with only continue_token; a batch stopped by input_continues or time_budget continues from stopped_at. "
 "Verify final critical results before reporting completion. Retry or replan only after observing a definite failure; never replay uncertain input/submission or an already executed multi-step operation wholesale. No_scroll_progress from explicit verification does not prove empty/complete data. "
 "For App passwords or Face ID call pua_screen(action=pause); phone_locked already auto-pauses the preview for device unlock, so do not overwrite that reason with an extra pause. Pause phone calls and use the available host question tool (request_user_input_async in Default), first option exactly 已完成继续. Wait for the actual user answer; async return or preselection is not confirmation. After the actual user completion answer, explicitly pua_screen(action=resume) for App authentication or legacy/unknown pause, then read fresh state. For device unlock run READY once; successful READY clears only its matching device_locked pause. READY/open never clear App authentication or unknown pause. Continue remaining work from that fresh state. "
 "Operation action_complete/verified fields do not mean the user's entire task is complete. Track all deliverables, give commentary progress and continue tools while work remains; final only after completion or a concrete blocker. For an unavailable MCP binding use the skill's direct Runtime fallback with the same operation lock."
)


def serve(runtime):
    """Read requests on this thread; run phone tools in order on one worker.

    The preview App polls frames several times a second. Answering those, pings and the
    catalog here keeps the preview live and the host responsive while a long phone
    operation is still running. Phone tools stay strictly sequential.
    """
    writing=threading.Lock()
    def send(response):
        line=json.dumps(response,ensure_ascii=False,allow_nan=False)
        with writing:print(line,flush=True)
    jobs=queue.Queue()
    def work():
        while True:
            job=jobs.get()
            if job is None:return
            ident,params,arrived=job
            result=tool_result(runtime,params)
            runtime.note_response(params.get("name"),result,arrived)
            send({"jsonrpc":"2.0","id":ident,"result":result})
            runtime.replied()
    worker=threading.Thread(target=work,name="wda-tools",daemon=True)
    worker.start()
    try:
        for line in sys.stdin:
            request=None
            try:
                if len(line)>1024*1024:raise ValueError("Request exceeds 1 MiB")
                request=json.loads(line,parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
                if not isinstance(request,dict) or request.get("jsonrpc")!="2.0" or not isinstance(request.get("method"),str):raise ValueError("Invalid request")
                if "id" not in request:continue
                ident=request["id"]
                if isinstance(ident,bool) or not isinstance(ident,(str,int)):raise ValueError("Invalid identifier")
                params=request.get("params",{})
                if not isinstance(params,dict):raise WDAError("invalid_argument","params must be an object.")
                method=request["method"]
                if method=="initialize":
                    offered=params.get("protocolVersion")
                    result={"protocolVersion":offered if offered in PROTOCOLS else PROTOCOLS[0],"capabilities":{"tools":{"listChanged":False},"resources":{"listChanged":False}},"serverInfo":{"name":"iphone-use","version":VERSION},"instructions":INSTRUCTIONS}
                    try:runtime.analytics.start()
                    except Exception:pass
                elif method=="ping":result={}
                elif method=="tools/list":result={"tools":TOOLS}
                elif method=="resources/list":
                    result={"resources":[{"uri":SCREEN_URI,"name":"iPhone Use Screen","title":"手机屏幕","mimeType":"text/html;profile=mcp-app","_meta":SCREEN_META}]}
                elif method=="resources/read":
                    if params.get("uri")!=SCREEN_URI:raise WDAError("invalid_argument","Unknown screen resource URI.")
                    html=(Path(__file__).resolve().parents[1]/"assets/phone-screen.html").read_text()
                    result={"contents":[{"uri":SCREEN_URI,"mimeType":"text/html;profile=mcp-app","text":html,"_meta":SCREEN_META}]}
                elif method=="tools/call":
                    arrived=time.monotonic()
                    if params.get("name") not in ("pua_screen_frame","pua_screen","pua_screen_action"):
                        jobs.put((ident,params,arrived));continue
                    # Preview polls, its toolbar and opening or pausing the panel never wait behind a phone operation.
                    result=tool_result(runtime,params)
                    if params.get("name")=="pua_screen":
                        runtime.note_response("pua_screen",result,arrived);runtime.replied()
                else:
                    send({"jsonrpc":"2.0","id":ident,"error":{"code":-32601,"message":"Method not found"}});continue
                response={"jsonrpc":"2.0","id":ident,"result":result}
            except WDAError as exc:response={"jsonrpc":"2.0","id":request.get("id") if isinstance(request,dict) else None,"error":{"code":-32602,"message":str(exc)}}
            except (ValueError,TypeError,KeyError):response={"jsonrpc":"2.0","id":request.get("id") if isinstance(request,dict) else None,"error":{"code":-32700 if request is None else -32600,"message":"Parse error" if request is None else "Invalid request"}}
            send(response)
    finally:
        # Finish the phone operation already accepted, then stop: its outcome must be reported.
        jobs.put(None);worker.join()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--state-dir");parser.add_argument("--url");parser.add_argument("--doctor",action="store_true");parser.add_argument("--ready",action="store_true")
    args=parser.parse_args();runtime=Runtime(args.state_dir,args.url)
    try:
        if args.doctor or args.ready:
            try:data=runtime.call("pua_doctor" if args.doctor else "pua_ready",{})
            except WDAError as exc:data={"error":exc.as_dict()}
            print(json.dumps(data,ensure_ascii=False));return 1 if "error" in data or (args.ready and data.get("ready") is not True) else 0
        serve(runtime);return 0
    finally:runtime.close()


if __name__=="__main__":sys.exit(main())
