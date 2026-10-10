#!/usr/bin/env python3
"""Android Use MCP runtime, independent of the iPhone service and configuration."""
import argparse
import collections
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from android_client import AndroidClient, AndroidError, adb_path
from android_controller import AndroidController
from android_screen import AndroidScreen
from android_widget import android_html
# Protocol validation / response encoding are platform-neutral existing utilities.
from phone_protocol import obj,string,num,BOOL,validate,result_content,copy_png,SCREEN_META,PROTOCOLS

VERSION='0.1.6'
SCREEN_URI='ui://android-use/screen-0.1.6.html'
ROOT=Path(__file__).resolve().parents[1]
STATE=Path(os.environ.get('ANDROID_USE_STATE_DIR',str(Path.home()/'.local/share/android-use/state')))
SEL=obj({**{k:string(max_length=2000) for k in ('text','text_contains','resource_id','content_desc','class_name','package')},
         **{k:BOOL for k in ('enabled','clickable','scrollable','focused','checked')},'index':num(0,29,'integer')})
SEL['minProperties']=1
OBS=string(enum=['none','tree','screenshot','both'])
POST={'expect':SEL,'observe':OBS}
REGION=obj({k:num(0 if k in ('x','y') else 1,20000) for k in ('x','y','width','height')},('x','y','width','height'))
SCHEMAS={
 'doctor':obj({}),
 'setup':obj({'action':string(enum=['status','discover','configure','start','stop','pair','connect']),
              'serial':string(max_length=200),'address':string(max_length=260),'code':string(max_length=16),
              'job_id':string(max_length=64),'wait_seconds':num(0,20)},('action',)),
 'ready':obj({'screenshot':BOOL,'recover':BOOL}),
 'observe':obj({'mode':string(enum=['tree','screenshot','both']),'max_nodes':num(1,500,'integer')}),
 'find':obj({'selector':SEL,'limit':num(1,30,'integer')},('selector',)),
 'tap':obj({'selector':SEL,'x':num(0,20000),'y':num(0,20000),'observation_id':string(),**POST}),
 'swipe':obj({'direction':string(enum=['up','down','left','right']),'region':REGION,'observation_id':string(),'verify':BOOL,**POST}),
 'type_text':obj({'text':{'type':'string','maxLength':10000},'selector':SEL,'replace':BOOL,'verify':BOOL,'submit':BOOL,'allow_newlines':BOOL,**POST},('text',)),
 'press_button':obj({'name':string(enum=['home','back','recent','enter','volumeup','volumedown']),**POST},('name',)),
 'launch_app':obj({'package':string(),'verify':BOOL,**POST},('package',)),
 'wait':obj({'selector':SEL,'timeout_seconds':num(0,20)},('selector',)),
 'scroll_find':obj({'selector':SEL,'direction':string(enum=['up','down','left','right']),'max_swipes':num(0,1,'integer')},('selector',)),
 'collect_list':obj({'row_selector':SEL,'max_pages':num(1,10,'integer'),'end_selector':SEL},('row_selector',)),
 'apps':obj({'query':{'type':'string','maxLength':100},'limit':num(1,100,'integer')}),
 'metrics':obj({'reset':BOOL}),
 'screen':obj({'action':string(enum=['open','pause','resume'])}),
 'screen_frame':obj({'after_seq':num(0,9007199254740991,'integer'),'last_event_id':num(0,9007199254740991,'integer')}),
 'screen_action':obj({'action':string(enum=['refresh','home','screenshot'])},('action',)),
}
BATCH=('observe','tap','swipe','type_text','press_button','launch_app','wait','scroll_find')
SCHEMAS['batch']=obj({'steps':{'type':'array','minItems':1,'maxItems':20,'items':{'oneOf':[obj({'op':{'type':'string','const':op},'args':SCHEMAS[op]},('op','args')) for op in BATCH]}}},('steps',))
DESCRIPTIONS={
 'doctor':'Read ADB/dependency and device status without starting UI Automator.',
 'setup':'First call status; configure one serial, then start once. Pending startup returns job_id; poll status with that ID. USB unauthorized requires user authorization. Android 11+ wireless debugging: pair address/code then connect address. stop only drops local bindings, never kills an unrelated phone service.',
 'ready':'Verify unlocked selected Android, UI Automator tree and optional screenshot. Only ready=true allows actions; run setup start to recover transport. Never replay an uncertain action. Also opens the screen widget.',
 'observe':'Fresh Android tree and/or image. Coordinates are display pixels; image pixels multiplied by image.pixel_to_display give action coordinates. Nodes expose Android text/resource_id/content_desc/class_name. observation_id guards app, rotation and viewport, not overlay occlusion.',
 'find':'Find native controls using exact selector fields or text_contains. index selects among matches in tree order. Use fields from fresh observations; repeated controls require disambiguation.',
 'tap':'Tap a uniquely resolved visible control or x/y display pixels. Executes once; expect optionally checks a postcondition. On failure inspect the returned screenshot, not another blind selector retry.',
 'swipe':'One finger gesture in a display-pixel region. verify checks common text-anchor movement once. No progress does not prove end of list. Inspect screenshot before another gesture.',
 'type_text':'Set full Unicode text in one request on a unique nonsecure EditText; omit selector for focused field. replace defaults true. verify checks exact readback; submit always verifies first. No automatic replay or partial continuation. Secure fields require user takeover. Custom editors may need a separate adapter.',
 'press_button':'Send one Android home/back/recent/enter/volume key event. expect verifies a page condition; key acceptance alone is not business success.',
 'launch_app':'Resolve and launch an installed package once; get package from apps first. verify checks foreground. No assumed package IDs.',
 'wait':'Bounded polling for a selector. Presence is a UI condition, not business outcome proof.',
 'scroll_find':'Find a target, with at most one swipe, then stop with screenshot if unresolved.',
 'collect_list':'Bounded accessibility row collection by row_selector. Deduplicates equal exposed rows; never claims complete coverage without external reconciliation.',
 'apps':'Installed package lookup. Query package substring or known name aliases; aliases are returned only when actually installed. Unknown display names require visible launcher evidence.',
 'batch':'Up to 20 validated steps, stopping on error, uncertain outcome, unverified submission or 25s budget. stopped_at is the zero-based next/unresolved step; never replay completed or uncertain steps.',
 'metrics':'Local in-memory tool timing and error counts; no input text or screenshots.',
 'screen':'Open/reuse Android preview. Pause before user authentication; resume only after user completion. Preview frames are not model observations.',
 'screen_frame':'App-only cached preview and gesture cursor events; capture runs only while widget polls.',
 'screen_action':'User widget toolbar: refresh, home or screenshot clipboard. No phone actions while preview is paused.'}
