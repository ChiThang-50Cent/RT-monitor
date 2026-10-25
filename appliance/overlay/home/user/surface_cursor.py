import ctypes as C
import os
from pathlib import Path
import queue
import re
import select
import socket
import sys
import tempfile
import threading

from surface_protocol import WIDTH, HEIGHT, CursorReader, CursorState


def bind(lib, name, args, result=C.c_int):
    fn = getattr(lib, name)
    fn.argtypes = args
    fn.restype = result
    return fn


class XImage(C.Structure):
    _fields_ = [('width', C.c_int), ('height', C.c_int), ('xoffset', C.c_int),
                ('format', C.c_int), ('data', C.c_void_p), ('byte_order', C.c_int),
                ('bitmap_unit', C.c_int), ('bitmap_bit_order', C.c_int),
                ('bitmap_pad', C.c_int), ('depth', C.c_int), ('bytes_per_line', C.c_int),
                ('bits_per_pixel', C.c_int), ('red_mask', C.c_ulong),
                ('green_mask', C.c_ulong), ('blue_mask', C.c_ulong)]


class XColor(C.Structure):
    _fields_ = [('pixel', C.c_ulong), ('red', C.c_ushort), ('green', C.c_ushort),
                ('blue', C.c_ushort), ('flags', C.c_char), ('pad', C.c_char)]


