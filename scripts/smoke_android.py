#!/usr/bin/env python3
"""Verify Android MCP and optional Settings actions; never runs in ordinary tests."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from android_use import Runtime,SCREEN_URI
from android_client import AndroidError


def main():
    p=argparse.ArgumentParser();p.add_argument('--exercise-settings',action='store_true');a=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    requests=[{'jsonrpc':'2.0','id':i,'method':method,'params':params} for i,(method,params) in enumerate([
        ('initialize',{'protocolVersion':'2025-11-25'}),('tools/list',{}),('resources/read',{'uri':SCREEN_URI}),('ping',{})])]
    done=subprocess.run([sys.executable,str(root/'server/android_use.py')],input=''.join(json.dumps(r)+'\n' for r in requests),capture_output=True,text=True,timeout=20,check=True)
    replies=[json.loads(line) for line in done.stdout.splitlines()]
    assert len(replies)==4 and all('result' in r for r in replies)
    assert len(replies[1]['result']['tools'])==19
    assert 'Android' in replies[2]['result']['contents'][0]['text']
    report={'mcp_initialize':True,'tools':19,'widget_resource':True}
    r=Runtime()
    def call(op,**kwargs):return r.call('pua_'+op,kwargs)
    try:
        ready=call('ready',screenshot=True)
        assert ready['ready'],ready
        report['ready']=True
        if a.exercise_settings:
            call('launch_app',package='com.android.settings',verify=True)
            call('wait',selector={'content_desc':'搜索'})
            call('tap',selector={'content_desc':'搜索'},expect={'class_name':'android.widget.EditText'})
            call('type_text',text='蓝牙',verify=True,expect={'text':'蓝牙','class_name':'android.widget.TextView'})
            report['unicode_exact_readback']=True
            rows=call('collect_list',row_selector={'resource_id':'android:id/title'},max_pages=1)
            assert len(rows['rows'])>0
            report['collect_rows']=len(rows['rows'])
            call('observe',mode='both')
            r.screen.frame();deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                frame=r.screen.frame()
                if frame['frame']:break
                time.sleep(.3)
            assert frame['frame_available'];report['preview_frame']=True
            call('screen',action='pause');assert r.screen.frame()['frame'] is None
            call('screen',action='resume');report['preview_pause_resume']=True
            result=call('batch',steps=[{'op':'tap','args':{'selector':{'resource_id':'android:id/title','text':'蓝牙'}}},
                                     {'op':'wait','args':{'selector':{'text':'连接','class_name':'android.widget.TextView'}}},
                                     {'op':'press_button','args':{'name':'back'}},
                                     {'op':'wait','args':{'selector':{'resource_id':'com.android.settings.intelligence:id/search_src_text'}}}])
            assert result['complete'],result;report['batch_navigation']=True
            call('type_text',text='',selector={'resource_id':'com.android.settings.intelligence:id/search_src_text'},verify=False)
            call('tap',selector={'resource_id':'com.android.settings.intelligence:id/search_back_btn'},expect={'content_desc':'搜索'});time.sleep(.5)
            # Search returns to the previously scrolled Settings list. Either direction may hit its boundary.
            try:call('swipe',direction='down',verify=True);report['swipe_anchor_verified']=True
            except AndroidError as exc:
                if exc.code!='no_scroll_progress':raise
                report['swipe_anchor_verified']=False
                report['swipe_boundary_stopped']=True
            call('press_button',name='home')
            call('wait',selector={'package':'com.sec.android.app.launcher'})
            report['home_verified']=True
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:r.close()


if __name__=='__main__':main()
