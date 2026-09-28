"""Unix-domain JSONL transport for the Harness Gateway."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from pathlib import Path

from .server import HostRuntimePort, JsonlHost

LOGGER = logging.getLogger(__name__)
MAX_CONNECTIONS = 16


class UnixJsonlGateway:
    """Serve one JSONL Host Protocol stream at a time over a Unix socket.

    ``JsonlHost`` remains the protocol and session authority.  The listener is
    deliberately a transport shell so the Gateway does not grow a second
    request dispatcher or runtime registry.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        runtime: HostRuntimePort,
    ) -> None:
        self.socket_path = Path(socket_path)
        if not self.socket_path.is_absolute():
            raise ValueError("socket_path must be absolute")
        self._runtime = runtime
        self._listener: socket.socket | None = None
        self._stopping = threading.Event()
        self._connections: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()
        self._lock = threading.RLock()
        self._socket_inode: int | None = None

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def serve(self) -> int:
        self._bind()
        try:
            if self.stopping:
                return 0
            assert self._listener is not None
            self._listener.settimeout(0.5)
            while not self.stopping:
                try:
                    connection, _ = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stopping:
                        break
                    raise
                with self._lock:
                    if len(self._connections) >= MAX_CONNECTIONS:
                        connection.close()
                        continue
                    self._connections.add(connection)
                    worker = threading.Thread(target=self._serve_connection, args=(connection,), daemon=True)
                    self._workers.add(worker)
                worker.start()
            return 0
        finally:
            self.close()

    def close(self) -> None:
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        with self._lock:
            connections = tuple(self._connections)
            workers = tuple(self._workers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline = time.monotonic() + 1
        for worker in workers:
            if worker is not threading.current_thread() and worker.is_alive():
                worker.join(timeout=max(0, deadline - time.monotonic()))
        try:
            if self._socket_inode is not None and self.socket_path.stat().st_ino == self._socket_inode:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOGGER.warning("failed to remove Gateway socket %s", self.socket_path)

    def _bind(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise RuntimeError(f"Gateway socket path already exists: {self.socket_path}")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            listener.bind(str(self.socket_path))
            bound = True
            os.chmod(self.socket_path, 0o600)
            listener.listen(MAX_CONNECTIONS)
            self._socket_inode = self.socket_path.stat().st_ino
            if self.stopping:
                listener.close()
                self.socket_path.unlink(missing_ok=True)
                return
        except Exception:
            listener.close()
            if bound:
                try:
                    self.socket_path.unlink()
                except FileNotFoundError:
                    pass
            raise
        self._listener = listener

    def _serve_connection(self, connection: socket.socket) -> None:
        reader = connection.makefile("r", encoding="utf-8", newline="")
        writer = connection.makefile("w", encoding="utf-8", newline="")
        try:
            host = JsonlHost(reader, writer, runtime=self._runtime)
            host.serve()
            if host.stopping:
                self._stopping.set()
        finally:
            reader.close()
            writer.close()
            connection.close()
            with self._lock:
                self._connections.discard(connection)
                self._workers.discard(threading.current_thread())