def published(schema):
    used=set()
    def visit(value):
        if value==SEL:used.add('selector');return {'$ref':'#/$defs/selector'}
        if isinstance(value,dict):return {k:visit(v) for k,v in value.items()}
        if isinstance(value,list):return [visit(v) for v in value]
        return value
    result=visit(schema)
    if used:result['$defs']={'selector':SEL}
    return result


TOOLS=[{'name':'pua_'+op,'description':DESCRIPTIONS[op],'inputSchema':published(schema),
        'annotations':{'readOnlyHint':op in ('doctor','observe','find','wait','apps','metrics','screen_frame'),
                       'destructiveHint':False,'openWorldHint':True}} for op,schema in SCHEMAS.items()]
for tool in TOOLS:
    if tool['name'] in ('pua_ready','pua_screen'): tool['_meta']={'ui':{'resourceUri':SCREEN_URI}}
    if tool['name'] in ('pua_screen_frame','pua_screen_action'): tool['_meta']={'ui':{'visibility':['app']}}
INSTRUCTIONS='Android Use controls one explicitly selected device. Read android-use-setup and android-use skills. First setup status/configure/start, then ready; only ready=true permits actions. Selectors are Android native fields, coordinates display pixels. Never replay uncertain clicks or input. Inspect screenshots after failures. Pause preview for user authentication and resume only on confirmation. Verify final business outcomes. All tools use pua_ in the android_use namespace.'


def save(path,data):
    temp=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False)); temp.chmod(0o600); temp.replace(path)


def read(path):
    try: return json.loads(path.read_text())
    except (OSError,ValueError): return {}


