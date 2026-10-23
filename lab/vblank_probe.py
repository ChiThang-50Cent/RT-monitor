#!/usr/bin/python3
"""DRI2-authenticated vblank timestamp sampling; no master, modeset or plane writes."""
import ctypes as C
import json
import os
import statistics
import time
from surface_xv import bind, Cookie, AuthReply

class Request(C.Structure):
    _fields_ = [('type', C.c_uint), ('sequence', C.c_uint), ('signal', C.c_ulong)]
class Reply(C.Structure):
    _fields_ = [('type', C.c_uint), ('sequence', C.c_uint),
                ('seconds', C.c_long), ('microseconds', C.c_long)]
class VBlank(C.Union):
    _fields_ = [('request', Request), ('reply', Reply)]

x = C.CDLL('libX11.so.6')
drm = C.CDLL('libdrm.so.2', use_errno=True)
x11xcb = C.CDLL('libX11-xcb.so.1')
xdri = C.CDLL('libxcb-dri2.so.0')
libc = C.CDLL(None)
p = C.c_void_p
bind(x, 'XOpenDisplay', [C.c_char_p], p)
bind(x, 'XDefaultRootWindow', [p], C.c_ulong)
bind(x, 'XCloseDisplay', [p])
bind(drm, 'drmOpen', [C.c_char_p, C.c_char_p])
bind(drm, 'drmGetMagic', [C.c_int, C.POINTER(C.c_uint32)])
bind(drm, 'drmGetCap', [C.c_int, C.c_uint64, C.POINTER(C.c_uint64)])
bind(drm, 'drmWaitVBlank', [C.c_int, C.POINTER(VBlank)])
bind(libc, 'free', [p], None)
display = None
fd = -1
try:
    display = x.XOpenDisplay(None)
    if not display:
        raise RuntimeError('XOpenDisplay failed')
    root = x.XDefaultRootWindow(display)
    fd = drm.drmOpen(b'tegra', None)
    if fd < 0:
        raise OSError(C.get_errno(), 'drmOpen')
    magic = C.c_uint32()
    if drm.drmGetMagic(fd, C.byref(magic)):
        raise OSError(C.get_errno(), 'drmGetMagic')
    connection = bind(x11xcb, 'XGetXCBConnection', [p], p)(display)
    auth = bind(xdri, 'xcb_dri2_authenticate', [p, C.c_uint32, C.c_uint32], Cookie)
    get_reply = bind(xdri, 'xcb_dri2_authenticate_reply', [p, Cookie, p], C.POINTER(AuthReply))
    reply = get_reply(connection, auth(connection, root, magic.value), None)
    ok = bool(reply and reply.contents.authenticated)
    if reply:
        libc.free(reply)
    if not ok:
        raise RuntimeError('DRI2 authentication failed')
    monotonic = C.c_uint64()
    if drm.drmGetCap(fd, 6, C.byref(monotonic)):
        raise OSError(C.get_errno(), 'drmGetCap TIMESTAMP_MONOTONIC')
    samples = []
    start = time.monotonic()
    for i in range(181):
        event = VBlank()
        # xrandr reports the active LVDS output on CRTC 1.
        event.request.type = 1 | 0x20000000
        event.request.sequence = 1
        if drm.drmWaitVBlank(fd, C.byref(event)):
            raise OSError(C.get_errno(), 'drmWaitVBlank')
        samples.append([event.reply.sequence,
                        event.reply.seconds + event.reply.microseconds / 1e6])
    periods = [(b[1]-a[1])*1000/(b[0]-a[0])
               for a,b in zip(samples, samples[1:])
               if a[1] > 0 and b[1] > a[1] and b[0] > a[0]]
    valid = [s for s in samples if s[1] > 0]
    if len(valid) < 2 or not periods:
        raise RuntimeError('Insufficient valid vblank timestamps')
    print(json.dumps({'timestamp_monotonic': monotonic.value,
                      'wall_seconds': time.monotonic()-start,
                      'invalid_timestamp_count': len(samples)-len(valid),
                      'samples': samples, 'period_ms_median': statistics.median(periods),
                      'period_ms_min': min(periods), 'period_ms_max': max(periods),
                      'refresh_hz': (valid[-1][0]-valid[0][0])/(valid[-1][1]-valid[0][1])}), flush=True)
finally:
    if fd >= 0:
        os.close(fd)
    if display:
        x.XCloseDisplay(display)
