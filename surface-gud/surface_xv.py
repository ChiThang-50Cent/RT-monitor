"""Tegra DMA-BUF Xv passthrough V1; blocking commits, no pixel mapping."""
import ctypes as C
import os
import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
gi.require_version('GstAllocators', '1.0')
from gi.repository import Gst, GstVideo, GstAllocators

from surface_protocol import WIDTH, HEIGHT
from surface_cursor import CursorServer

class WindowAttributes(C.Structure):
    _fields_ = [('background_pixmap', C.c_ulong), ('background_pixel', C.c_ulong),
                ('border_pixmap', C.c_ulong), ('border_pixel', C.c_ulong),
                ('bit_gravity', C.c_int), ('win_gravity', C.c_int), ('backing_store', C.c_int),
                ('backing_planes', C.c_ulong), ('backing_pixel', C.c_ulong),
                ('save_under', C.c_int), ('event_mask', C.c_long),
                ('do_not_propagate_mask', C.c_long), ('override_redirect', C.c_int),
                ('colormap', C.c_ulong), ('cursor', C.c_ulong)]

class XvImage(C.Structure):
    _fields_ = [('id', C.c_int), ('width', C.c_int), ('height', C.c_int),
                ('data_size', C.c_int), ('num_planes', C.c_int),
                ('pitches', C.POINTER(C.c_int)), ('offsets', C.POINTER(C.c_int)),
                ('data', C.c_void_p), ('obdata', C.c_void_p)]

class Cookie(C.Structure):
    _fields_ = [('sequence', C.c_uint)]

class AuthReply(C.Structure):
    _fields_ = [('response_type', C.c_uint8), ('pad', C.c_uint8),
                ('sequence', C.c_uint16), ('length', C.c_uint32), ('authenticated', C.c_uint32)]

class Flink(C.Structure):
    _fields_ = [('handle', C.c_uint32), ('name', C.c_uint32)]

class VBlankRequest(C.Structure):
    _fields_ = [('type', C.c_uint), ('sequence', C.c_uint), ('signal', C.c_ulong)]

class VBlankReply(C.Structure):
    _fields_ = [('type', C.c_uint), ('sequence', C.c_uint),
                ('seconds', C.c_long), ('microseconds', C.c_long)]

class VBlank(C.Union):
    _fields_ = [('request', VBlankRequest), ('reply', VBlankReply)]

# The Surface RT LVDS panel is driven by CRTC 1 (DRM_VBLANK_SECONDARY).
VBLANK_SECONDARY = 0x20000000


def bind(lib, name, args, result=C.c_int):
    fn = getattr(lib, name)
    fn.argtypes = args
    fn.restype = result
    return fn


