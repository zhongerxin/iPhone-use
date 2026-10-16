# Tool schemas and validation from iPhone Use. MIT, copyright 2026 ZHONG XIN.
import math
import re
from wda_client import WDAError

def obj(properties,required=()):
    return {"type":"object","properties":properties,"required":list(required),"additionalProperties":False}


def string(description="",max_length=1000,enum=None):
    result={"type":"string","minLength":1,"maxLength":max_length}
    if description:result["description"]=description
    if enum:result["enum"]=enum
    return result


def num(low,high,kind="number"):
    return {"type":kind,"minimum":low,"maximum":high}


BOOL={"type":"boolean"}
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
 "observe":obj({"mode":string("Standalone observation output, default tree. Use mode here; observe is a post-action option on mutation tools. none is not a standalone observation mode.",enum=["tree","screenshot","both"]),"include_invisible":BOOL,"max_nodes":num(1,500,"integer"),"expensive_visibility":BOOL}),
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
 "setup":obj({"action":string(enum=["discover","fetch","configure","build","start","stop","status"]),"udid":string(),"team_id":string(),"bundle_id":string(),"source_dir":string(max_length=4096),"local_port":num(1024,65535,"integer"),"device_port":num(1024,65535,"integer"),"job_id":string()},("action",))
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
 "setup":"Initialize/start PUA when READY is unreachable or not_ready: status first, reuse an active start/recovery job, or start once with the existing config/build. Poll its job until service.ready=true, then READY again. Missing config/source/build uses iphone-use-setup. No blanket reinstall or extra approval for authorized startup; honor no-restart instructions. Never uninstalls apps.",
 "ready":"First phone task in a new chat: initialize with READY (recover=true or omitted); only ready=true permits phone tasks. Reuse this chat's healthy channel afterward. pua_unreachable/not_ready is a setup branch, not final task failure: setup(status), reuse an active job or start once, then READY again. recover=true handles owned runtime faults; it does not cold-start a stopped service. For state=recovering/recovery_required follow guidance. Never replay phone actions.",
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

def validate(value,schema,path="arguments"):
    if "oneOf" in schema:
        if isinstance(value,dict) and "op" in value:
            # Surface the chosen batch step's precise argument error instead
            # of hiding it behind the union of unrelated operation schemas.
            chosen=[candidate for candidate in schema["oneOf"] if candidate.get("properties",{}).get("op",{}).get("const")==value["op"]]
            if len(chosen)==1:return validate(value,chosen[0],path)
        matches=0
        for candidate in schema["oneOf"]:
            try:validate(value,candidate,path);matches+=1
            except WDAError:pass
        if matches!=1:raise WDAError("invalid_argument",f"{path} must match exactly one allowed operation schema.")
        return
    kind=schema.get("type")
    valid={"object":lambda:isinstance(value,dict),"array":lambda:isinstance(value,list),"string":lambda:isinstance(value,str),"boolean":lambda:isinstance(value,bool),"number":lambda:not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value),"integer":lambda:not isinstance(value,bool) and isinstance(value,int)}
    if kind and not valid[kind]():raise WDAError("invalid_argument",f"{path} must be {kind}.")
    if "const" in schema and value!=schema["const"]:raise WDAError("invalid_argument",f"Invalid {path} operation.")
    if "enum" in schema and value not in schema["enum"]:raise WDAError("invalid_argument",f"Invalid {path} option.")
    if kind=="object":
        props=schema.get("properties",{})
        if schema.get("additionalProperties") is False and set(value)-set(props):raise WDAError("invalid_argument",f"Unknown fields in {path}: {', '.join(sorted(set(value)-set(props)))}. Use only the declared fields.",details={"action_executed":False,"argument_path":path,"unknown_fields":sorted(set(value)-set(props)),"allowed_fields":sorted(props),"recovery":{"next_step":"Correct these fields using the current tool schema, then call once; no device action was executed."}})
        if set(schema.get("required",[]))-set(value):raise WDAError("invalid_argument",f"Missing required fields in {path}.")
        if len(value)<schema.get("minProperties",0):raise WDAError("invalid_argument",f"{path} cannot be empty.")
        for k,v in value.items():
            if k in props:validate(v,props[k],path+"."+k)
    if kind in ("number","integer") and not schema.get("minimum",-math.inf)<=value<=schema.get("maximum",math.inf):raise WDAError("invalid_argument",f"{path} is out of range.")
    if kind=="string" and not schema.get("minLength",0)<=len(value)<=schema.get("maxLength",100000):raise WDAError("invalid_argument",f"{path} has invalid length.")
    if kind=="array":
        if not schema.get("minItems",0)<=len(value)<=schema.get("maxItems",100000):raise WDAError("invalid_argument",f"{path} has invalid number of items.")
        for i,v in enumerate(value):validate(v,schema["items"],f"{path}[{i}]")


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
