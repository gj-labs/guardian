"""Linux -> STM32 calls through the arduino-router MessagePack-RPC socket.

Same wire format as the track guide's msgpack_test.py, but with one persistent
connection, increasing message ids, a timeout and automatic reconnect.
The MCU sketch (mcu/guardian_mcu) provides `guardian_set`.
"""
from __future__ import annotations

import itertools
import logging
import socket
import threading

import msgpack

log = logging.getLogger("guardian.bridge")

ROUTER_SOCK = "/var/run/arduino-router.sock"


class RpcError(RuntimeError):
    pass


class RouterBridge:
    def __init__(self, path: str = ROUTER_SOCK, timeout: float = 2.0) -> None:
        self.path, self.timeout = path, timeout
        self._sock: socket.socket | None = None
        self._unpacker: msgpack.Unpacker | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.ok = False

    def _connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.path)
        self._sock = s
        self._unpacker = msgpack.Unpacker(raw=False, max_buffer_size=65536)

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self.ok = False

    def call(self, method: str, *args):
        with self._lock:
            try:
                if self._sock is None:
                    self._connect()
                msgid = next(self._ids) & 0x7FFFFFFF
                self._sock.sendall(msgpack.packb([0, msgid, method, list(args)], use_bin_type=True))
                while True:
                    data = self._sock.recv(4096)
                    if not data:
                        raise ConnectionError("router closed the connection")
                    self._unpacker.feed(data)
                    for msg in self._unpacker:
                        # Skip anything that isn't the reply to this call (e.g. notifications).
                        if isinstance(msg, list) and len(msg) == 4 and msg[0] == 1 and msg[1] == msgid:
                            if msg[2] is not None:
                                raise RpcError(f"{method} failed: {msg[2]!r}")
                            self.ok = True
                            return msg[3]
            except RpcError:
                self.ok = False
                raise
            except (OSError, ValueError, msgpack.UnpackException) as e:
                self._close()
                raise ConnectionError(f"bridge call {method} failed: {e}") from e


class MockBridge:
    """Used off the board: remembers the last call so tests and the dashboard can show it."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.ok = True

    def call(self, method: str, *args):
        self.calls.append((method, *args))
        del self.calls[:-50]
        return True


def make_bridge(kind: str):
    if kind == "mock":
        return MockBridge()
    return RouterBridge(kind if kind.startswith("/") else ROUTER_SOCK)
