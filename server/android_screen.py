"""Demand-driven Android preview using the existing widget frame/event protocol."""
import io
import time
import threading
from android_client import AndroidError
from wda_screen import ScreenHub


class AndroidScreen(ScreenHub):
    def __init__(self,state_dir,client):
        super().__init__(state_dir); self.client=client
        self._capture_size=None
        self.transport="screenshot"
        self._last_capture=None

    def _wire(self,state,after_seq=0,last_event_id=0,include_frame=True):
        data=super()._wire(state,after_seq,last_event_id,include_frame)
        age=None if self._last_capture is None else max(0,time.monotonic()-self._last_capture)
        data['capture_age_ms']=None if age is None else round(age*1000)
        data['transport']=self.transport
        if age is not None and age>3:
            data['frame']=None;data['frame_available']=False
        return data

    def frame(self,after_seq=0,last_event_id=0):
        # A stalled decoder must not leave the old picture marked as live forever.
        if self._frame is not None and self._last_capture is not None and time.monotonic()-self._last_capture>5:
            self.restart()
        data=super().frame(after_seq,last_event_id)
        data['transport']=self.transport
        return data

    def shutdown(self):
        self.close()
        thread=self._thread
        if thread and thread is not threading.current_thread():thread.join(timeout=6)
        return not (thread and thread.is_alive())

    def _publish_frame(self,raw,width,height,stop,viewport=None):
        with self._lock:
            if stop.is_set() or self._paused():return
            # Rotation / fold-unfold / resolution changes must reach the widget
            # even if the model has not requested another UI tree observation.
            native=viewport or {'width':width,'height':height}
            native_size=(native['width'],native['height'])
            if self._capture_size!=native_size:
                self.set_viewport(native)
                self._capture_size=native_size
            self._last_capture=time.monotonic()
            super()._publish_frame(raw,width,height,stop)

    def _run(self,stop):
        from PIL import Image
        from android_video import capture
        try:
            if capture(self,stop):return
        except AndroidError:pass
        self.transport="screenshot"
        jpeg_supported=True
        while self._live(stop):
            started=time.monotonic()
            try:
                if self.client.locked():
                    self.set_paused(True,reason='device_locked'); return
                raw=None
                if jpeg_supported:
                    try:raw=self.client.preview_screenshot()
                    except AndroidError:
                        # Read-only fallback for devices without takeScreenshot.
                        # Disable for this stream instead of paying a timeout per frame.
                        jpeg_supported=False
                if raw is None:
                    raw=self.client.screenshot()
                    image=Image.open(io.BytesIO(raw))
                    buffer=io.BytesIO(); image.convert('RGB').save(buffer,format='JPEG',quality=75)
                    raw=buffer.getvalue()
                image=Image.open(io.BytesIO(raw))
                # A lock occurring during capture must not publish authentication UI.
                if self.client.locked():
                    self.set_paused(True,reason='device_locked'); return
                self._publish_frame(raw,image.width,image.height,stop)
            except (AndroidError,OSError,ValueError):
                with self._lock: self._frame=None
            stop.wait(max(0, .25-(time.monotonic()-started)))
