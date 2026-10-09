"""Android native selectors, bounded observations and conservative action semantics."""
import collections
import io
import json
from pathlib import Path
import re
import time
import uuid
import xml.etree.ElementTree as ET
from android_client import AndroidError

FIELDS = {'text':'text','resource_id':'resource-id','content_desc':'content-desc',
          'class_name':'class','package':'package','enabled':'enabled','clickable':'clickable',
          'scrollable':'scrollable','focused':'focused','checked':'checked'}


def parse_nodes(xml):
    root = ET.fromstring(xml)
    result = []
    for element in root.iter('node'):
        a = element.attrib
        bounds = re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', a.get('bounds',''))
        if not bounds: continue
        left, top, right, bottom = map(int, bounds.groups())
        node = {k:a.get(v,'') for k,v in FIELDS.items() if a.get(v,'')}
        for key in ('enabled','clickable','scrollable','focused','checked'):
            node[key] = a.get(FIELDS[key]) == 'true'
        node.update(rect=[left,top,right-left,bottom-top], password=a.get('password') == 'true')
        if node['password']: node['text'] = '[secure]'; node['content_desc'] = ''
        result.append(node)
    return result


def matches(node, selector):
    return all(key == 'index' or (str(value) in node.get('text','') if key == 'text_contains'
               else node.get(key) == value) for key,value in selector.items())


