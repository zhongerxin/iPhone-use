"""Opt-in fast preview path.

The classic preview asks the runner for the window geometry before every gesture and
leaves the stream at the runner defaults. This module keeps the alternative choices in
one place so a deployment selects them and the classic path stays the default.

Select with IPHONE_USE_FAST_PREVIEW=1 (the same shape as IPHONE_USE_ANALYTICS). While it
is unset nothing here runs: the preview keeps its current behaviour and the runner keeps
its own settings.

Measured on an iPhone 15 Plus (iOS 27.0, WDA 16.14.0, USB):
- one GET /window/size round trip takes 278 ms and ran in front of every gesture;
- the runner streams at 10 fps with 402 KB frames; 24 fps at quality 35 measured ~92 KB.
- /wda/fastTap and /wda/fastDrag answer in 2.5-8 ms and 10-13 ms where the confirmed
  routes take 0.62-0.94 s and 1.73 s. Those routes need a runner that has them; a 404
  raises Unavailable so the caller can use the confirmed route instead.
"""
import os

# Deliberately no import from wda_client: that module reads stream_settings() at import
# time, and importing it here would make the pair circular.
ENABLED_VAR = "IPHONE_USE_FAST_PREVIEW"
VIEWPORT_MAX_AGE = 10
STREAM_SETTINGS = {"mjpegServerFramerate": 24, "mjpegServerScreenshotQuality": 35}
TAP_ROUTE = "/wda/fastTap"
DRAG_ROUTE = "/wda/fastDrag"


class Unavailable(Exception):
    """The fast path cannot run here: disabled, or the runner predates the route."""


def enabled(value=None):
    raw = os.environ.get(ENABLED_VAR, "") if value is None else value
    return str(raw).strip().lower() in ("1", "true", "on", "yes")


def stream_settings(flag=None):
    """Session settings for the preview stream, or nothing when the path is off."""
    return dict(STREAM_SETTINGS) if enabled(flag) else {}


def gesture_viewport(phone, flag=None):
    """Geometry for a gesture. A probe must keep asking the phone itself."""
    if enabled(flag):
        return phone.viewport(VIEWPORT_MAX_AGE)
    return phone.viewport()


def _missing_route(error):
    """True when the runner answered 404 for a route it does not implement."""
    return (getattr(error, "code", None) == "http_error"
            and "404" in str(error))


def tap(phone, x, y, flag=None):
    """Send a tap on the immediate route; raise Unavailable to fall back."""
    if not enabled(flag):
        raise Unavailable("fast preview is disabled")
    try:
        phone.post(TAP_ROUTE, {"x": x, "y": y})
    except Exception as error:
        if _missing_route(error):
            raise Unavailable("this runner has no " + TAP_ROUTE) from error
        raise


def drag(phone, x, y, to_x, to_y, duration_ms, flag=None):
    """Send a drag on the immediate route; raise Unavailable to fall back."""
    if not enabled(flag):
        raise Unavailable("fast preview is disabled")
    seconds = max(0.05, min(10.0, float(duration_ms) / 1000.0))
    try:
        phone.post(DRAG_ROUTE, {"fromX": x, "fromY": y, "toX": to_x, "toY": to_y,
                                "duration": seconds})
    except Exception as error:
        if _missing_route(error):
            raise Unavailable("this runner has no " + DRAG_ROUTE) from error
        raise