def semantics(op,args):
    if op=='tap' and (('selector' in args)==('x' in args and 'y' in args) or ('selector' in args and ('x' in args or 'y' in args))):
        raise AndroidError('invalid_argument','Supply either selector or both x/y.')
    if op=='type_text':
        if any(ord(c)<32 and c not in '\n\r' or ord(c)==127 for c in args['text']): raise AndroidError('invalid_argument','Control characters are unsupported.')
        if any(c in args['text'] for c in '\n\r') and not args.get('allow_newlines'): raise AndroidError('newline_requires_intent','Multiline text requires allow_newlines=true.')
    if op=='launch_app' and not re.fullmatch(r'[A-Za-z][\w]*(?:\.[A-Za-z][\w]*)+',args['package']): raise AndroidError('invalid_argument','Invalid package identifier.')
    for key in ('selector','expect','row_selector','end_selector'):
        if key in args and set(args[key])=={'index'}: raise AndroidError('invalid_argument','index alone is not a selector.')
    if op=='batch':
        for step in args['steps']: semantics(step['op'],step['args'])


class Runtime:
    def __init__(self,state_dir=None):
        self.state_dir=Path(state_dir or STATE).expanduser().resolve(); self.state_dir.mkdir(parents=True,exist_ok=True,mode=0o700); self.state_dir.chmod(0o700)
        self.client=AndroidClient(read(self.state_dir/'config.json').get('serial'))
        self.screen=AndroidScreen(self.state_dir,self.client)
        self.phone=AndroidController(self.client,self.state_dir,self.screen)
        self.records=collections.deque(maxlen=1000)
        self.recovery_started=None
        self.widget_session_id="android-use-screen-"+uuid.uuid4().hex

    def select_device(self, serial):
        # Video cleanup still addresses self.client. Never change its serial
        # until the old capture thread has released its socket/JAR/forward.
        if not self.screen.shutdown():
            raise AndroidError('preview_stopping', 'Preview cleanup is still running; retry device selection after it finishes. No device was switched.')
        self.client.close()
        self.client.serial=serial
        self.screen.device={}
        self.phone.snapshots.clear()

    def doctor(self):
        versions={}
        for name in ('uiautomator2','adbutils','Pillow'):
            try: versions[name]=importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError: versions[name]=None
        return {'adb':adb_path(),'dependencies':versions,'devices':self.client.devices() if self.client.adb else [],'configured':bool(self.client.serial),
                'selected_serial':self.client.serial,'platform':'Android','host_support':'macOS; other hosts unverified'}

    def setup(self,action,serial=None,address=None,code=None,job_id=None,wait_seconds=0):
        job_path=self.state_dir/'setup-job.json'
        if action in ('discover','status'):
            job=read(job_path)
            if job_id and job.get('id')!=job_id: raise AndroidError('unknown_job','Setup job ID not found.')
            until=time.monotonic()+wait_seconds
            while job.get('state')=='running' and time.monotonic()<until:
                time.sleep(.25); job=read(job_path)
            if job.get('state')=='running' and time.time()-job.get('started',0)>90:
                job.update(state='failed',error='Startup worker exceeded 90 seconds. Inspect connection then start again.');save(job_path,job)
            return {**self.doctor(),'job':job}
        if action=='configure':
            if not serial: raise AndroidError('invalid_argument','configure needs serial from discover.')
            if read(job_path).get('state')=='running': raise AndroidError('setup_busy','Wait for the active setup before switching devices.')
            if serial not in [d['serial'] for d in self.client.devices()]: raise AndroidError('device_disconnected','Serial absent from ADB inventory.')
            self.select_device(serial)
            save(self.state_dir/'config.json',{'serial':serial}); return {'configured':True,'serial':serial}
        if action in ('pair','connect'):
            if not address or not re.fullmatch(r'[A-Za-z0-9.-]+:\d{1,5}',address) or not 0<int(address.rsplit(':',1)[1])<65536:
                raise AndroidError('invalid_argument','Use the phone-displayed host:port.')
            if action=='pair' and (not code or not re.fullmatch(r'\d{6}',code)): raise AndroidError('invalid_argument','Pairing needs the six-digit phone-displayed code.')
            output=self.client.command([action,address]+([code] if action=='pair' else []),device=False,timeout=20)
            if not any(s in output.lower() for s in ('successfully paired','connected to','already connected')):
                raise AndroidError('wireless_failed','Pair/connect failed; verify the phone address, code and local network.')
            return {'ok':True,'next_step':'Discover and configure the connected serial.'}
        if action=='stop':
            self.screen.close(); self.client.close()
            job=read(job_path)
            if job.get('state')=='running':raise AndroidError('setup_busy','Wait for startup before stopping its worker.')
            if job:
                job['stop_requested']=True;save(job_path,job)
            return {'stopped':True,'scope':'Local preview/forward and this installation\'s startup worker; an externally started UI Automator is preserved.'}
        if not self.client.serial:
            devices=[d for d in self.client.devices() if d['state']=='device']
            if len(devices)!=1: raise AndroidError('device_selection_required','Configure an explicit serial; available device count is not one.')
            self.client.serial=devices[0]['serial']; save(self.state_dir/'config.json',{'serial':self.client.serial})
        job=read(job_path)
        if job.get('state')=='running' and time.time()-job.get('started',0)<90: return {'job':job,'reused':True}
        try:
            # USB reconnect invalidates the old forward, not necessarily the phone service.
            # Probe a fresh forward before replacing its owning worker.
            self.client.close(); self.client.attach(); self.client.rpc('deviceInfo'); return {'service':{'ready':True},'reused':True}
        except AndroidError: self.client.close()
        job={'id':uuid.uuid4().hex,'state':'running','started':time.time()};save(job_path,job)
        log=(self.state_dir/'setup.log').open('ab');os.chmod(log.name,0o600)
        try: subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--state-dir',str(self.state_dir),'--setup-worker',job['id']],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        finally: log.close()
        return {'job':job,'next_step':'Poll setup status with job_id; do not start duplicate workers.'}

    def guard(self):
        if self.screen.paused(): raise AndroidError('preview_paused','User takeover is active. Resume after user confirmation.')
        if self.client.locked():
            self.screen.set_paused(True,reason='device_locked')
            raise AndroidError('phone_locked','Unlock the phone yourself, then ready again.')

    def recover_service(self):
        # Recover transport only. Never replay any phone action.
        if self.recovery_started is not None and time.monotonic()-self.recovery_started<30:
            job=read(self.state_dir/'setup-job.json')
            return {'ready':False,'state':'recovering' if job.get('state')=='running' else 'recovery_required','job':job}
        self.recovery_started=time.monotonic()
        result=self.setup('start')
        return {'ready':False,'state':'recovering' if result.get('job',{}).get('state')=='running' else 'recheck_required',**result}

    def ready(self,screenshot=True,recover=True):
        pause_id=self.screen.locked_pause_id()
        if self.client.locked():
            self.screen.set_paused(True,reason='device_locked');return {'ready':False,'state':'phone_locked'}
        self.screen.resume_after_unlock(pause_id)
        if self.screen.paused(): return {'ready':False,'state':'authentication_paused'}
        try:observation=self.phone.observe('both' if screenshot else 'tree')
        except AndroidError as exc:
            if exc.code not in ('automation_unreachable','automation_error'):raise
            if not recover:return {'ready':False,'state':'recovery_required'}
            recovery=self.recover_service()
            if recovery['state']!='recheck_required':return recovery
            observation=self.phone.observe('both' if screenshot else 'tree')
        self.recovery_started=None
        self.screen.device={'model':self.client.command(['shell','getprop','ro.product.model'])}
        return {'ready':True,'platform':'Android','device':self.screen.device,'observation':observation,'preview':self.screen.start()}

    def apps(self,query='',limit=30):
        aliases={'设置':'com.android.settings','相机':'com.sec.android.app.camera','微信':'com.tencent.mm','chrome':'com.android.chrome','浏览器':'com.android.chrome'}
        packages=[s[8:] for s in self.client.command(['shell','pm','list','packages']).splitlines() if s.startswith('package:')]
        found=[p for p in packages if query.lower() in p.lower() or aliases.get(query.lower())==p]
        return {'apps':[{'package':p,'installed_verified':True} for p in found[:limit]],'matches':len(found),'truncated':len(found)>limit,'lookup_scope':'Installed package IDs plus a small verified-on-device alias set.'}

    def batch(self,steps):
        results=[]; deadline=time.monotonic()+25; self.phone.deadline=deadline
        try:
            for i,step in enumerate(steps):
                if time.monotonic()>deadline: return {'results':results,'complete':False,'stop_reason':'time_budget','stopped_at':i}
                mark=self.client.mutations
                try:
                    self.guard(); result=getattr(self.phone,step['op'])(**step['args']); results.append(result)
                except AndroidError as exc:
                    exc.details.setdefault('action_executed',self.client.mutations>mark or exc.uncertain)
                    if not self.screen.paused():
                        try:exc.details['observation']=self.phone.observe('screenshot')
                        except Exception:pass
                    return {'results':results,'error':exc.as_dict(),'complete':False,'stop_reason':'error','stopped_at':i,'replay_action':False}
                if result.get('submitted') and not result.get('verified'):
                    return {'results':results,'complete':False,'stop_reason':'submission_needs_verification','stopped_at':i+1}
            return {'results':results,'complete':True}
        finally: self.phone.deadline=None

    def call(self,name,args):
        if not isinstance(name,str) or not name.startswith('pua_') or name[4:] not in SCHEMAS: raise AndroidError('unknown_tool','Unknown Android tool.')
        op=name[4:];validate(args,SCHEMAS[op]);semantics(op,args)
        if op=='screen_frame': return self.screen.frame(**args)
        if op=='screen':
            action=args.get('action','open')
            if action=='pause':return self.screen.set_paused(True)
            if action=='resume':
                if self.client.locked():raise AndroidError('phone_locked','Unlock before resuming.')
                return self.screen.set_paused(False)
            return self.screen.start()
        start=time.monotonic();error=None;token=None;mark=self.client.mutations
        with (self.state_dir/'operation.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise AndroidError('device_busy','Another Android operation is active; wait without replaying.')
            try:
                serial=read(self.state_dir/'config.json').get('serial')
                if serial!=self.client.serial:
                    self.select_device(serial)
                if op=='doctor':return self.doctor()
                if op=='setup':return self.setup(**args)
                if op=='ready':return self.ready(**args)
                if op=='metrics':
                    result={'records':list(self.records),'rpc_calls':len(self.client.records),'rpc_seconds':sum(r['seconds'] for r in self.client.records)}
                    if args.get('reset'):self.records.clear();self.client.records.clear()
                    return result
                if op=='apps':return self.apps(**args)
                if op=='screen_action':
                    if args['action']=='refresh':
                        self.client.close()
                        if self.client.locked():
                            self.screen.set_paused(True,reason='device_locked')
                            return {**self.screen.start(),'service_ready':False}
                        pause_id=self.screen.reconnect_pause_id()
                        self.screen.resume_after_unlock(pause_id,explicit=True)
                        try:self.client.rpc('deviceInfo');service_ready=True
                        except AndroidError:
                            recovery=self.recover_service()
                            if recovery['state']=='recovering':
                                job=recovery.get('job',{})
                                if job.get('id'):self.setup('status',job_id=job['id'],wait_seconds=3)
                            try:self.client.rpc('deviceInfo');service_ready=True
                            except AndroidError:service_ready=False
                            return {**self.screen.restart(),'service_ready':service_ready,'service_recovering':not service_ready and read(self.state_dir/'setup-job.json').get('state')=='running','recovery':recovery}
                        return {**self.screen.restart(),'service_ready':service_ready}
                    self.guard()
                    if args['action']=='home':return self.phone.press_button('home')
                    copy_png(self.state_dir,self.client.screenshot());return {'copied':True}
                self.guard();token=self.screen.begin(op)
                if op=='batch': return self.batch(**args)
                return getattr(self.phone,op)(**args)
            except AndroidError as exc:
                error=exc.code
                exc.details.setdefault('action_executed',self.client.mutations>mark or exc.uncertain)
                # Only capture error context when user takeover/lock permits it.
                if op not in ('setup','doctor','screen_action','ready') and not self.screen.paused():
                    try:exc.details['observation']=self.phone.observe('screenshot')
                    except Exception:pass
                raise
            finally:
                if token:self.screen.end(token)
                self.records.append({'tool':op,'seconds':round(time.monotonic()-start,4),'error':error})

    def close(self):self.screen.shutdown();self.client.close()


def worker(state_dir,job_id):
    # A replacement must wait for the previous owner to stop its service.
    # Otherwise the old worker can kill the service just reused by the new one.
    with (Path(state_dir)/'service.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        run_worker(state_dir,job_id)


def run_worker(state_dir,job_id):
    state_dir=Path(state_dir);path=state_dir/'setup-job.json';job=read(path)
    if job.get('id')!=job_id:return
    try:
        os.environ['ADBUTILS_ADB_PATH']=adb_path() or 'adb'
        import uiautomator2 as u2
        d=u2.connect(read(state_dir/'config.json')['serial'])
        # Start/reuse only; no user actions through u2's retrying wrapper.
        if not d.info:raise RuntimeError('No device info')
        job.update(state='complete',service={'ready':True},pid=os.getpid())
        if read(path).get('id')==job_id:save(path,job)
        # u2 registers an atexit handler that terminates the service it starts.
        # Keep its ADB process owner alive across short-lived CLI/MCP clients.
        serial=read(state_dir/'config.json')['serial']
        while read(path).get('id')==job_id and not read(path).get('stop_requested') and read(state_dir/'config.json').get('serial')==serial:
            time.sleep(1)
        d.stop_uiautomator(wait=False)
        job.update(state='stopped',service={'ready':False})
    except Exception as exc:job.update(state='failed',error=type(exc).__name__,next_step='Read setup.log, check USB authorization and installed dependencies.')
    if read(path).get('id')==job_id:save(path,job)


def tool_result(runtime,params):
    name=params.get('name')
    try:
        data=runtime.call(name,params.get('arguments',{}))
        if name=='pua_screen_frame':return {'content':[],'structuredContent':data,'isError':False}
        result=result_content(data,structured=name in ('pua_screen','pua_screen_action'))
        if name in ('pua_screen','pua_ready'):result['_meta']={'openai/widgetSessionId':runtime.widget_session_id}
        return result
    except AndroidError as exc:return result_content({'error':exc.as_dict()},structured=name=='pua_screen_action')
    except Exception as exc:
        print('android-use failure: '+type(exc).__name__,file=sys.stderr)
        return result_content({'error':{'code':'internal_error','message':'Local failure; inspect stderr. Do not replay actions.','uncertain':True}})


def serve(runtime):
    writing=threading.Lock();jobs=queue.Queue()
    def send(value):
        with writing:print(json.dumps(value,ensure_ascii=False,allow_nan=False),flush=True)
    def execute():
        while True:
            job=jobs.get()
            if job is None:return
            ident,params=job
            send({'jsonrpc':'2.0','id':ident,'result':tool_result(runtime,params)})
    thread=threading.Thread(target=execute,daemon=True);thread.start()
    try:
        for line in sys.stdin:
            request=None
            try:
                if len(line)>1024*1024:raise ValueError()
                request=json.loads(line,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
                if not isinstance(request,dict) or request.get('jsonrpc')!='2.0' or not isinstance(request.get('method'),str):raise ValueError()
                if 'id' not in request:continue
                ident=request['id'];params=request.get('params',{});method=request['method']
                if isinstance(ident,bool) or not isinstance(ident,(str,int)) or not isinstance(params,dict):raise ValueError()
                if method=='initialize':result={'protocolVersion':params.get('protocolVersion') if params.get('protocolVersion') in PROTOCOLS else PROTOCOLS[0],'capabilities':{'tools':{},'resources':{}},'serverInfo':{'name':'android-use','version':VERSION},'instructions':INSTRUCTIONS}
                elif method=='ping':result={}
                elif method=='tools/list':result={'tools':TOOLS}
                elif method=='resources/list':result={'resources':[{'uri':SCREEN_URI,'name':'Android Screen','mimeType':'text/html;profile=mcp-app','_meta':SCREEN_META}]}
                elif method=='resources/read':
                    if params.get('uri')!=SCREEN_URI:raise ValueError()
                    html=android_html((ROOT/'assets/phone-screen.html').read_text())
                    result={'contents':[{'uri':SCREEN_URI,'mimeType':'text/html;profile=mcp-app','text':html,'_meta':SCREEN_META}]}
                elif method=='tools/call':
                    if params.get('name') not in ('pua_screen_frame','pua_screen','pua_screen_action'):
                        jobs.put((ident,params));continue
                    result=tool_result(runtime,params)
                else:
                    send({'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'Method not found'}});continue
                send({'jsonrpc':'2.0','id':ident,'result':result})
            except (ValueError,TypeError,KeyError):
                send({'jsonrpc':'2.0','id':request.get('id') if isinstance(request,dict) else None,'error':{'code':-32600,'message':'Invalid request'}})
    finally:jobs.put(None);thread.join()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state-dir');parser.add_argument('--setup-worker');parser.add_argument('--tool');parser.add_argument('--arguments',default='{}');args=parser.parse_args()
    if args.setup_worker:worker(args.state_dir,args.setup_worker);return 0
    runtime=Runtime(args.state_dir)
    try:
        if args.tool:
            result=tool_result(runtime,{'name':args.tool,'arguments':json.loads(args.arguments)})
            print(json.dumps(result,ensure_ascii=False));return int(result.get('isError',False))
        serve(runtime);return 0
    finally:runtime.close()


if __name__=='__main__':sys.exit(main())
