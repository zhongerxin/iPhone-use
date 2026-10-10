"""Optimistic iPhone operations with optional observations and explicit verification."""
import base64
import collections
import datetime as dt
from functools import wraps
import json
import math
from pathlib import Path
import re
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote
from wda_client import WDAError
import wda_image
from wda_text import request_timeout, split_text

ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"
TYPE_PREFIX = "XCUIElementType"
EDITABLE = ("XCUIElementTypeTextField","XCUIElementTypeSearchField","XCUIElementTypeTextView")
SELECTOR_FIELDS = {"label","label_contains","name","value","type","enabled","predicate","index"}
# Rects of this many matches are read one by one when WDA answers with bare element IDs.
CANDIDATE_LIMIT = 8
# Bounds checks reuse the viewport briefly; an observation or a supplied ID reads it again.
VIEWPORT_TTL = 30
# One typing request stays short; one tool call stops typing at the budget and hands back a token.
TYPING_FREQUENCY = 30
TYPING_PIECE = 200
CALL_BUDGET = 90
INPUT_TTL = 600
# A selector that leads to no action is not retried in other spellings: the error hands back
# the screen so the very next call can act by coordinates.
SELECTOR_FAILURES = ("no_such_element","ambiguous_target","occluded_target","offscreen_target","not_editable","no_focused_field","search_exhausted")
VISUAL_FAILURES = SELECTOR_FAILURES + ("no_scroll_progress","scroll_context_changed","modal_requires_region","blocked_scroll_region","postcondition_failed","input_mismatch",
                                      "element not interactable","element click intercepted","invalid element state","no such element")


def fail(code, message, **details):
    raise WDAError(code, message, details=details)


def stale(message,reason,scope="page",**details):
    fail("stale_observation",message,action_executed=False,reason=reason,freshness_scope=scope,
         recovery={"next_tool":"pua_observe","next_arguments":{"mode":"both"},"same_observation_retry":False,"replay_action":False,
                   "next_step":"Inspect the current app and viewport before reusing coordinates. An observation ID is optional; changing text, numbers or screenshot pixels does not invalidate its app/viewport context."},**details)


def finite(value, name, low=0, high=10000):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or not low <= value <= high:
        fail("invalid_argument", f"{name} must be a finite number in [{low}, {high}].")
    return value


def integer(value, name, low, high):
    finite(value, name, low, high)
    if not isinstance(value,int):
        fail("invalid_argument", f"{name} must be an integer.")
    return value


def action_result(function):
    @wraps(function)
    def wrapped(self,*args,**kwargs):
        before=self.accepted_actions
        try:return function(self,*args,**kwargs)
        except WDAError as error:
            if self.accepted_actions>before:
                error.details.update(action_executed=True,action_complete=False)
                if function.__name__=="swipe":
                    error.details.setdefault("attempts",self.accepted_actions-before)
                error.details.setdefault("verification_required","At least one phone action was accepted before the error. Read actual state; do not automatically replay the operation.")
            self.switch_to_coordinates(error,typing=function.__name__=="type_text")
            raise
    return wrapped


def predicate_literal(value):
    # NSPredicate rejects literal line breaks inside quoted strings. Its Unicode
    # escapes preserve exact text, including a literal backslash followed by n.
    return "".join("\\\\" if c=="\\" else "\\'" if c=="'" else f"\\u{ord(c):04x}" if ord(c)<32 or ord(c)==127 or c in "\u2028\u2029" else c for c in value)


def predicate(selector):
    if not isinstance(selector,dict) or not selector or set(selector)-SELECTOR_FIELDS:
        fail("invalid_selector", "Use label/label_contains/name/value/type/enabled, or a standalone PUA predicate; index picks one match.")
    if "index" in selector:
        index=selector["index"]
        if isinstance(index,bool) or not isinstance(index,int) or not 0 <= index <= 199:
            fail("invalid_selector","index must be an integer 0..199 choosing one match in tree order.")
    criteria={key:value for key,value in selector.items() if key!="index"}
    if not criteria:
        fail("invalid_selector","index only chooses among matches; add label, name, value, type or a predicate.")
    if "predicate" in criteria:
        if len(criteria)!=1:
            fail("invalid_selector", "predicate cannot be mixed with exact fields.")
        result = criteria["predicate"]
        if not isinstance(result,str) or not 1 <= len(result) <= 2000:
            fail("invalid_selector", "predicate must have 1..2000 characters.")
        return result
    clauses=[]
    for key,value in criteria.items():
        if key=="enabled":
            if not isinstance(value,bool) and value not in ("true","false"):
                fail("invalid_selector","enabled must be a boolean or the exact tree string true/false.")
            literal="true" if value is True or value=="true" else "false"
            clauses.append("enabled == "+literal)
            continue
        if not isinstance(value,str) or not 1 <= len(value) <= 1000:
            fail("invalid_selector", "Selector fields must be nonempty strings up to 1000 characters.")
        if "\x00" in value:
            fail("invalid_selector", "Exact selector strings cannot contain NUL; NSPredicate cannot match that character reliably.")
        if key=="type" and not value.startswith("XCUIElementType"):
            value="XCUIElementType"+value
        escaped=predicate_literal(value)
        clauses.append(f"label CONTAINS '{escaped}'" if key=="label_contains" else f"{key} == '{escaped}'")
    return " AND ".join(clauses)


def short_type(kind):
    return kind[len(TYPE_PREFIX):] if isinstance(kind,str) and kind.startswith(TYPE_PREFIX) else kind


def compact_node(node):
    """Model-facing node: short type, each distinct text once, integer [x,y,width,height].

    Omitted fields carry their defaults: name equals label, value repeats the text,
    enabled and visible are true, and the node intersects the viewport.
    """
    result={}
    if node.get("type"):result["type"]=short_type(node["type"])
    label,name,value=node.get("label"),node.get("name"),node.get("value")
    if label:result["label"]=label
    if name and name!=label:result["name"]=name
    if value and value!=(label or name):result["value"]=value
    if node.get("enabled") in ("false",False):result["enabled"]=False
    if node.get("visible") in ("false",False):result["visible"]=False
    if node.get("in_viewport") is False:result["in_viewport"]=False
    rect=node.get("rect")
    if rect:result["rect"]=[round(rect[key]) for key in ("x","y","width","height")]
    return result


def contains(outer,inner,slack=1):
    return (outer["x"]-slack<=inner["x"] and outer["y"]-slack<=inner["y"]
            and inner["x"]+inner["width"]<=outer["x"]+outer["width"]+slack
            and inner["y"]+inner["height"]<=outer["y"]+outer["height"]+slack)


