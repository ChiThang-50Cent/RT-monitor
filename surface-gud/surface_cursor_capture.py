import os
from pathlib import Path
import select
import subprocess
import threading

from surface_protocol import CursorReader, CursorState


class CursorCapture:
    def __init__(self, node):
        self.cv = threading.Condition()
        self.state = CursorState()
        self.revision = 0
        self.error = None
        self.closed = False
        self.process = None
        self.reader = None
        ready_read, ready_write = os.pipe()
        try:
            self.process = subprocess.Popen(
                [str(Path.home() / ".local/libexec/surface-cursor-capture"),
                 str(node), str(ready_write)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                pass_fds=(ready_write,))
            os.close(ready_write)
            ready_write = None
            self.reader = threading.Thread(target=self._read, name="cursor-capture")
            self.reader.start()
            if not select.select([ready_read], [], [], 10)[0]:
                raise TimeoutError("cursor capture streaming readiness timed out")
            if os.read(ready_read, 1) != b"R":
                raise OSError("cursor capture exited before streaming")
            self.check()
        except Exception:
            self.close()
            raise
        finally:
            os.close(ready_read)
            if ready_write is not None:
                os.close(ready_write)

    def _read(self):
        try:
            reader = CursorReader(self.process.stdout)
            while True:
                state = reader.read()
                if state is None:
                    raise OSError("cursor capture output closed")
                with self.cv:
                    previous = self.state
                    if (state.x != previous.x or state.y != previous.y or
                            state.shape is not previous.shape):
                        self.state = state
                        self.revision += 1
                        self.cv.notify_all()
        except Exception as error:
            with self.cv:
                if not self.closed:
                    self.error = str(error)
                self.cv.notify_all()

    def check(self):
        with self.cv:
            if self.error is not None:
                raise OSError(self.error)
            if self.closed:
                raise OSError("cursor capture closed")
            if self.process.poll() is not None:
                raise OSError("cursor capture exited")

    def snapshot(self, previous_revision):
        with self.cv:
            self.check()
            if self.revision == previous_revision:
                self.cv.wait(0.2)
                self.check()
            return self.revision, self.state

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()
        if self.process is not None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            if self.reader is not None:
                self.reader.join()
            self.process.stdout.close()