class XvPresenter:
    def __init__(self):
        self.display = None
        self.gpu = -1
        self.window = 0
        self.gc = None
        self.port = None
        self.image = None
        self.previous = None
        self.cursor_server = None
        self.color_key = 0x080808
        self.x = C.CDLL('libX11.so.6')
        self.xv = C.CDLL('libXv.so.1')
        self.drm = C.CDLL('libdrm.so.2', use_errno=True)
        self.libc = C.CDLL(None)
        self.header = (C.c_uint32 * 9)()
        self.names = {}
        self.geometry = None
        p, u = C.c_void_p, C.c_ulong
        bind(self.x, 'XInitThreads', [])()
        bind(self.x, 'XOpenDisplay', [C.c_char_p], p)
        bind(self.x, 'XDefaultRootWindow', [p], u)
        bind(self.x, 'XInternAtom', [p, C.c_char_p, C.c_int], u)
        bind(self.x, 'XCreateSimpleWindow', [p, u, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_uint, u, u], u)
        bind(self.x, 'XChangeWindowAttributes', [p, u, u, C.POINTER(WindowAttributes)])
        bind(self.x, 'XStoreName', [p, u, C.c_char_p])
        bind(self.x, 'XMapRaised', [p, u])
        bind(self.x, 'XRaiseWindow', [p, u])
        bind(self.x, 'XCreateGC', [p, u, u, p], p)
        bind(self.x, 'XSync', [p, C.c_int])
        bind(self.x, 'XFree', [p])
        bind(self.x, 'XFreeGC', [p, p])
        bind(self.x, 'XDestroyWindow', [p, u])
        bind(self.x, 'XCloseDisplay', [p])
        bind(self.xv, 'XvQueryAdaptors', [p, u, C.POINTER(C.c_uint), C.POINTER(p)])
        bind(self.xv, 'XvFreeAdaptorInfo', [p])
        bind(self.xv, 'XvGrabPort', [p, u, u])
        bind(self.xv, 'XvUngrabPort', [p, u, u])
        bind(self.xv, 'XvStopVideo', [p, u, u])
        bind(self.xv, 'XvCreateImage', [p, u, C.c_int, p, C.c_int, C.c_int], C.POINTER(XvImage))
        bind(self.xv, 'XvPutImage', [p, u, u, p, C.POINTER(XvImage), C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_int, C.c_int, C.c_uint, C.c_uint])
        bind(self.xv, 'XvGetPortAttribute', [p, u, u, C.POINTER(C.c_int)])
        bind(self.xv, 'XvSetPortAttribute', [p, u, u, C.c_int])
        bind(self.drm, 'drmOpen', [C.c_char_p, C.c_char_p])
        bind(self.drm, 'drmGetMagic', [C.c_int, C.POINTER(C.c_uint32)])
        bind(self.drm, 'drmPrimeFDToHandle', [C.c_int, C.c_int, C.POINTER(C.c_uint32)])
        bind(self.drm, 'drmIoctl', [C.c_int, C.c_ulong, p])
        bind(self.drm, 'drmWaitVBlank', [C.c_int, C.POINTER(VBlank)])
        bind(self.libc, 'free', [p], None)
        self.display = self.x.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError('XOpenDisplay failed')
        self.root = self.x.XDefaultRootWindow(self.display)
        self.info_atom = self.x.XInternAtom(self.display, b'XV_TEGRA_VDPAU_INFO', 0)
        self.visible_frames = 0
        self.gpu = self.drm.drmOpen(b'tegra', None)
        if self.gpu < 0:
            raise OSError(C.get_errno(), 'drmOpen(tegra)')
        x11xcb = C.CDLL('libX11-xcb.so.1')
        xdri = C.CDLL('libxcb-dri2.so.0')
        connection = bind(x11xcb, 'XGetXCBConnection', [p], p)(self.display)
        auth = bind(xdri, 'xcb_dri2_authenticate', [p, C.c_uint32, C.c_uint32], Cookie)
        reply_fn = bind(xdri, 'xcb_dri2_authenticate_reply', [p, Cookie, p], C.POINTER(AuthReply))
        magic = C.c_uint32()
        if self.drm.drmGetMagic(self.gpu, C.byref(magic)):
            raise OSError(C.get_errno(), 'drmGetMagic')
        reply = reply_fn(connection, auth(connection, self.root, magic.value), None)
        authenticated = bool(reply and reply.contents.authenticated)
        if reply:
            self.libc.free(reply)
        if not authenticated:
            raise RuntimeError('DRI2 authentication failed')
        count, adaptors = C.c_uint(), p()
        if self.xv.XvQueryAdaptors(self.display, self.root, C.byref(count), C.byref(adaptors)) or not count.value:
            raise RuntimeError('No Xv adaptors')
        # Only the ABI's first field is needed: first adaptor's base_id.
        port = C.cast(adaptors, C.POINTER(u))[0]
        self.xv.XvFreeAdaptorInfo(adaptors)
        if self.xv.XvGrabPort(self.display, port, 0):
            raise RuntimeError('Could not grab first Xv adaptor port')
        self.port = port
        color_atom = self.x.XInternAtom(self.display, b'XV_COLORKEY', 0)
        if self.xv.XvSetPortAttribute(self.display, self.port, color_atom, self.color_key):
            raise RuntimeError('Could not enable Xv colorkey')
        self.window = self.x.XCreateSimpleWindow(
            self.display, self.root, 0, 0, WIDTH, HEIGHT, 0, self.color_key, self.color_key)
        if not self.window:
            raise RuntimeError('XCreateSimpleWindow failed')
        self.x.XStoreName(self.display, self.window, b'Surface PC Display')
        attrs = WindowAttributes()
        attrs.override_redirect = 1
        self.x.XChangeWindowAttributes(self.display, self.window, 1 << 9, C.byref(attrs))
        self.gc = self.x.XCreateGC(self.display, self.window, 0, None)
        if not self.gc:
            raise RuntimeError('XCreateGC failed')
        self.x.XMapRaised(self.display, self.window)
        self.x.XSync(self.display, 0)
        self.cursor_server = CursorServer(self)
        self.cursor_socket = self.cursor_server.name

    def present(self, sample):
        b = sample.get_buffer()
        meta = GstVideo.buffer_get_video_meta(b)
        caps = sample.get_caps().get_structure(0)
        visible = (caps.get_value('width'), caps.get_value('height'))
        if not meta or meta.n_planes != 3:
            raise RuntimeError('Expected three-plane GstVideoMeta')
        geometry = (visible[0], visible[1], meta.width, meta.height)
        if visible != (WIDTH, HEIGHT):
            raise RuntimeError('expected native visible geometry 1366x768')
        if self.geometry is None:
            self.geometry = geometry
            # Existing passthrough V1 FOURCC, deliberately synchronous commits.
            fourcc = ord('Y') | (ord('V') << 8) | (ord('2') << 16) | (ord('1') << 24)
            self.image = self.xv.XvCreateImage(self.display, self.port, fourcc, None, meta.width, meta.height)
            if not self.image or self.image.contents.data_size != C.sizeof(self.header):
                raise RuntimeError('Xv passthrough V1 header not supported')
            self.image.contents.data = C.addressof(self.header)
            self.x.XSync(self.display, 0)
        elif geometry != self.geometry:
            raise RuntimeError('Unexpected geometry transition')
        for plane in range(3):
            found, index, length, skip = b.find_memory(meta.offset[plane], 1)
            if not found or length != 1:
                raise RuntimeError('Invalid video plane memory mapping')
            mem = b.peek_memory(index)
            if not GstAllocators.is_dmabuf_memory(mem):
                raise RuntimeError('Decoder output is not DMA-BUF')
            handle = C.c_uint32()
            if self.drm.drmPrimeFDToHandle(self.gpu, GstAllocators.dmabuf_memory_get_fd(mem), C.byref(handle)):
                raise OSError(C.get_errno(), 'drmPrimeFDToHandle')
            if handle.value not in self.names:
                if len(self.names) >= 64:
                    raise RuntimeError('Decoder GEM pool exceeded 64 objects')
                link = Flink(handle.value, 0)
                if self.drm.drmIoctl(self.gpu, 0xc008640a, C.byref(link)):
                    raise OSError(C.get_errno(), 'GEM_FLINK')
                self.names[handle.value] = link.name
            self.header[plane] = self.names[handle.value]
            self.header[3 + plane] = meta.stride[plane]
            self.header[6 + plane] = mem.get_sizes()[1] + skip
        self.x.XRaiseWindow(self.display, self.window)
        status = self.xv.XvPutImage(self.display, self.port, self.window, self.gc, self.image,
                                   0, 0, *visible, 0, 0, *visible)
        self.x.XSync(self.display, 0)
        if status:
            raise RuntimeError('XvPutImage status %s' % status)
        info = C.c_int()
        if self.xv.XvGetPortAttribute(self.display, self.port, self.info_atom, C.byref(info)):
            raise RuntimeError('Could not query overlay visibility')
        if not info.value & 1:
            raise RuntimeError('Xorg reports overlay is not visible')
        self.visible_frames += 1
        # V1's blocking atomic commit has replaced the old scanout buffer now.
        self.previous = sample

    def wait_vblank(self, target=None):
        """Return the panel vblank count, first blocking until `target` if given."""
        event = VBlank()
        # Relative 0 only queries; absolute blocks until the count is reached.
        event.request.type = VBLANK_SECONDARY | (1 if target is None else 0)
        event.request.sequence = 0 if target is None else target & 0xFFFFFFFF
        if self.drm.drmWaitVBlank(self.gpu, C.byref(event)):
            raise OSError(C.get_errno(), 'drmWaitVBlank')
        return event.reply.sequence

    def close(self):
        cursor_error = None
        if self.cursor_server is not None:
            try:
                self.cursor_server.close()
            except Exception as error:
                cursor_error = error
            self.cursor_server = None
        if self.display:
            if self.window and self.port is not None:
                self.xv.XvStopVideo(self.display, self.port, self.window)
                self.x.XSync(self.display, 0)
            self.previous = None
            if self.image:
                self.image.contents.data = None
                self.x.XFree(self.image)
            if self.gc:
                self.x.XFreeGC(self.display, self.gc)
            if self.window:
                self.x.XDestroyWindow(self.display, self.window)
            if self.port is not None:
                self.xv.XvUngrabPort(self.display, self.port, 0)
            self.x.XCloseDisplay(self.display)
            self.display = None
        if self.gpu >= 0:
            os.close(self.gpu)
            self.gpu = -1
        if cursor_error is not None:
            raise cursor_error