class PhoneController:
    def __init__(self, client, state_dir):
        self.client, self.state_dir = client, Path(state_dir)
        self.snapshots=collections.OrderedDict()
        self.tool_records=collections.deque(maxlen=500)
        self.accepted_actions=0
        self.screen=None
        self._screen_viewport=None
        self._screen_targets={}
        self._viewport_cache=None
        self._source_app=None
        self._deadline=None
        self.pending_input=None
        self.last_target=None
        self.typing_frequency=TYPING_FREQUENCY
        self.typing_piece=TYPING_PIECE
        self.call_budget=CALL_BUDGET
        # Seconds WDA may wait for animations before a post-action tree read; 0 reads at once.
        self.settle_seconds=0

    def external_action(self):
        """The user acted on the phone from the preview toolbar: earlier focus and geometry may be gone."""
        self.accepted_actions+=1
        self._viewport_cache=None
        self.pending_input=None

    def reset(self):
        """Forget context tied to a PUA session that was closed or replaced."""
        self.snapshots.clear()
        self._viewport_cache=None
        self.pending_input=None

    def screen_event(self,kind,**kwargs):
        # Display telemetry must never change, retry or delay a phone action
        # with extra WDA requests. No selector, input text or app data is sent.
        if self.screen is not None:
            try:self.screen.gesture(kind,viewport=self._screen_viewport,**kwargs)
            except Exception:pass

    def post(self,path,payload,timeout=None,session=True):
        if path=="/wda/tap":self.screen_event("tap",point={"x":payload["x"],"y":payload["y"]})
        elif path.endswith("/click") and path[:-6] in self._screen_targets:
            self.screen_event("tap",point=self._screen_targets[path[:-6]])
        elif path=="/wda/dragfromtoforduration":
            self.screen_event("drag",**{"from_point":{"x":payload["fromX"],"y":payload["fromY"]},"to_point":{"x":payload["toX"],"y":payload["toY"]},"duration_ms":max(300,min(3000,int((payload.get("duration",0)+.35)*1000)))})
        result=self.client.session("POST",path,payload,timeout=timeout) if session else self.client.request("POST",path,payload,timeout=timeout)
        self.accepted_actions+=1
        return result

    def viewport(self,max_age=0):
        cached=self._viewport_cache
        if max_age and cached and time.monotonic()-cached[0]<=max_age:
            return dict(cached[1])
        v=self.client.session("GET", "/window/size")
        if not isinstance(v,dict):
            fail("invalid_response", "Missing viewport.")
        finite(v.get("width"),"width",1,10000);finite(v.get("height"),"height",1,10000)
        if self.screen is not None and v!=self._screen_viewport:
            self._screen_viewport={"width":v["width"],"height":v["height"]}
            try:self.screen.set_viewport(self._screen_viewport)
            except Exception:pass
        result={"width":v["width"],"height":v["height"],"units":"iPhone points"}
        self._viewport_cache=(time.monotonic(),result)
        return dict(result)

    def source_viewport(self,root):
        """Reuse the cached window size while the source root still has exactly that size."""
        cached=self._viewport_cache
        try:size=(float(root["width"]),float(root["height"]))
        except (KeyError,ValueError):return self.viewport()
        if not cached or (cached[1]["width"],cached[1]["height"])!=size:
            # First read, or the root changed shape (rotation): ask WDA for the real window size.
            return self.viewport()
        self._viewport_cache=(time.monotonic(),cached[1])
        return dict(cached[1])

    def active_app(self,timeout=None):
        result=self.client.request("GET", "/wda/activeAppInfo",timeout=timeout).get("value") or {}
        app=result.get("bundleId")
        if not isinstance(app,str) or not app or app.startswith("local.pid."):
            fail("pua_foreground_unavailable","PUA cannot resolve a running foreground application. This is a PUA/XCTest channel problem, not a selector or schema error.",foreground_app=app,recovery={"tool":"pua_ready","arguments":{"screenshot":False},"replay_action":False})
        return app

    def tree(self, include_invisible=False, expensive_visibility=False):
        # wdAccessible may query native accessibility for each node and its parents.
        # It is unused by observations/collection; exclude the attribute, not nodes.
        excluded=["accessible"]
        if not expensive_visibility:
            excluded.insert(0,"visible")
        path="/source?format=xml&excluded_attributes="+",".join(excluded)
        raw=self.client.session("GET",path)
        if not isinstance(raw,str):
            fail("invalid_response", "PUA source is not XML text.")
        try:
            tree=ET.fromstring(raw)
        except ET.ParseError as exc:
            fail("invalid_response", f"PUA XML parse failed: {exc}.")
        root=tree.attrib if tree.attrib.get("type")=="XCUIElementTypeApplication" else {}
        # The page source root names the foreground app and spans the screen, so one read
        # usually answers what activeAppInfo and window/size would be asked separately.
        bundle=root.get("bundleId")
        self._source_app=bundle if isinstance(bundle,str) and bundle and not bundle.startswith("local.pid.") else None
        viewport=self.source_viewport(root)
        nodes=[]
        for element in tree.iter():
            a=element.attrib
            if not any(a.get(k) for k in ("label","name","value")) and a.get("type") not in ("XCUIElementTypeAlert","XCUIElementTypeSheet"):
                continue
            try:
                rect={k:float(a.get(k,0)) for k in ("x","y","width","height")}
            except ValueError:
                continue
            if not all(math.isfinite(n) for n in rect.values()):
                continue
            intersects=rect["width"]>0 and rect["height"]>0 and rect["x"]<viewport["width"] and rect["y"]<viewport["height"] and rect["x"]+rect["width"]>0 and rect["y"]+rect["height"]>0
            if not include_invisible and (not intersects or a.get("visible")=="false"):
                continue
            if a.get("type") in ("XCUIElementTypeApplication","XCUIElementTypeWindow"):
                continue
            node={k:a[k] for k in ("type","name","label","value","enabled","visible") if k in a}
            node["rect"]=rect;node["in_viewport"]=intersects
            nodes.append(node)
        return nodes,viewport

    def region_nodes(self,nodes,region=None):
        if region:
            x,y,w,h=region["x"],region["y"],region["width"],region["height"]
            nodes=[n for n in nodes if x<=n["rect"]["x"]+n["rect"]["width"]/2<=x+w and y<=n["rect"]["y"]+n["rect"]["height"]/2<=y+h]
        # Ignore status bar clocks/battery values. Compare visible content and geometry.
        nodes=[n for n in nodes if n["type"]!="XCUIElementTypeStatusBar" and n["rect"]["y"]>=45]
        return nodes

    def remember(self,nodes,viewport,app,has_tree=True):
        ident=uuid.uuid4().hex[:12]
        self.snapshots[ident]={"time":time.monotonic(),"viewport":viewport,"app":app,"nodes":nodes if has_tree else None}
        while len(self.snapshots)>32:
            self.snapshots.popitem(last=False)
        return ident

    def observe(self,mode="tree",include_invisible=False,max_nodes=100,expensive_visibility=False):
        if mode not in ("tree","screenshot","both"):
            fail("invalid_argument","mode must be tree, screenshot or both.")
        integer(max_nodes,"max_nodes",1,500)
        if mode=="screenshot":
            app=self.active_app()
            nodes,viewport=[],self.viewport(VIEWPORT_TTL)
        else:
            nodes,viewport=self.tree(include_invisible,expensive_visibility)
            app=self._source_app or self.active_app()
        return self.observation_from_state(nodes,viewport,app,mode,max_nodes,expensive_visibility)

    def observation_from_state(self,nodes,viewport,app,mode,max_nodes=100,expensive_visibility=False):
        result={"observation_id":self.remember(nodes,viewport,app,has_tree=mode!="screenshot"),
                "observed_at":dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),"app":app,"viewport":viewport}
        if mode in ("tree","both"):
            result.update({"nodes":[compact_node(node) for node in nodes[:max_nodes]],"total_nodes":len(nodes),"truncated":len(nodes)>max_nodes})
            if expensive_visibility:result["visibility_computed"]=True
            if not nodes:
                result["warnings"]=["Empty accessibility tree: inspect a screenshot for lock, iPhone Mirroring conflict, loading, or custom-rendered content before acting."]
        if mode in ("screenshot","both"):
            result["image"]=self.capture(viewport)
        return result

    def capture(self,viewport):
        """Native PNG kept as evidence; the model receives a bounded image with its exact point scale."""
        encoded=self.client.request("GET","/screenshot").get("value")
        try:
            data=base64.b64decode(encoded,validate=True)
        except (ValueError,TypeError) as exc:
            fail("invalid_response", "Invalid screenshot encoding.")
        if not data.startswith(wda_image.PNG_SIGNATURE):
            fail("invalid_response","PUA screenshot is not PNG.")
        artifacts=self.state_dir/"artifacts";artifacts.mkdir(mode=0o700,parents=True,exist_ok=True)
        dest=artifacts/(dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+"-"+uuid.uuid4().hex[:8]+".png")
        dest.write_bytes(data);dest.chmod(0o600)
        native=wda_image.png_size(data[:32])
        if native and (native[0]>native[1])!=(viewport["width"]>viewport["height"]):
            # The capture is turned the other way than the remembered viewport: the phone rotated.
            viewport.update(self.viewport())
        path,mime,width,height=wda_image.model_image(dest)
        for pattern in ("*.png","*.jpg"):
            for old in sorted(artifacts.glob(pattern))[:-100]:
                old.unlink()
        image={"path":str(path),"mimeType":mime}
        if width and height:
            image.update(width=width,height=height,pixel_to_point=[round(viewport["width"]/width,4),round(viewport["height"]/height,4)])
        return image

    def snapshot(self,observation_id):
        old=self.snapshots.get(observation_id)
        if not old:
            stale("This observation is unknown to the current Runtime. Observe again or use current viewport coordinates.","unknown_observation")
        if self.active_app()!=old["app"]:
            stale("The foreground app changed. Observe again.","foreground_changed")
        return old

    def guard(self,observation_id=None):
        old=self.snapshot(observation_id) if observation_id is not None else None
        # A supplied ID asks for a context check, so read the viewport again for it.
        viewport=self.viewport(0 if old else VIEWPORT_TTL)
        if old and viewport!=old["viewport"]:
            stale("Orientation or viewport changed. Observe again before using the earlier coordinates.","viewport_changed")
        return viewport

    def guard_region(self,observation_id,region):
        viewport=self.guard(observation_id)
        self.region(region,viewport)
        return viewport

    def query(self,selector,limit=CANDIDATE_LIMIT):
        """Matches in tree order, with whatever attributes PUA returned inline."""
        found=self.client.session("POST","/elements",{"using":"predicate string","value":predicate(selector)}) or []
        if not isinstance(found,list):
            fail("invalid_response","PUA elements result is not a list.")
        index=selector.get("index")
        inline=all(isinstance(item,dict) and isinstance(item.get("rect"),dict) for item in found)
        if index is not None:
            chosen=[(index,found[index])] if index<len(found) else []
        else:
            # Inline rects cost nothing more, so every match can be weighed; bare IDs are read up to the limit.
            chosen=list(enumerate(found if inline else found[:limit]))
        elements=[]
        for position,item in chosen:
            ident=(item.get(ELEMENT_KEY) or item.get("ELEMENT")) if isinstance(item,dict) else None
            if not isinstance(ident,str):
                continue
            element={"element_id":ident,"index":position}
            for source,key in (("rect","rect"),("type","type"),("label","label"),("attribute/name","name"),("attribute/value","value"),("enabled","enabled")):
                if item.get(source) is not None:element[key]=item[source]
            elements.append(element)
        return {"elements":elements,"matches":len(found),"complete":index is not None or len(elements)==len(found)}

    def element_rect(self,element):
        if "rect" not in element:
            element["rect"]=self.client.session("GET","/element/"+quote(element["element_id"],safe="")+"/rect")
        rect=element["rect"]
        if not isinstance(rect,dict) or not all(isinstance(rect.get(key),(int,float)) and not isinstance(rect.get(key),bool) and math.isfinite(rect[key]) for key in ("x","y","width","height")):
            fail("invalid_response","PUA element rect is not numeric.")
        return rect

    def element_hittable(self,element):
        if "hittable" not in element:
            element["hittable"]=self.client.session("GET","/element/"+quote(element["element_id"],safe="")+"/attribute/hittable") in (True,1,"true","1")
        return element["hittable"]

    def describe(self,element):
        rect=self.element_rect(element)
        described={"index":element["index"],**compact_node({**element,"rect":rect}),"tap":[round(rect["x"]+rect["width"]/2),round(rect["y"]+rect["height"]/2)]}
        if "hittable" in element:described["hittable"]=element["hittable"]
        return described

    def switch_to_coordinates(self,error,typing=False):
        """Hand abnormal UI state back with a screenshot before the model chooses another action."""
        if error.code not in VISUAL_FAILURES or error.details.get("secure_field"):
            return
        # Nested action/wait wrappers must not capture twice, even when capture failed.
        if error.details.get("recovery",{}).get("visual_check_required"):
            return
        error.details.setdefault("action_executed",False)
        try:
            observed=error.details.get("observation")
            if observed is None:
                app=error.details.get("foreground_app")
                error.details["observation"]=(self.observation_from_state([],self.viewport(VIEWPORT_TTL),app,"screenshot")
                                              if app else self.observe("screenshot"))
            elif "image" not in observed:observed["image"]=self.capture(observed["viewport"])
        except WDAError as failure:error.details["observation_error"]={"code":failure.code,"message":str(failure)}
        if error.code=="offscreen_target":
            tool,step="pua_swipe","Inspect the attached screenshot FIRST. The match lies outside the viewport; check the actual list, direction and overlays before deciding whether to swipe toward it. Then use the resulting screenshot to locate it."
        elif error.code=="occluded_target":
            tool,step="pua_tap","Inspect the attached screenshot FIRST. A popup may cover the target; dismiss its visible close/cancel control before tapping the field or target. tap_point is the background element's location, NOT proof it can be clicked."
        elif error.code in ("search_exhausted","no_scroll_progress","scroll_context_changed","modal_requires_region","blocked_scroll_region"):
            tool,step=None,"Inspect the attached screenshot FIRST for the last row/end marker, unchanged content, wrong region, fixed header or popup. Decide whether another swipe is warranted and choose the visible target/region/direction; do not automatically repeat scrolling or increase its budget. No progress alone does not prove an empty or complete list."
        elif error.code in ("postcondition_failed","input_mismatch"):
            tool,step=None,"Inspect the attached screenshot FIRST to establish the actual page, field text or submission result. Continue only the missing work; do not blindly repeat the previous click, input or submission."
        elif typing:
            tool,step="pua_tap","Inspect the attached screenshot FIRST for a popup or wrong page. Tap the visible editable field with pua_tap x/y, then call pua_type_text with text and no selector. If a prior tap failed to focus the field, do not repeat the same point blindly; choose a new target from the current screenshot."
        else:
            tool,step="pua_tap","Inspect the attached screenshot FIRST, then tap the visible target with pua_tap x/y; a returned tap or tap_point is already in iPhone points."
        recovery={**error.details.get("recovery",{}),"use":"coordinates" if error.code in SELECTOR_FAILURES else "screenshot","visual_check_required":True,"replay_action":False,
                  "next_step":step+" Image pixels x image.pixel_to_point give iPhone points. Inspect the image content, not its base64 text; through functions.exec forward image blocks with image(block), or open observation.image.path with view_image. If no screenshot is available, take one pua_observe(mode=screenshot) before acting. Do not try other selector spellings or read the tree again first."}
        recovery.pop("next_tool",None);recovery.pop("next_arguments",None)
        if tool:recovery["next_tool"]=tool
        if "image" not in error.details.get("observation",{}):
            recovery.update(next_tool="pua_observe",next_arguments={"mode":"screenshot"})
        if recovery.get("next_tool")=="pua_tap" and not typing and error.code!="occluded_target" and "tap_point" in error.details:recovery["next_arguments"]=dict(error.details["tap_point"])
        error.details["recovery"]=recovery

    def find(self,selector,limit=10):
        integer(limit,"limit",1,30)
        found=self.query(selector,limit)
        return {"matches":found["matches"],"elements":[self.describe(element) for element in found["elements"][:limit]],
                "truncated":found["matches"]>min(limit,len(found["elements"])) and selector.get("index") is None}

    def choose(self,found,viewport,editable):
        """Reduce several matches to the one a tap can only mean, or report the candidates."""
        def on_screen(element):
            rect=self.element_rect(element)
            return rect["width"]>0 and rect["height"]>0 and 0<=rect["x"]+rect["width"]/2<viewport["width"] and 0<=rect["y"]+rect["height"]/2<viewport["height"]
        matches,complete=found["matches"],found["complete"]
        recovery={"replay_action":False,"next_step":"Tap the intended candidate's tap point with x/y, or repeat the call with selector.index. Nothing was sent to the phone."}
        candidates=[element for element in found["elements"] if on_screen(element)]
        if not candidates and complete:
            fail("offscreen_target","Several elements match, but none is inside the viewport. Scroll the intended one into view and observe again.",action_executed=False,matches=matches,
                 candidates=[self.describe(element) for element in found["elements"][:CANDIDATE_LIMIT]],viewport=viewport,recovery={"next_tool":"pua_observe","next_arguments":{"mode":"both"},"replay_action":False})
        if editable:
            fields=[element for element in candidates if element.get("type") in EDITABLE]
            if fields:candidates=fields
        if complete and len(candidates)==1:
            # The observation only listed on-screen nodes, so the remaining match is the one that was seen.
            self.last_target={"matches":matches,"chosen":"only_match_on_screen","index":candidates[0]["index"]}
            return candidates[0]
        listed=candidates[:CANDIDATE_LIMIT]
        ordered=sorted(listed,key=lambda element:element["rect"]["width"]*element["rect"]["height"])
        nested=complete and len(listed)==len(candidates)>1 and all(contains(ordered[i+1]["rect"],ordered[i]["rect"]) for i in range(len(ordered)-1))
        if nested:
            # Nested matches share one touch point: the innermost centre lies inside every one of them.
            for element in ordered:
                if self.element_hittable(element):
                    self.last_target={"matches":matches,"chosen":"innermost_of_nested_matches","index":element["index"]}
                    return element
            fail("occluded_target","The matching elements are nested at one place and none is reported hittable; no tap was sent. If the screen shows the target uncovered, tap its tap point with x/y; if something covers it, deal with that first.",action_executed=False,matches=matches,
                 candidates=[self.describe(element) for element in ordered],viewport=viewport,recovery={"use":"coordinates","next_tool":"pua_tap","replay_action":False})
        for element in listed:self.element_hittable(element)
        fail("ambiguous_target","Several separate elements match. Tap the intended candidate's tap point with x/y, or choose it by selector.index.",action_executed=False,matches=matches,
             candidates=[self.describe(element) for element in listed],candidates_truncated=not complete or len(listed)<len(candidates),recovery=recovery)

    def resolve(self,selector,editable=False,found=None):
        """One reachable element for the selector, with its PUA path and tap point."""
        result=found if found is not None else self.query(selector)
        self.last_target=None
        if result["matches"]==0:
            fail("no_such_element","No element matches this selector. Locate the target on the screen and act by coordinates instead of trying other selector spellings.",action_executed=False)
        if not result["elements"]:
            fail("no_such_element","selector.index is beyond the current matches. Read fresh state and choose again.",action_executed=False,matches=result["matches"])
        viewport=self.viewport(VIEWPORT_TTL)
        element=result["elements"][0] if result["matches"]==1 or selector.get("index") is not None else self.choose(result,viewport,editable)
        rect=self.element_rect(element)
        cx=rect["x"]+rect["width"]/2;cy=rect["y"]+rect["height"]/2
        if rect["width"]<=0 or rect["height"]<=0 or not (0<=cx<viewport["width"] and 0<=cy<viewport["height"]):
            fail("offscreen_target","Target center is outside the viewport. Scroll the actual list into view, observe again and re-find the target; a stopped batch has not completed its failed step.",action_executed=False,target_rect=rect,viewport=viewport,recovery={"next_tool":"pua_observe","next_arguments":{"mode":"both"},"replay_action":False})
        path="/element/"+quote(element["element_id"],safe="")
        if not self.element_hittable(element):
            fail("occluded_target","Target was found but is not reported hittable; no tap was sent, so nothing on the phone changed. If the screen shows the target uncovered, tap tap_point with x/y; if an overlay, picker or fixed header covers it, deal with that first.",action_executed=False,target_rect=rect,tap_point={"x":round(cx),"y":round(cy)},viewport=viewport,recovery={"use":"coordinates","next_tool":"pua_tap","next_arguments":{"x":round(cx),"y":round(cy)},"replay_action":False})
        if editable:
            if "type" not in element:element["type"]=self.client.session("GET",path+"/attribute/type")
            if element["type"] not in EDITABLE:
                fail("not_editable","Select a readable text/search field or text view. Ask the user to handle secure fields.",secure_field=element["type"]=="XCUIElementTypeSecureTextField")
        if self.screen is not None:
            if len(self._screen_targets)>=32:self._screen_targets.clear()
            self._screen_targets[path]={"x":cx,"y":cy}
        element["path"]=path
        return element

    def target(self,selector,editable=False,found=None,with_kind=False):
        element=self.resolve(selector,editable,found)
        return (element["path"],element["type"]) if editable and with_kind else element["path"]

    @action_result
    def wait(self,selector,timeout_seconds=6):
        finite(timeout_seconds,"timeout_seconds",0,20)
        pred=predicate(selector)
        deadline=time.monotonic()+timeout_seconds;attempts=0
        while True:
            attempts+=1
            remaining=max(0.05,deadline-time.monotonic()) if timeout_seconds else 0.5
            found=self.client.session("POST","/elements",{"using":"predicate string","value":pred},timeout=min(remaining,2)) or []
            if len(found)>selector.get("index",0):
                return {"verified":True,"matches":len(found),"polls":attempts,"criterion":"selector exists; presence alone does not prove business success"}
            if time.monotonic()>=deadline:
                fail("postcondition_failed","Expected target did not appear within the wait budget.",polls=attempts)
            time.sleep(min(0.25,max(0,deadline-time.monotonic())))

    def cool_off(self,seconds):
        try:
            self.client.session("POST","/appium/settings",{"settings":{"animationCoolOffTimeout":seconds}})
            return True
        except WDAError:
            # The read continues without the wait; the next command sends the fast settings again.
            self.client.reapply_settings()
            return False

    def observe_after(self,mode,max_nodes=100):
        """Post-action observation. A tree read may let PUA wait for the transition to end first."""
        if self.settle_seconds>0 and mode=="screenshot":
            # A screenshot has no such wait in WDA; give the transition a moment instead.
            time.sleep(min(self.settle_seconds,0.5))
        if self.settle_seconds<=0 or mode not in ("tree","both") or not self.cool_off(self.settle_seconds):
            return self.observe(mode,max_nodes=max_nodes)
        try:return self.observe(mode,max_nodes=max_nodes)
        finally:self.cool_off(0)

    def after(self,expect=None,observe="none",max_nodes=100):
        result={"action_executed":True,"action_complete":True,"verified":False,"verification_deferred":True}
        if expect:
            result["postcondition"]=self.wait(expect);result.update(verified=True,verification_deferred=False)
        if observe!="none":
            result["observation"]=self.observe_after(observe,max_nodes=max_nodes)
        return result

    @action_result
    def tap(self,selector=None,x=None,y=None,observation_id=None,expect=None,observe="none"):
        if observe not in ("none","tree","screenshot","both"):
            fail("invalid_argument","Invalid observation mode.")
        if expect:predicate(expect)
        if selector is not None:
            if x is not None or y is not None:
                fail("invalid_argument","Choose selector or coordinates.")
            try:path=self.target(selector)
            except WDAError as error:
                self.switch_to_coordinates(error);raise
            chosen=self.last_target
            self.post(path+"/click",{})
        else:
            finite(x,"x");finite(y,"y")
            viewport=self.guard(observation_id)
            if x>=viewport["width"] or y>=viewport["height"]:
                fail("invalid_argument","Coordinates are outside the iPhone viewport.")
            chosen=None
            self.post("/wda/tap",{"x":x,"y":y})
        result=self.after(expect,observe)
        if chosen:result["target"]=chosen
        return result

    @action_result
    def launch_app(self,bundle_id,expect=None,observe="none",verify=False):
        if not isinstance(bundle_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+",bundle_id):
            fail("invalid_argument","Use the app's verified bundle ID.")
        if observe not in ("none","tree","screenshot","both"):fail("invalid_argument","Invalid observe mode.")
        if not isinstance(verify,bool):fail("invalid_argument","verify must be boolean.")
        if expect:predicate(expect)
        self._viewport_cache=None
        self.post("/wda/apps/activate",{"bundleId":bundle_id},timeout=45)
        if not verify:
            result=self.after(expect,observe);result["foreground_verified"]=False
            return result
        deadline=time.monotonic()+5
        app=None
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                fail("postcondition_failed","Activation was accepted, but the requested app did not become foreground within five seconds. Inspect loading, login or system prompts before continuing; do not replay activation automatically.",requested_app=bundle_id,foreground_app=app,foreground_verified=False,recovery={"next_tool":"pua_observe","next_arguments":{"mode":"both"},"replay_action":False})
            app=self.active_app(timeout=remaining)
            if app==bundle_id:break
            time.sleep(min(.1,max(0,deadline-time.monotonic())))
        result=self.after(expect,observe)
        result.update(verified=True,verification_deferred=False,foreground_verified=True,verification_scope="Requested app is foreground; user task completion is separate.")
        return result

    @action_result
    def press_button(self,name,observe="none",verify=False,expect=None):
        if name not in ("home","volumeup","volumedown"):
            fail("invalid_argument","Supported buttons: home, volumeup, volumedown.")
        if observe not in ("none","tree","screenshot","both"):fail("invalid_argument","Invalid observe mode.")
        if not isinstance(verify,bool):fail("invalid_argument","verify must be boolean.")
        if expect:predicate(expect)
        if name!="home":
            self.post("/wda/pressButton",{"name":name})
            return self.after(expect,observe)
        # XCTest pressButton can acknowledge Home without changing foreground.
        # WDA's dedicated endpoint activates the system application instead.
        self._viewport_cache=None
        self.post("/wda/homescreen",{},session=False)
        if not verify:
            result=self.after(expect,observe);result["foreground_verified"]=False
            return result
        deadline=time.monotonic()+2
        app=None
        try:
            while True:
                app=self.active_app(timeout=max(.05,deadline-time.monotonic()))
                if app=="com.apple.springboard":break
                if time.monotonic()>=deadline:
                    fail("postcondition_failed","Home request returned, but SpringBoard did not become foreground. Inspect fresh state; do not loop on the same button.",foreground_app=app)
                time.sleep(min(.1,max(0,deadline-time.monotonic())))
            result=self.after(expect,observe)
            result.update(verified=True,verification_deferred=False,foreground_verified=True,foreground_app=app,verification_scope="Home navigation: SpringBoard is foreground; user task completion is separate.")
            return result
        except WDAError as exc:
            exc.details.update({"action_executed":True,"home_foreground_verified":app=="com.apple.springboard","verification_required":"Home was requested. Inspect fresh state before another action; do not automatically replay."})
            raise

    @action_result
    def type_text(self,selector=None,text=None,allow_newlines=False,submit=False,replace=True,observe="none",verify=False,expect=None,continue_token=None):
        if continue_token is not None:
            if selector is not None or text is not None or expect is not None or allow_newlines or submit or not replace or verify or observe!="none":
                fail("invalid_argument","continue_token resumes the earlier call with the options given there; pass it alone.",action_executed=False)
            return self.continue_input(continue_token)
        if text is None:
            fail("invalid_argument","type_text needs text, or continue_token alone.",action_executed=False)
        if not isinstance(text,str) or not 1<=len(text)<=10000 or "\x00" in text:
            fail("invalid_argument","text must have 1..10000 characters without NUL.")
        if ("\n" in text or "\r" in text) and not allow_newlines:
            fail("newline_requires_intent","This text contains line breaks, which can send a message. Use allow_newlines only for an observed multiline editor.")
        if observe not in ("none","tree","screenshot","both"):fail("invalid_argument","Invalid observe mode.")
        if not isinstance(verify,bool):fail("invalid_argument","verify must be boolean.")
        if expect:predicate(expect)
        try:path,kind=self.target(selector,editable=True,with_kind=True) if selector is not None else self.focused_field()
        except WDAError as error:
            self.switch_to_coordinates(error,typing=True);raise
        if ("\n" in text or "\r" in text) and kind!="XCUIElementTypeTextView":
            fail("newline_unsafe","Line breaks are allowed only for a verified TextView.")
        before=(self.client.session("GET",path+"/attribute/value") or "") if verify and not replace else ""
        self.pending_input=None
        # Without a selector the field was focused by a coordinate tap; another tap could move the caret.
        if selector is not None:self.post(path+"/click",{})
        if replace:
            self.post(path+"/clear",{})
        return self.type_pieces({"path":path,"focused":selector is None,"pieces":split_text(text,self.typing_piece),"typed":0,"total":len(text),
                                 "expected":text if replace else str(before)+text,"verify":verify,"submit":submit,"expect":expect,"observe":observe})

    def focused_field(self):
        """The editable element that has keyboard focus, for typing after a coordinate tap."""
        missing="No text field has keyboard focus; no text was entered. Inspect the screenshot for a blocking popup or wrong target, then tap the visible field with pua_tap x/y. Do not repeat a failed point blindly."
        try:found=self.client.session("GET","/element/active")
        except WDAError as error:
            if error.code!="no such element":raise
            fail("no_focused_field",missing,action_executed=False)
        ident=(found.get(ELEMENT_KEY) or found.get("ELEMENT")) if isinstance(found,dict) else None
        if not isinstance(ident,str):
            fail("no_focused_field",missing,action_executed=False)
        path="/element/"+quote(ident,safe="")
        kind=found.get("type") or self.client.session("GET",path+"/attribute/type")
        if kind not in EDITABLE:
            fail("not_editable","The focused element is not a readable text field. Ask the user to handle secure fields.",secure_field=kind=="XCUIElementTypeSecureTextField")
        return path,kind

    def type_pieces(self,plan):
        """Type bounded requests in order; stop at the call budget and keep the rest for a continuation."""
        deadline=time.monotonic()+self.call_budget
        if self._deadline is not None:deadline=min(deadline,self._deadline)
        sent=0
        while plan["pieces"]:
            if sent and time.monotonic()>=deadline:break
            piece=plan["pieces"][0]
            timeout=request_timeout(len(piece),self.typing_frequency,getattr(self.client,"timeout",15))
            try:
                # The element request focuses the field once; later pieces go to that focus,
                # because another element request may tap again and move the caret.
                if plan["focused"]:self.post("/wda/keys",{"value":[piece],"frequency":self.typing_frequency},timeout=timeout)
                else:self.post(plan["path"]+"/value",{"text":piece,"frequency":self.typing_frequency},timeout=timeout)
            except WDAError as error:
                error.details.update(characters_confirmed=plan["typed"],characters_total=plan["total"])
                self.pending_input=None
                raise
            plan["focused"]=True;plan["pieces"].pop(0);plan["typed"]+=len(piece);sent+=1
        if plan["pieces"]:
            if "app" not in plan:
                try:plan.update(app=self.active_app(),session_id=getattr(self.client,"session_id",None))
                except WDAError as error:
                    self.pending_input=None
                    error.details.update(characters_confirmed=plan["typed"],characters_total=plan["total"],
                                         recovery={"replay_action":False,"next_step":"Locate the original intended field, read its actual text, and enter only the missing remainder with replace=false."})
                    raise
            plan.update(token=uuid.uuid4().hex[:12],mark=self.accepted_actions,created=time.monotonic())
            self.pending_input=plan
            return {"action_executed":True,"action_complete":False,"input_complete":False,"verified":False,"verification_deferred":True,
                    "characters":plan["typed"],"remaining_characters":plan["total"]-plan["typed"],"submitted":False,"continue_token":plan["token"],
                    "next_step":"Call pua_type_text with only continue_token to type the rest. Do not resend the text or act on the phone in between."}
        self.pending_input=None
        if plan["verify"]:
            actual=self.client.session("GET",plan["path"]+"/attribute/value")
            if actual!=plan["expected"]:
                fail("input_mismatch","Typed text did not round-trip exactly. Do not submit or blindly type it again.",expected_length=len(plan["expected"]),actual_length=len(str(actual or "")))
        result={"action_executed":True,"action_complete":True,"verified":plan["verify"],"verification_deferred":not plan["verify"],"exact_readback":plan["verify"],"characters":plan["total"],"submitted":False}
        if plan["submit"]:
            self.post("/wda/keys",{"value":["\n"]})
            result.update({"submitted":True,"verified":False,"verification_deferred":True,"submission_verified":False,"verification_required":"Check the final submission result before claiming task completion; do not automatically repeat submission."})
        if plan["expect"]:
            result["postcondition"]=self.wait(plan["expect"])
            result.update(verified=True,verification_deferred=False)
            if plan["submit"]:result.update(submission_verified=True,verification_scope="Expected selector is present; verify the final business outcome before claiming task completion.")
        if plan["observe"]!="none":result["observation"]=self.observe_after(plan["observe"])
        return result

    def continue_input(self,token):
        plan=self.pending_input
        # A rejected or unreadable continuation must never remain reusable.
        self.pending_input=None
        def expired(reason,cause=None):
            details={"action_executed":False,"reason":reason,
                     "recovery":{"replay_action":False,"next_step":"Locate the original intended field and read its actual text, then type only the missing remainder with replace=false. Never resume into another app or field."}}
            if cause:details["cause"]={"code":cause.code}
            fail("input_continuation_expired","This continuation no longer has its original app, session and focused field, or it expired. Nothing was typed now.",**details)
        if not plan or plan["token"]!=token or self.accepted_actions!=plan["mark"] or time.monotonic()-plan["created"]>INPUT_TTL:
            expired("token_or_action_changed")
        if getattr(self.client,"session_id",None)!=plan["session_id"]:
            expired("session_changed")
        # The operation lock serializes tools, but another Runtime (or the user)
        # can change focus between calls without changing this instance's counter.
        try:
            app=self.active_app()
            path,_=self.focused_field()
        except WDAError as error:
            expired("context_unavailable",error)
        if getattr(self.client,"session_id",None)!=plan["session_id"]:
            expired("session_changed")
        if app!=plan["app"]:expired("app_changed")
        if path!=plan["path"]:expired("focus_changed")
        return self.type_pieces(plan)

    def region(self,region,viewport):
        if region is None:
            return {"x":viewport["width"]*.15,"y":viewport["height"]*.25,"width":viewport["width"]*.7,"height":viewport["height"]*.5}
        if not isinstance(region,dict) or set(region)!={"x","y","width","height"}:
            fail("invalid_argument","region must have x/y/width/height in iPhone points.")
        for k,v in region.items():finite(v,k,0 if k in ("x","y") else 1)
        if region["x"]+region["width"]>viewport["width"] or region["y"]+region["height"]>viewport["height"]:
            fail("invalid_argument","Gesture region is outside viewport.")
        return region

    def native_modals(self,nodes):
        return [{k:n[k] for k in ("type","name","label","rect") if k in n} for n in nodes if n["type"] in ("XCUIElementTypeAlert","XCUIElementTypeSheet")]

    @staticmethod
    def modal_report(modals):
        return [compact_node(modal) for modal in modals]

    def scroll_observation(self,nodes,viewport,mode,max_nodes=100):
        if mode=="none":return None
        # The caller already read the complete tree and viewport. Reuse both;
        # adding a screenshot does not require another viewport/XML request.
        return self.observation_from_state(nodes,viewport,self._source_app or self.active_app(),mode,max_nodes)

    def scroll_progress(self,before,after,region,direction):
        # Numeric/value refreshes and animation metadata are not movement.
        # Match unique stable row/text anchors and require a displacement along
        # the requested axis, with substantially unchanged row dimensions.
        def anchors(nodes):
            found=collections.defaultdict(list)
            for node in self.region_nodes(nodes,region):
                if node["type"] not in ("XCUIElementTypeCell","XCUIElementTypeStaticText","XCUIElementTypeOther","XCUIElementTypeImage"):
                    continue
                for field in ("name","label"):
                    value=node.get(field)
                    if isinstance(value,str) and value:
                        found[(node["type"],field,value)].append(node["rect"])
            return {key:rects[0] for key,rects in found.items() if len(rects)==1}
        old,new=anchors(before),anchors(after)
        axis,cross=("y","x") if direction in ("up","down") else ("x","y")
        sign=-1 if direction in ("up","left") else 1
        for key in old.keys()&new.keys():
            a,b=old[key],new[key]
            if (b[axis]-a[axis])*sign>2 and abs(b[cross]-a[cross])<=3 and all(abs(b[k]-a[k])<=2 for k in ("width","height")):
                return True
        return False

    @action_result
    def swipe(self,direction="up",region=None,observation_id=None,expect=None,verify=False,max_attempts=1,observe="none",_baseline=None,_max_nodes=100):
        if direction not in ("up","down","left","right"):fail("invalid_argument","Invalid direction.")
        if observe not in ("none","tree","screenshot","both"):fail("invalid_argument","Invalid observe mode.")
        integer(max_attempts,"max_attempts",1,2)
        if not isinstance(verify,bool):fail("invalid_argument","verify must be boolean.")
        if expect:predicate(expect)
        viewport=self.guard_region(observation_id,region) if observation_id is not None else None
        if verify:
            before,current_viewport=_baseline if _baseline is not None else self.tree()
            if viewport is not None and current_viewport!=viewport:
                stale("Viewport changed while preparing this scroll.","viewport_changed","region")
            viewport=current_viewport
        else:
            before=[]
            if viewport is None:viewport=_baseline[1] if _baseline is not None else self.viewport(VIEWPORT_TTL)
        area=self.region(region,viewport)
        modals=self.native_modals(before) if verify else []
        if modals:
            contained=lambda m:area["x"]>=m["rect"]["x"] and area["y"]>=m["rect"]["y"] and area["x"]+area["width"]<=m["rect"]["x"]+m["rect"]["width"] and area["y"]+area["height"]<=m["rect"]["y"]+m["rect"]["height"]
            if region is None or not all(contained(m) for m in modals):
                details={"action_executed":False,"verified":False,"region":area,"native_modals":self.modal_report(modals),"recovery":{"next_tool":"pua_observe","next_arguments":{"mode":"both"},"replay_action":False,"next_step":"Handle the existing modal first, or choose a fresh explicit scroll region wholly inside its intended list. Do not scroll the underlying page through a modal."}}
                observed=self.scroll_observation(before,viewport,"tree" if observe in ("tree","both") else "none",_max_nodes)
                if observed:details["observation"]=observed
                fail("modal_requires_region" if region is None else "blocked_scroll_region","Native modals are present. The intended scroll area must be explicit and inside every modal's bounds; otherwise handle the foreground modal first.",**details)
        x=area["x"]+area["width"]/2;y=area["y"]+area["height"]/2
        dx=area["width"]*.32;dy=area["height"]*.32
        points={"up":(x,y+dy,x,y-dy),"down":(x,y-dy,x,y+dy),"left":(x+dx,y,x-dx,y),"right":(x-dx,y,x+dx,y)}[direction]
        # max_attempts=2 remains accepted for older callers, but a failed first
        # gesture now yields a screenshot instead of an unseen fallback gesture.
        self.post("/wda/dragfromtoforduration",dict(zip(("fromX","fromY","toX","toY"),points),duration=.1))
        if not verify:
            result=self.after(expect,observe,max_nodes=_max_nodes)
            result.update(strategy="short_drag",attempts=1,progress_verified=False)
            return result
        after,v=self.tree()
        reasons=[]
        if v!=viewport:reasons.append("viewport_changed")
        if self.native_modals(after)!=modals:reasons.append("modal_changed")
        if reasons:
            details={"action_executed":True,"verified":False,"changed":False,"attempts":1,"reasons":reasons,"recovery":{"same_gesture_retry":False,"replay_action":False}}
            observed=self.scroll_observation(after,v,"tree" if observe in ("tree","both") else "none",_max_nodes)
            if observed:details["observation"]=observed
            fail("scroll_context_changed","A gesture was accepted, but the viewport or native modal context changed. Inspect the screenshot before choosing another action.",**details)
        changed=self.scroll_progress(before,after,area,direction)
        if changed:
            result={"action_executed":True,"action_complete":True,"verified":True,"verification_deferred":False,"changed":True,"progress_verified":True,"verification_scope":"Stable accessibility anchors moved in the requested direction; business coverage is separate.", "attempts":1,"strategy":"short_drag"}
            observed=self.scroll_observation(after,v,observe,_max_nodes)
            if observed:result["observation"]=observed
            if expect:result["postcondition"]=self.wait(expect)
            return result
        details={"action_executed":True,"verified":False,"changed":False,"attempts":1,"region":area,
                 "recovery":{"next_tool":"pua_observe","next_arguments":{"mode":"both"},"next_step":"Inspect the current page and list entrance. If this is an overview, tap the actual list entry; if at the end, reconcile counts. Otherwise inspect a screenshot, including custom pickers/overlays that may not appear as native modals, or another stable region.","same_gesture_retry":False,"end_of_list_proven":False}}
        observed=self.scroll_observation(after,v,"tree" if observe in ("tree","both") else "none",_max_nodes)
        if observed:details["observation"]=observed
        fail("no_scroll_progress","Gestures executed but stable accessibility anchors did not show movement in the requested direction. The page may be an overview, boundary, blocked region or custom-rendered list. Changing numbers alone are not scroll progress. This does not prove an empty or complete list; inspect returned state before choosing the next action.",**details)

    @action_result
    def scroll_find(self,selector,direction="up",max_swipes=1):
        predicate(selector);integer(max_swipes,"max_swipes",0,10)
        if direction not in ("up","down","left","right"):fail("invalid_argument","Invalid direction.")
        # At most one gesture per call: unresolved state goes to the model's
        # screenshot fallback before any further scrolling, even with a larger budget.
        for count in range(min(max_swipes,1)+1):
            found=self.query(selector)
            if found["matches"]:
                try:
                    element=self.resolve(selector,found=found)
                    return {"verified":True,"swipes":count,"matches":found["matches"],"target":self.describe(element)}
                except WDAError as exc:
                    exc.details.update(swipes=count,max_swipes=max_swipes)
                    if exc.code!="offscreen_target" or count or not max_swipes:raise
            if count==min(max_swipes,1):break
            self.swipe(direction,verify=False,observe="none")
        fail("search_exhausted","No hittable match after this search step. Inspect the screenshot before deciding whether to scroll again; do not automatically repeat the search or increase max_swipes.",
             swipes=count,max_swipes=max_swipes,stop_reason="visual_check_required",action_executed=count>0)

    @action_result
    def collect_list(self,row_type="Cell",max_pages=6,end_selector=None):
        integer(max_pages,"max_pages",1,10)
        if end_selector:predicate(end_selector)
        if not isinstance(row_type,str) or not re.fullmatch(r"(?:XCUIElementType)?[A-Za-z]+",row_type):fail("invalid_argument","Invalid row_type.")
        kind=row_type if row_type.startswith("XCUIElementType") else "XCUIElementType"+row_type
        rows={};pages=[];seen_pages=set();reason="page_budget";end=False
        observed=self.observe(max_nodes=500)
        for index in range(max_pages):
            nodes=self.snapshots[observed["observation_id"]]["nodes"]
            current=[n for n in nodes if n["type"]==kind]
            for n in current:
                key=json.dumps({k:n[k] for k in ("type","name","label","value") if k in n},ensure_ascii=False,sort_keys=True)
                rows.setdefault(key,n)
            pages.append({"page":index+1,"rows_seen":len(current),"truncated":observed["truncated"]})
            found=self.query(end_selector) if end_selector else None
            if found and found["matches"]:
                try:self.resolve(end_selector,found=found);end=True;reason="explicit_end_marker";break
                except WDAError as exc:
                    if exc.code not in ("offscreen_target","occluded_target","ambiguous_target"):raise
            # Collection already needs the next page, so consume it directly.
            # Virtualized lists may replace every label in fixed row slots;
            # requiring a shared moving anchor would discard that new page.
            # Ignore value refreshes and unrelated controls for repeat detection.
            page_key=json.dumps([{k:n[k] for k in ("type","name","label","rect") if k in n} for n in current],ensure_ascii=False,sort_keys=True)
            if page_key in seen_pages:
                reason="no_progress";break
            seen_pages.add(page_key)
            if index==max_pages-1:break
            observed=self.swipe("up",verify=False,observe="tree",_baseline=(nodes,observed["viewport"]),_max_nodes=500)["observation"]
        result={"rows":[compact_node(row) for row in rows.values()],"pages":pages,"stop_reason":reason,"end_marker_seen":end,"complete":False,
                "coverage_verified":end and not any(p["truncated"] for p in pages),
                "limitations":["Rows deduplicate by identical type/name/label/value; identical rows may collapse.","Only exposed accessibility labels are collected. Reconcile expected counts, screenshot-only fields, totals, currencies and dates before claiming business completeness."]}
        if reason=="no_progress":
            error=WDAError("no_scroll_progress","Repeated collection page; inspect the screenshot before continuing.",details={"observation":observed,"action_executed":index>0,"recovery":{"same_gesture_retry":False,"end_of_list_proven":False}})
            self.switch_to_coordinates(error)
            result.update({key:value for key,value in error.details.items() if key in ("observation","observation_error","recovery")})
        return result

    def batch(self,steps):
        if not isinstance(steps,list) or not 1<=len(steps)<=20:
            fail("invalid_argument","steps must contain 1..20 operations.")
        allowed={"tap":self.tap,"swipe":self.swipe,"type_text":self.type_text,"launch_app":self.launch_app,"press_button":self.press_button,"wait":self.wait,"observe":self.observe,"scroll_find":self.scroll_find}
        # Validate every step's shape before executing anything. Tool schemas validate nested arguments in the MCP layer.
        for step in steps:
            if not isinstance(step,dict) or set(step)-{"op","args"} or step.get("op") not in allowed or not isinstance(step.get("args",{}),dict):
                fail("invalid_argument","Each batch step needs a supported op and args object.")
        results=[]
        # One call stays inside the host's tool timeout: later steps are handed back, never dropped silently.
        self._deadline=time.monotonic()+self.call_budget
        try:
            for index,step in enumerate(steps):
                if index and time.monotonic()>=self._deadline:
                    return {"completed_steps":len(results),"stopped_at":index,"stop_reason":"time_budget","results":results,"complete":False,
                            "next_step":"Send the steps from stopped_at onward in a new call. Completed steps were executed and must not be repeated."}
                try:
                    result=allowed[step["op"]](**step.get("args",{}))
                    if result.get("input_complete") is False:
                        return {"completed_steps":len(results),"stopped_at":index,"stop_reason":"input_continues","results":results+[result],"complete":False,
                                "next_step":"Call pua_type_text with the returned continue_token until input_complete, then send the steps after stopped_at."}
                    results.append(result)
                    if result.get("submitted") and not result.get("submission_verified"):
                        return {"completed_steps":len(results),"stopped_at":index,"stop_reason":"submission_requires_verification","results":results,"complete":False}
                except WDAError as exc:
                    return {"completed_steps":len(results),"stopped_at":index,"stop_reason":exc.code,"error":exc.as_dict(),"results":results,"complete":False}
            return {"completed_steps":len(results),"results":results,"complete":True}
        finally:self._deadline=None
