"""Read-only scrcpy H.264 capture, decoded to latest-frame MJPEG on the host."""
import os
import io
from pathlib import Path
import re
import secrets
import select
import shutil
import socket
import subprocess
import time
from wda_screen import MJPEGParser


def executable(name):
    return shutil.which(name) or next((str(p/name) for p in (Path('/opt/homebrew/bin'),Path('/usr/local/bin')) if (p/name).is_file()),None)


def dependencies():
    scrcpy,ffmpeg=executable('scrcpy'),executable('ffmpeg')
    if not scrcpy or not ffmpeg:return None
    binary=Path(scrcpy).resolve()
    jar=binary.parent.parent/'share/scrcpy/scrcpy-server'
    if not jar.is_file():return None
    version=subprocess.check_output([scrcpy,'--version'],timeout=5,text=True).splitlines()[0]
    match=re.search(r'scrcpy ([0-9]+\.[0-9]+(?:\.[0-9]+)?)',version)
    return (str(jar),match[1],ffmpeg) if match else None


def idle_snapshot(screen,stop):
    """Prove freshness when the encoder has no new frames (including static UI)."""
    from PIL import Image
    if stop.is_set():return
    if screen.client.locked():screen.set_paused(True,reason='device_locked');return
    raw=screen.client.preview_screenshot()
    frame=Image.open(io.BytesIO(raw));native={'width':frame.width,'height':frame.height}
    frame.thumbnail((1600,1600))
    output=io.BytesIO();frame.convert('RGB').save(output,format='JPEG',quality=75)
    if screen.client.locked():screen.set_paused(True,reason='device_locked');return
    screen.transport='scrcpy-idle-snapshot'
    screen._publish_frame(output.getvalue(),frame.width,frame.height,stop,viewport=native)


def capture(screen,stop):
    try:deps=dependencies()
    except (OSError,ValueError,subprocess.SubprocessError):return False
    if not deps:return False
    client=screen.client
    jar,version,ffmpeg=deps
    scid=secrets.token_hex(4);scid=f'{int(scid,16)&0x7fffffff:08x}'
    remote=f'/data/local/tmp/android-use-{scid}.jar'
    port=None;server=None;decoder=None;connection=None
    log=open(screen.state_dir/'video.log','w')
    try:
        if client.locked():screen.set_paused(True,reason='device_locked');return True
        client.command(['push',jar,remote])
        port=int(client.command(['forward','tcp:0',f'localabstract:scrcpy_{scid}']))
        args=[f'CLASSPATH={remote}','app_process','/','com.genymobile.scrcpy.Server',version,
              f'scid={scid}','tunnel_forward=true','audio=false','control=false','cleanup=false',
              'raw_stream=true','max_size=0','max_fps=30','video_bit_rate=8000000']
        import shlex
        # MCP owns stdin. An interactive adb shell otherwise consumes JSON-RPC
        # requests from that same pipe, leaving the host waiting for lost IDs.
        server=subprocess.Popen([client.adb,'-s',client.serial,'shell',shlex.join(args)],stdin=subprocess.DEVNULL,stdout=log,stderr=log)
        deadline=time.monotonic()+5
        while screen._live(stop) and time.monotonic()<deadline:
            try:
                connection=socket.create_connection(('127.0.0.1',port),timeout=.3)
                # ADB accepts a forward even before the remote socket exists.
                connection.settimeout(2)
                if connection.recv(1,socket.MSG_PEEK):break
            except OSError:pass
            if connection:connection.close();connection=None
            stop.wait(.1)
        if connection is None:return False
        connection.settimeout(None)
        decoder=subprocess.Popen([ffmpeg,'-hide_banner','-loglevel','error','-probesize','32',
            '-analyzeduration','0','-flags','low_delay','-threads','1','-f','h264','-i','pipe:0',
            '-an','-vf',"scale=w='if(gte(iw,ih),min(1600,iw),-2)':h='if(gte(iw,ih),-2,min(1600,ih))'",
            '-threads','2','-c:v','mjpeg','-q:v','7','-fps_mode','passthrough',
            '-f','image2pipe','pipe:1'],stdin=connection,stdout=subprocess.PIPE,stderr=log)
        parser=MJPEGParser();last_lock=0;received=False;started=time.monotonic()
        video_size=None;viewport=None;last_geometry=0;last_picture=started
        screen.transport='scrcpy'
        while screen._live(stop) and decoder.poll() is None:
            if time.monotonic()-last_lock>.15:
                if client.locked():screen.set_paused(True,reason='device_locked');return True
                last_lock=time.monotonic()
            if not select.select([decoder.stdout],[],[],.1)[0]:
                if not received and time.monotonic()-started>8:return False
                if received and time.monotonic()-last_picture>1:
                    idle_snapshot(screen,stop)
                    last_picture=time.monotonic()
                continue
            data=os.read(decoder.stdout.fileno(),65536)
            if not data:break
            frames=parser.feed(data)
            if frames:
                raw,(width,height)=frames[-1]
                # Preview pixels may be scaled; gestures always use native display pixels.
                # Recheck periodically as a resolution change can keep the same aspect.
                if video_size!=(width,height) or time.monotonic()-last_geometry>2:
                    info=client.rpc('deviceInfo')
                    viewport={'width':info['displayWidth'],'height':info['displayHeight']}
                    video_size=(width,height);last_geometry=time.monotonic()
                screen.transport='scrcpy'
                screen._publish_frame(raw,width,height,stop,viewport=viewport);received=True
                last_picture=time.monotonic()
        return not screen._live(stop)
    except (OSError,ValueError,subprocess.SubprocessError):return False
    finally:
        if connection:connection.close()
        for child in (decoder,server):
            if child:
                screen._terminate(child)
                if child.stdout:child.stdout.close()
        if port:
            try:client.command(['forward','--remove',f'tcp:{port}'])
            except Exception:pass
        log.close()
        try:client.command(['shell','rm','-f',remote])
        except Exception:pass
