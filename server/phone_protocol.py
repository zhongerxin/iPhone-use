"""Shared phone MCP contracts; importing this module never starts a device service."""
import base64
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

class PhoneError(Exception):
    def __init__(self, code, message, uncertain=False, details=None):
        super().__init__(message)
        self.code, self.uncertain, self.details = code, uncertain, details or {}

    def as_dict(self):
        return {"code": self.code, "message": str(self), "uncertain": self.uncertain, **self.details}



SCREEN_META={"ui":{"csp":{"connectDomains":[],"resourceDomains":[]},"prefersBorder":False},"openai/ui":{"availableDisplayModes":["fullscreen"],"preferredDisplayMode":"fullscreen"}}
PROTOCOLS=("2025-11-25","2025-06-18","2025-03-26","2024-11-05")
BOOL={"type":"boolean"}


def obj(properties,required=()):
    return {"type":"object","properties":properties,"required":list(required),"additionalProperties":False}



def string(description="",max_length=1000,enum=None):
    result={"type":"string","minLength":1,"maxLength":max_length}
    if description:result["description"]=description
    if enum:result["enum"]=enum
    return result



def num(low,high,kind="number"):
    return {"type":kind,"minimum":low,"maximum":high}



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
            except PhoneError:pass
        if matches!=1:raise PhoneError("invalid_argument",f"{path} must match exactly one allowed operation schema.")
        return
    kind=schema.get("type")
    valid={"object":lambda:isinstance(value,dict),"array":lambda:isinstance(value,list),"string":lambda:isinstance(value,str),"boolean":lambda:isinstance(value,bool),"number":lambda:not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value),"integer":lambda:not isinstance(value,bool) and isinstance(value,int)}
    if kind and not valid[kind]():raise PhoneError("invalid_argument",f"{path} must be {kind}.")
    if "const" in schema and value!=schema["const"]:raise PhoneError("invalid_argument",f"Invalid {path} operation.")
    if "enum" in schema and value not in schema["enum"]:raise PhoneError("invalid_argument",f"Invalid {path} option.")
    if kind=="object":
        props=schema.get("properties",{})
        if schema.get("additionalProperties") is False and set(value)-set(props):raise PhoneError("invalid_argument",f"Unknown fields in {path}: {', '.join(sorted(set(value)-set(props)))}. Use only the declared fields.",details={"action_executed":False,"argument_path":path,"unknown_fields":sorted(set(value)-set(props)),"allowed_fields":sorted(props),"recovery":{"next_step":"Correct these fields using the current tool schema, then call once; no device action was executed."}})
        if set(schema.get("required",[]))-set(value):raise PhoneError("invalid_argument",f"Missing required fields in {path}.")
        if len(value)<schema.get("minProperties",0):raise PhoneError("invalid_argument",f"{path} cannot be empty.")
        for k,v in value.items():
            if k in props:validate(v,props[k],path+"."+k)
    if kind in ("number","integer") and not schema.get("minimum",-math.inf)<=value<=schema.get("maximum",math.inf):raise PhoneError("invalid_argument",f"{path} is out of range.")
    if kind=="string" and not schema.get("minLength",0)<=len(value)<=schema.get("maxLength",100000):raise PhoneError("invalid_argument",f"{path} has invalid length.")
    if kind=="array":
        if not schema.get("minItems",0)<=len(value)<=schema.get("maxItems",100000):raise PhoneError("invalid_argument",f"{path} has invalid number of items.")
        for i,v in enumerate(value):validate(v,schema["items"],f"{path}[{i}]")



def copy_png(directory,data):
    """Put a PNG on the macOS clipboard; the file exists only for the duration of the copy."""
    fd,name=tempfile.mkstemp(prefix=".clipboard-",suffix=".png",dir=directory)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"wb") as stream:stream.write(data)
        # The path travels as an argument, never inside the script text.
        done=subprocess.run(["/usr/bin/osascript","-e","on run argv","-e","set the clipboard to (read (POSIX file (item 1 of argv)) as «class PNGf»)","-e","end run",name],
                            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8,check=False)
        if done.returncode!=0:raise PhoneError("clipboard_unavailable","The screenshot was captured but macOS did not accept it on the clipboard.")
    except (OSError,subprocess.SubprocessError) as exc:
        raise PhoneError("clipboard_unavailable","The screenshot could not be copied to the clipboard.") from exc
    finally:
        try:os.unlink(name)
        except OSError:pass



def result_content(data,structured=False):
    """One compact JSON text block, plus the screenshot when the result carries one.

    structuredContent is left out on purpose: a host that receives it may give the model
    only that object and drop every content block, including the image.
    """
    content=[{"type":"text","text":json.dumps(data,ensure_ascii=False,allow_nan=False,separators=(",",":"))}]
    image=data.get("image") or data.get("observation",{}).get("image")
    if not image and isinstance(data.get("error"),dict):image=data["error"].get("observation",{}).get("image")
    if not image and data.get("results"):
        last=data["results"][-1];image=last.get("image") or last.get("observation",{}).get("image")
    if image and Path(image["path"]).is_file():content.append({"type":"image","data":base64.b64encode(Path(image["path"]).read_bytes()).decode(),"mimeType":image.get("mimeType","image/png")})
    result={"content":content,"isError":"error" in data}
    if structured:result["structuredContent"]=data
    return result

