"""Appliance receiver admission, held until presentation resources are closed."""
import os
from pathlib import Path
import select
import socket


class AppliancePresentation:
    def __init__(self, connection):
        self.connection = connection
        self.buffer = bytearray()
        self.was_revoked = False

    @classmethod
    def acquire(cls):
        path = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/10000')) / 'surface-power.sock'
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(2)
            connection.connect(str(path))
            connection.sendall(b'surface-power-v1 acquire\n')
            reply = bytearray()
            while b'\n' not in reply and len(reply) <= 64:
                data = connection.recv(65 - len(reply))
                if not data:
                    raise RuntimeError('appliance presentation controller disconnected')
                reply.extend(data)
            if reply != b'surface-power-v1 granted\n':
                raise RuntimeError('appliance presentation admission unavailable')
            connection.setblocking(False)
            return cls(connection)
        except Exception:
            connection.close()
            raise

    def revoked(self):
        if self.was_revoked or self.connection is None:
            return True
        try:
            while select.select([self.connection], [], [], 0)[0]:
                data = self.connection.recv(128)
                if not data:
                    self.was_revoked = True
                    break
                self.buffer.extend(data)
                if self.buffer == b'surface-power-v1 revoke\n' or len(self.buffer) > 64 or b'\n' in self.buffer:
                    self.was_revoked = True
                    break
        except OSError:
            self.was_revoked = True
        return self.was_revoked

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