class AndroidController:
    def __init__(self, client, state_dir, screen):
        self.client, self.state_dir, self.screen = client, Path(state_dir), screen
        self.snapshots = collections.OrderedDict()
        self.deadline = None

    def context(self):
        info = self.client.rpc('deviceInfo')
        return {'package':info['currentPackageName'], 'rotation':info['displayRotation'],
                'width':info['displayWidth'], 'height':info['displayHeight']}

    def tree(self):
        return parse_nodes(self.client.rpc('dumpWindowHierarchy',[False,50]))

    def observe(self, mode='tree', max_nodes=150):
        context = self.context()
        ident = uuid.uuid4().hex[:12]
        self.snapshots[ident] = (context, time.monotonic())
        while len(self.snapshots)>20: self.snapshots.popitem(last=False)
        viewport = {key:context[key] for key in ('width','height')}
        self.screen.set_viewport(viewport)
        result = {'observation_id':ident,'viewport':viewport,'coordinate_system':'display_pixels',
                  'package':context['package'],'rotation':context['rotation']}
        if mode in ('tree','both'):
            nodes = self.tree(); result.update(nodes=nodes[:max_nodes],truncated=len(nodes)>max_nodes,total_nodes=len(nodes))
        if mode in ('screenshot','both'):
            from PIL import Image
            raw = self.client.screenshot()
            image = Image.open(io.BytesIO(raw)); native = image.size
            image.thumbnail((1000,1600))
            path = self.state_dir/'observation.png'; image.save(path); path.chmod(0o600)
            result['image'] = {'path':str(path),'mimeType':'image/png','width':image.width,'height':image.height,
                               'pixel_to_display':[native[0]/image.width,native[1]/image.height]}
            if native != (context['width'],context['height']):
                raise AndroidError('viewport_changed','Rotation or resolution changed during capture; observe again.')
        return result

    def find(self, selector, limit=30):
        found = [n for n in self.tree() if matches(n,selector)]
        return {'matches':len(found),'nodes':[dict(n,index=i) for i,n in enumerate(found[:limit])],
                'truncated':len(found)>limit}

    def resolve(self, selector):
        found = self.find(selector,30)
        nodes = found['nodes']
        if not nodes: raise AndroidError('target_not_found','Target is absent; inspect a screenshot before another action.')
        if 'index' in selector:
            if selector['index'] >= len(nodes): raise AndroidError('target_not_found','Selector index is absent.')
            node = nodes[selector['index']]
        elif found['matches'] != 1:
            raise AndroidError('ambiguous_target','Several controls match. Choose from the observed candidates.',details={'candidates':nodes})
        else: node = nodes[0]
        c = self.context(); x,y,w,h = node['rect']
        if not node['enabled'] or w<=0 or h<=0 or not (0<=x+w/2<c['width'] and 0<=y+h/2<c['height']):
            raise AndroidError('offscreen_target','Target is disabled or outside the viewport.')
        return node

    def check_context(self, observation_id):
        if observation_id:
            cached = self.snapshots.get(observation_id)
            if not cached or time.monotonic()-cached[1]>120 or cached[0]!=self.context():
                raise AndroidError('stale_observation','App, rotation or viewport changed, or observation expired; observe again.')

    def finish(self, expect=None, observe='none', **result):
        result = {'action_executed':True,'action_complete':True,'verified':False,**result}
        if expect:
            try: result['postcondition']=self.wait(expect)
            except AndroidError as exc:
                exc.details['action_executed']=True; raise
            result['verified']=True
        result['verification_deferred']=not result['verified']
        if observe!='none':
            time.sleep(.35); result['observation']=self.observe(observe)
        return result

    def tap(self, selector=None, x=None, y=None, observation_id=None, expect=None, observe='none'):
        self.check_context(observation_id)
        if selector:
            node=self.resolve(selector); left,top,w,h=node['rect']; x,y=left+w/2,top+h/2
        c=self.context()
        if not (0<=x<c['width'] and 0<=y<c['height']): raise AndroidError('invalid_argument','Tap outside display.')
        self.screen.gesture('tap',point={'x':x,'y':y},viewport=c)
        self.client.rpc('click',[int(x),int(y)],mutation=True)
        return self.finish(expect,observe)

    def swipe(self,direction='up',region=None,observation_id=None,verify=False,expect=None,observe='none'):
        self.check_context(observation_id); c=self.context()
        r=region or {'x':c['width']*.15,'y':c['height']*.2,'width':c['width']*.7,'height':c['height']*.6}
        x,y,w,h=(r[k] for k in ('x','y','width','height'))
        if x<0 or y<0 or w<=0 or h<=0 or x+w>c['width'] or y+h>c['height']:
            raise AndroidError('invalid_argument','Swipe region outside display.')
        points={'up':(x+w/2,y+h*.8,x+w/2,y+h*.2),'down':(x+w/2,y+h*.2,x+w/2,y+h*.8),
                'left':(x+w*.8,y+h/2,x+w*.2,y+h/2),'right':(x+w*.2,y+h/2,x+w*.8,y+h/2)}
        coords=list(map(int,points[direction])); before=self.tree() if verify else []
        self.screen.gesture('drag',from_point={'x':coords[0],'y':coords[1]},to_point={'x':coords[2],'y':coords[3]},duration_ms=350,viewport=c)
        self.client.rpc('swipe',coords+[70],mutation=True)
        if verify:
            time.sleep(.5); after=self.tree(); axis=1 if direction in ('up','down') else 0; sign=-1 if direction in ('up','left') else 1
            moved=any(a.get('text') and not a['text'].isnumeric() and a.get('text')==b.get('text')
                      and a.get('resource_id')==b.get('resource_id') and (b['rect'][axis]-a['rect'][axis])*sign>12
                      for a in before for b in after)
            if not moved: raise AndroidError('no_scroll_progress','Gesture sent; no stable anchor movement proven. Inspect screenshot; this does not prove end of list.',details={'action_executed':True})
        return self.finish(expect,observe,verified=verify)

    def type_text(self,text,selector=None,replace=True,verify=False,submit=False,allow_newlines=False,expect=None,observe='none'):
        node=self.resolve(selector or {'focused':True,'class_name':'android.widget.EditText'})
        if node['password']:
            self.screen.set_paused(True)
            raise AndroidError('secure_field','Enter passwords on the phone; preview paused.')
        if node.get('class_name') not in ('android.widget.EditText','android.widget.AutoCompleteTextView'):
            raise AndroidError('not_editable','Choose an editable field from a fresh observation.')
        # Bind to a fresh native selector. setText is one RPC even for long Unicode text;
        # it replaces the field atomically from the caller perspective, with no replay.
        from uiautomator2._selector import Selector
        criteria={'className':node['class_name'],'packageName':node['package']}
        if node.get('resource_id'): criteria['resourceId']=node['resource_id']
        if node.get('focused'): criteria['focused']=True
        native=Selector(**criteria)
        if self.client.rpc('count',[native]) != 1:
            raise AndroidError('ambiguous_target','Cannot uniquely bind the editable control.')
        expected=text if replace else node.get('text','')+text
        if expected:
            self.client.rpc('setText',[native,expected],mutation=True,timeout=20)
        else:
            self.client.rpc('clearTextField',[native],mutation=True,timeout=20)
        if verify or submit:
            deadline=time.monotonic()+2
            while True:
                actual=self.client.rpc('getText',[native])
                if actual==expected or time.monotonic()>=deadline:break
                time.sleep(.2)
            if actual!=expected: raise AndroidError('input_mismatch','Text did not round-trip exactly; do not submit or replay.',details={'action_executed':True,'expected_length':len(expected),'actual_length':len(actual or '')})
        if submit: self.client.rpc('pressKeyCode',[66],mutation=True)
        return self.finish(expect,observe,verified=(verify and not submit),input_complete=True,characters=len(text),submitted=submit)

    def press_button(self,name,expect=None,observe='none'):
        codes={'home':3,'back':4,'recent':187,'enter':66,'volumeup':24,'volumedown':25}
        self.client.rpc('pressKeyCode',[codes[name]],mutation=True)
        return self.finish(expect,observe)

    def launch_app(self,package,verify=False,expect=None,observe='none'):
        output=self.client.command(['shell','cmd','package','resolve-activity','--brief','-a','android.intent.action.MAIN','-c','android.intent.category.LAUNCHER',package])
        components=[s.strip() for s in output.splitlines() if re.fullmatch(r'[\w.]+/[\w.$]+',s.strip())]
        if len(components)!=1 or not components[0].startswith(package+'/'):
            raise AndroidError('app_not_launchable','No unique installed launcher activity for this package.')
        # Samsung's am -W can hang even after the activity is visibly foreground.
        # Send once, then use our own bounded foreground check when requested.
        response=self.client.command(['shell','am','start','-n',components[0]],mutation=True)
        if 'Error:' in response: raise AndroidError('launch_failed','Android did not launch the activity.',uncertain=True)
        if verify:
            deadline=time.monotonic()+5
            while self.context()['package']!=package:
                if time.monotonic()>=deadline: raise AndroidError('postcondition_failed','Requested app is not foreground.',details={'action_executed':True})
                time.sleep(.2)
        return self.finish(expect,observe,verified=verify)

    def wait(self,selector,timeout_seconds=10):
        deadline=time.monotonic()+timeout_seconds
        if self.deadline: deadline=min(deadline,self.deadline)
        while True:
            result=self.find(selector)
            if result['matches']: return {'verified':True,**result}
            if time.monotonic()>=deadline: raise AndroidError('postcondition_failed','Expected control did not appear within the time budget.')
            time.sleep(.3)

    def scroll_find(self,selector,direction='up',max_swipes=1):
        for i in range(min(max_swipes,1)+1):
            found=self.find(selector)
            if found['matches']: return {'target':self.resolve(selector),'swipes':i,'verified':True}
            if i<min(max_swipes,1): self.swipe(direction); time.sleep(.5)
        raise AndroidError('search_exhausted','Target unresolved after one search step; inspect screenshot before another swipe.',details={'action_executed':bool(max_swipes)})

    def collect_list(self,row_selector,max_pages=5,end_selector=None):
        if self.deadline is None:
            self.deadline=time.monotonic()+25
            try:return self.collect_list(row_selector,max_pages,end_selector)
            finally:self.deadline=None
        rows={}; pages=[]; seen=set(); end=False; reason='page_budget'
        for i in range(max_pages):
            if self.deadline and time.monotonic()>self.deadline: reason='time_budget'; break
            nodes=self.tree(); current=[n for n in nodes if matches(n,row_selector)]
            fingerprint=json.dumps(current,sort_keys=True,ensure_ascii=False)
            pages.append({'page':i+1,'rows_seen':len(current)})
            for n in current:
                key=json.dumps({k:v for k,v in n.items() if k!='rect'},sort_keys=True,ensure_ascii=False); rows.setdefault(key,n)
                if len(rows)>=500:break
            if len(rows)>=500:reason='row_budget';break
            if end_selector and any(matches(n,end_selector) for n in nodes): end=True; reason='end_marker'; break
            if not current: reason='no_rows'; break
            if fingerprint in seen: reason='repeated_page'; break
            seen.add(fingerprint)
            if i+1<max_pages: self.swipe(); time.sleep(.5)
        return {'rows':list(rows.values()),'pages':pages,'end_marker_seen':end,'stop_reason':reason,'complete':False,
                'limitations':['Identical accessibility rows may collapse. Only exposed controls are collected. Reconcile expected counts before claiming completeness.']}