class CursorRenderer:
    def __init__(self, presenter):
        self.window = presenter.window
        self.key = presenter.color_key
        self.requests = queue.Queue(maxsize=1)
        self.started = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name='cursor-render')
        self.thread.start()
        self.started.wait()
        if self.error:
            self.thread.join()
            raise self.error

    def apply(self, state):
        if self.error:
            raise self.error
        done = threading.Event()
        self.requests.put((state, done))
        while not done.wait(0.1):
            if self.error:
                raise self.error
        if self.error:
            raise self.error

    def close(self):
        if self.thread.is_alive():
            self.requests.put(None)
            self.thread.join()
        if self.error:
            raise self.error

    def _run(self):
        display = gc = pixmap = image = None
        try:
            x = C.CDLL('libX11.so.6')
            p, u, i = C.c_void_p, C.c_ulong, C.c_int
            signatures = {
                'XOpenDisplay': ([C.c_char_p], p),
                'XDefaultScreen': ([p], i), 'XDefaultVisual': ([p, i], p),
                'XDefaultDepth': ([p, i], i), 'XCloseDisplay': ([p], i),
                'XCreateGC': ([p, u, u, p], p), 'XFreeGC': ([p, p], i),
                'XSetForeground': ([p, p, u], i),
                'XFillRectangle': ([p, u, p, i, i, C.c_uint, C.c_uint], i),
                'XCreatePixmap': ([p, u, C.c_uint, C.c_uint, C.c_uint], u),
                'XFreePixmap': ([p, u], i),
                'XCopyArea': ([p, u, u, p, i, i, C.c_uint, C.c_uint, i, i], i),
                'XCreateImage': ([p, p, C.c_uint, i, i, p, C.c_uint, C.c_uint, i, i], C.POINTER(XImage)),
                'XPutImage': ([p, u, p, C.POINTER(XImage), i, i, i, i, C.c_uint, C.c_uint], i),
                'XDestroyImage': ([C.POINTER(XImage)], i),
                'XSelectInput': ([p, u, C.c_long], i), 'XPending': ([p], i),
                'XNextEvent': ([p, p], i), 'XSync': ([p, i], i),
                'XCreateBitmapFromData': ([p, u, C.c_char_p, C.c_uint, C.c_uint], u),
                'XCreatePixmapCursor': ([p, u, u, C.POINTER(XColor), C.POINTER(XColor), C.c_uint, C.c_uint], u),
                'XDefineCursor': ([p, u, u], i), 'XFreeCursor': ([p, u], i),
            }
            for name, (args, result) in signatures.items():
                bind(x, name, args, result)
            display = x.XOpenDisplay(None)
            if not display:
                raise RuntimeError('cursor XOpenDisplay failed')
            screen = x.XDefaultScreen(display)
            visual = x.XDefaultVisual(display, screen)
            depth = x.XDefaultDepth(display, screen)
            gc = x.XCreateGC(display, self.window, 0, None)
            if not gc:
                raise RuntimeError('cursor XCreateGC failed')
            bitmap = x.XCreateBitmapFromData(display, self.window, b'\0', 1, 1)
            if not bitmap:
                raise RuntimeError('transparent pointer bitmap failed')
            color = XColor()
            cursor = x.XCreatePixmapCursor(display, bitmap, bitmap, C.byref(color), C.byref(color), 0, 0)
            x.XFreePixmap(display, bitmap)
            if not cursor:
                raise RuntimeError('transparent pointer creation failed')
            x.XDefineCursor(display, self.window, cursor)
            x.XFreeCursor(display, cursor)
            x.XSelectInput(display, self.window, 1 << 15)
            x.XSetForeground(display, gc, self.key)
            x.XSync(display, 0)
            libc = C.CDLL(None)
            malloc = bind(libc, 'malloc', [C.c_size_t], p)
            state, shape, old_rect = CursorState(), None, None
            event = (C.c_long * 24)()
            self.started.set()
            while True:
                exposed = False
                while x.XPending(display):
                    x.XNextEvent(display, C.byref(event))
                    exposed |= C.cast(C.byref(event), C.POINTER(i))[0] == 12
                try:
                    request = self.requests.get(timeout=0.02)
                except queue.Empty:
                    if not exposed:
                        continue
                    request = (state, None)
                if request is None:
                    request = (CursorState(), None)
                    closing = True
                else:
                    closing = False
                state, done = request
                if old_rect:
                    x.XFillRectangle(display, self.window, gc, *old_rect)
                    old_rect = None
                if exposed:
                    x.XFillRectangle(display, self.window, gc, 0, 0, WIDTH, HEIGHT)
                if state.shape is not None:
                    if state.shape is not shape:
                        if pixmap:
                            x.XFreePixmap(display, pixmap)
                            pixmap = None
                        shape = state.shape
                        image = x.XCreateImage(display, visual, depth, 2, 0, None,
                                               shape.width, shape.height, 32, 0)
                        if not image:
                            raise RuntimeError('cursor XCreateImage failed')
                        info = image.contents
                        if info.bits_per_pixel not in (16, 24, 32):
                            raise RuntimeError('unsupported cursor X image pixel format')
                        size = info.bytes_per_line * shape.height
                        info.data = malloc(size)
                        if not info.data:
                            raise MemoryError('cursor image allocation failed')
                        C.memset(info.data, 0, size)
                        pixels = (C.c_ubyte * size).from_address(info.data)
                        masks = (info.red_mask, info.green_mask, info.blue_mask)
                        shifts = tuple((mask & -mask).bit_length() - 1 for mask in masks)
                        scales = tuple(mask >> shift for mask, shift in zip(masks, shifts))
                        pixel_size = info.bits_per_pixel // 8
                        order = 'little' if info.byte_order == 0 else 'big'
                        for row in range(shape.height):
                            for col in range(shape.width):
                                source = (row * shape.width + col) * 4
                                blue, green, red, alpha = shape.pixels[source:source + 4]
                                pixel = self.key
                                if alpha >= 128:
                                    rgb = tuple(min(255, (value * 255 + alpha // 2) // alpha)
                                                for value in (red, green, blue))
                                    pixel = sum(((value * scale + 127) // 255) << shift
                                                for value, scale, shift in zip(rgb, scales, shifts))
                                    if pixel == self.key:
                                        pixel ^= masks[2] & -masks[2]
                                offset = row * info.bytes_per_line + col * pixel_size
                                pixels[offset:offset + pixel_size] = pixel.to_bytes(pixel_size, order)
                        pixmap = x.XCreatePixmap(display, self.window, shape.width, shape.height, depth)
                        if not pixmap:
                            raise RuntimeError('cursor XCreatePixmap failed')
                        x.XPutImage(display, pixmap, gc, image, 0, 0, 0, 0, shape.width, shape.height)
                        x.XDestroyImage(image)
                        image = None
                    left = state.x - shape.hotspot_x
                    top = state.y - shape.hotspot_y
                    x0, y0 = max(0, left), max(0, top)
                    x1, y1 = min(WIDTH, left + shape.width), min(HEIGHT, top + shape.height)
                    if x0 < x1 and y0 < y1:
                        old_rect = (x0, y0, x1 - x0, y1 - y0)
                        x.XCopyArea(display, pixmap, self.window, gc, x0 - left, y0 - top,
                                    x1 - x0, y1 - y0, x0, y0)
                x.XSync(display, 0)
                if done:
                    done.set()
                if closing:
                    break
        except Exception as error:
            self.error = error
        finally:
            self.started.set()
            if display:
                if gc:
                    x.XSetForeground(display, gc, self.key)
                    x.XFillRectangle(display, self.window, gc, 0, 0, WIDTH, HEIGHT)
                    x.XSync(display, 0)
                if image:
                    x.XDestroyImage(image)
                if pixmap:
                    x.XFreePixmap(display, pixmap)
                if gc:
                    x.XFreeGC(display, gc)
                x.XCloseDisplay(display)


class CursorServer:
    def __init__(self, presenter):
        self.renderer = None
        self.listener = self.active = None
        self.directory = None
        self.thread = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        try:
            self.renderer = CursorRenderer(presenter)
            runtime = os.environ.get('XDG_RUNTIME_DIR', '/run/user/10000')
            self.directory = Path(tempfile.mkdtemp(prefix='surface-cursor-', dir=runtime))
            os.chmod(self.directory, 0o700)
            self.name = self.directory.name
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(self.directory / 'stream.sock'))
            self.listener.listen(1)
            self.thread = threading.Thread(target=self._run, name='cursor-stream')
            self.thread.start()
        except Exception:
            self.close()
            raise

    def _run(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except OSError:
                break
            with self.lock:
                if self.stopped.is_set():
                    connection.close()
                    break
                self.active = connection
            try:
                with connection, connection.makefile('rb') as stream:
                    reader = CursorReader(stream)
                    while not self.stopped.is_set():
                        state = reader.read()
                        if state is None:
                            break
                        self.renderer.apply(state)
                        connection.sendall(b'ok\n')
            except (OSError, ValueError, EOFError, RuntimeError) as error:
                if not self.stopped.is_set():
                    print(f'cursor stream: {error}', file=sys.stderr, flush=True)
            finally:
                with self.lock:
                    self.active = None
                try:
                    self.renderer.apply(CursorState())
                except Exception as error:
                    print(f'cursor hide: {error}', file=sys.stderr, flush=True)
                    break

    def close(self):
        self.stopped.set()
        with self.lock:
            for connection in (self.active, self.listener):
                if connection is not None:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            if self.listener is not None:
                self.listener.close()
        if self.thread is not None:
            self.thread.join()
        try:
            if self.renderer is not None:
                self.renderer.close()
        finally:
            if self.directory is not None:
                (self.directory / 'stream.sock').unlink(missing_ok=True)
                self.directory.rmdir()
                self.directory = None


def cursor_channel(name):
    if not re.fullmatch(r'surface-cursor-[A-Za-z0-9_-]+', name):
        raise ValueError('invalid cursor channel name')
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/10000'))
    directory = runtime / name
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise ValueError('cursor channel directory is not owned by this user')
    if directory.stat().st_mode & 0o777 != 0o700:
        raise ValueError('cursor channel directory is not private')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(directory / 'stream.sock'))
        while True:
            readable, _, _ = select.select([0, connection], [], [])
            if connection in readable:
                data = connection.recv(4096)
                if not data:
                    return 0
                view = memoryview(data)
                while view:
                    view = view[os.write(1, view):]
            if 0 in readable:
                data = os.read(0, 65536)
                if not data:
                    connection.shutdown(socket.SHUT_WR)
                    while True:
                        data = connection.recv(4096)
                        if not data:
                            return 0
                        view = memoryview(data)
                        while view:
                            view = view[os.write(1, view):]
                connection.sendall(data)
