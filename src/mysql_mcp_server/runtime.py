"""Reusable SSH tunnels, connection pools, and cooperative query cancellation."""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import os
import socket
import subprocess
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

import anyio
from mysql.connector import connect
from mysql.connector.pooling import MySQLConnectionPool

from .config import ConnectionProfile
from .credential_store import credential_cache
from .errors import QueryFailure


@dataclass(frozen=True)
class TunnelEndpoint:
    host: str
    port: int
    tunneled: bool = False
    generation: str = field(default="", repr=False, compare=False)


@dataclass
class _ManagedTunnel:
    process: Any
    endpoint: TunnelEndpoint
    fingerprint: str


def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.1):
            return True
    except OSError:
        return False


class SshTunnelManager:
    """Start one SSH process per profile and reuse it across tool calls."""

    def __init__(
        self,
        *,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        port_allocator: Callable[[], int] = _allocate_local_port,
        readiness_check: Callable[[str, int], bool] = _port_is_open,
        port_in_use: Callable[[str, int], bool] = _port_is_open,
    ) -> None:
        self._popen_factory = popen_factory
        self._port_allocator = port_allocator
        self._readiness_check = readiness_check
        self._port_in_use = port_in_use
        # Guards the two tables only; a tunnel start waits on its profile lock,
        # so one slow bastion cannot block other profiles.
        self._lock = threading.Lock()
        self._tunnels: dict[str, _ManagedTunnel] = {}
        self._profile_locks: dict[str, threading.Lock] = {}

    def _profile_lock(self, name: str) -> threading.Lock:
        with self._lock:
            return self._profile_locks.setdefault(name, threading.Lock())

    @staticmethod
    def _fingerprint(profile: ConnectionProfile) -> str:
        ssh = profile.ssh
        value = (
            ssh.host,
            ssh.port,
            ssh.user,
            ssh.key_path,
            ssh.remote_host,
            ssh.remote_port,
            ssh.local_port,
        )
        return hashlib.sha256(repr(value).encode()).hexdigest()

    @staticmethod
    def _validate(profile: ConnectionProfile) -> None:
        ssh = profile.ssh
        missing = [
            key
            for key, value in {
                "host": ssh.host,
                "user": ssh.user,
                "key_path": ssh.key_path,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(
                f"Connection '{profile.name}' SSH config is missing: "
                f"{', '.join(missing)}"
            )

    def endpoint(self, profile: ConnectionProfile) -> TunnelEndpoint:
        if not profile.ssh.enabled:
            with self._profile_lock(profile.name):
                with self._lock:
                    existing = self._tunnels.pop(profile.name, None)
                if existing is not None:
                    self._stop(existing)
            return TunnelEndpoint(profile.host, profile.port, False)

        self._validate(profile)
        fingerprint = self._fingerprint(profile)
        with self._profile_lock(profile.name):
            with self._lock:
                existing = self._tunnels.get(profile.name)
            if (
                existing is not None
                and existing.fingerprint == fingerprint
                and existing.process.poll() is None
                and self._readiness_check(
                    existing.endpoint.host, existing.endpoint.port
                )
            ):
                return existing.endpoint
            if existing is not None:
                with self._lock:
                    self._tunnels.pop(profile.name, None)
                self._stop(existing)

            managed = self._start(profile, fingerprint)
            with self._lock:
                self._tunnels[profile.name] = managed
            return managed.endpoint

    def _start(self, profile: ConnectionProfile, fingerprint: str) -> _ManagedTunnel:
        ssh = profile.ssh
        local_port = ssh.local_port or self._port_allocator()
        # Readiness only proves that something accepts connections on the
        # port, so a port that is already taken would pass it.
        if self._port_in_use("127.0.0.1", local_port):
            raise RuntimeError(
                f"Local port {local_port} for the SSH tunnel of '{profile.name}' "
                "is already in use; set ssh.local_port to a free port, or 0 to "
                "choose one automatically"
            )
        command = [
            "ssh",
            "-i",
            str(ssh.key_path),
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            f"{local_port}:{ssh.remote_host}:{ssh.remote_port}",
            f"{ssh.user}@{ssh.host}",
            "-p",
            str(ssh.port),
        ]
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        process = self._popen_factory(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + ssh.startup_timeout
        while time.monotonic() < deadline:
            ready = self._readiness_check("127.0.0.1", local_port)
            # Check the process after the port: another listener that took the
            # port answers before ssh, with ExitOnForwardFailure, exits.
            if process.poll() is not None:
                raise RuntimeError(
                    f"SSH tunnel for '{profile.name}' exited early with status "
                    f"{process.poll()}"
                )
            if ready:
                return _ManagedTunnel(
                    process=process,
                    endpoint=TunnelEndpoint(
                        "127.0.0.1", local_port, True, uuid.uuid4().hex
                    ),
                    fingerprint=fingerprint,
                )
            time.sleep(0.05)

        process.terminate()
        try:
            process.wait(timeout=2)
        except Exception:
            process.kill()
        raise TimeoutError(
            f"SSH tunnel for '{profile.name}' did not become ready within "
            f"{ssh.startup_timeout:g}s"
        )

    @staticmethod
    def _stop(managed: _ManagedTunnel) -> None:
        if managed.process.poll() is not None:
            return
        managed.process.terminate()
        try:
            managed.process.wait(timeout=2)
        except Exception:
            managed.process.kill()

    def close_all(self) -> None:
        with self._lock:
            names = list(self._profile_locks)
        for name in names:
            # Wait for a start in progress so its process is not left running.
            with self._profile_lock(name):
                with self._lock:
                    managed = self._tunnels.pop(name, None)
                if managed is not None:
                    self._stop(managed)


class ConnectionPoolManager:
    """Cache classic Connector/Python pools per profile and selected database.

    A profile keeps up to max_database_pools pools, one per selected database
    (no database is its own pool, since MySQL cannot unselect one), evicting
    the least recently used. Any other config change replaces all of them.
    """

    def __init__(self, pool_factory: Callable[..., Any] = MySQLConnectionPool) -> None:
        self._pool_factory = pool_factory
        self._lock = threading.RLock()
        self._pools: dict[str, Any] = {}
        # Profile name -> key of its config without the database.
        self._profile_keys: dict[str, str] = {}
        # Profile name -> its pool keys, least recently used first.
        self._profile_pools: dict[str, list[str]] = {}
        # Pools dropped while connections may still be borrowed from them.
        self._retired: weakref.WeakSet[Any] = weakref.WeakSet()

    @staticmethod
    def _dispose_pool(pool: Any) -> None:
        close = getattr(pool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
            return

        # Connector/Python does not expose a public pool shutdown API. Drain
        # and close its idle raw connections so profile reloads and credential
        # rotations do not retain server sessions indefinitely.
        queue = getattr(pool, "_cnx_queue", None)
        if queue is None:
            return
        while True:
            try:
                connection = queue.get_nowait()
            except Exception:
                break
            try:
                connection.close()
            except Exception:
                pass

    @staticmethod
    def _key(
        profile: ConnectionProfile,
        endpoint: TunnelEndpoint,
        config: dict[str, Any],
    ) -> str:
        redacted = {
            key: (
                hashlib.sha256(str(value).encode()).hexdigest()
                if key == "password"
                else value
            )
            for key, value in config.items()
        }
        value = (
            profile.name,
            profile.pool_size,
            endpoint.host,
            endpoint.port,
            endpoint.generation,
            redacted,
        )
        return hashlib.sha256(repr(value).encode()).hexdigest()

    def _retire(self, key: str) -> None:
        pool = self._pools.pop(key, None)
        if pool is not None:
            self._retired.add(pool)
            self._dispose_pool(pool)

    def _retire_profile(self, name: str) -> None:
        self._profile_keys.pop(name, None)
        for key in self._profile_pools.pop(name, []):
            self._retire(key)

    def get_connection(
        self,
        profile: ConnectionProfile,
        endpoint: TunnelEndpoint,
        config: dict[str, Any],
        *,
        connect_factory: Callable[..., Any] = connect,
    ) -> Any:
        if profile.pool_size == 0:
            with self._lock:
                self._retire_profile(profile.name)
            return connect_factory(**config)

        key = self._key(profile, endpoint, config)
        profile_key = self._key(
            profile,
            endpoint,
            {name: value for name, value in config.items() if name != "database"},
        )
        with self._lock:
            if self._profile_keys.get(profile.name) not in (None, profile_key):
                self._retire_profile(profile.name)
            self._profile_keys[profile.name] = profile_key
            keys = self._profile_pools.setdefault(profile.name, [])
            pool = self._pools.get(key)
            if pool is None:
                while len(keys) >= profile.max_database_pools:
                    self._retire(keys.pop(0))
                pool = self._pool_factory(
                    pool_name=f"mcp_{key[:20]}",
                    pool_size=profile.pool_size,
                    pool_reset_session=True,
                    **config,
                )
                self._pools[key] = pool
            else:
                keys.remove(key)
            keys.append(key)
        return pool.get_connection()

    def release(self, connection: Any) -> None:
        """Return a connection to its pool, closing it if the pool was retired."""
        connection.close()
        pool = getattr(connection, "_cnx_pool", None)
        if pool is None:
            return
        with self._lock:
            retired = pool in self._retired
        # A pool retired before or during close() got this connection back
        # after its idle connections were closed; close them again.
        if retired:
            self._dispose_pool(pool)

    def discard(
        self,
        profile: ConnectionProfile,
        endpoint: TunnelEndpoint,
        config: dict[str, Any],
    ) -> None:
        """Remove a pool whose effective connection failed a safety check."""
        key = self._key(profile, endpoint, config)
        with self._lock:
            keys = self._profile_pools.get(profile.name, [])
            if key in keys:
                keys.remove(key)
                if not keys:
                    self._profile_pools.pop(profile.name, None)
                    self._profile_keys.pop(profile.name, None)
            self._retire(key)

    def clear(self) -> None:
        """Close idle pool connections and drop cached references."""
        with self._lock:
            for name in list(self._profile_pools):
                self._retire_profile(name)
            for key in list(self._pools):
                self._retire(key)
            self._profile_keys.clear()


class QueryControl:
    """Cross-thread handle used to close a connector socket on cancellation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._connection: Any | None = None
        self._cancelled = False

    def bind(self, connection: Any) -> None:
        with self._lock:
            self._connection = connection
            if self._cancelled:
                self._shutdown(connection)
                raise RuntimeError("Query was cancelled before execution")

    def unbind(self) -> None:
        with self._lock:
            self._connection = None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._connection is not None:
                self._shutdown(self._connection)

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @staticmethod
    def _shutdown(connection: Any) -> None:
        try:
            connection.shutdown()
        except Exception:
            try:
                connection.close()
            except Exception:
                pass


# Safety net only: waiters are woken by release(); this bounds a missed wake-up.
_ADMISSION_WAKE_FALLBACK_SECONDS = 0.05


class QueryAdmission:
    """Bound work per profile; a cancelled worker retains its slot until cleanup."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active: dict[str, int] = {}
        self._waiting: dict[str, int] = {}
        self._waiters: dict[
            str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]
        ] = {}

    def release(self, key: str) -> None:
        """Free a slot from any thread and wake that profile's queued callers."""
        with self._condition:
            self._active[key] -= 1
            if not self._active[key]:
                del self._active[key]
            waiters = tuple(self._waiters.get(key, ()))
            self._condition.notify_all()
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # The waiter's event loop already closed; nothing to wake.
                pass

    async def acquire(self, profile: ConnectionProfile) -> QueryLease:
        """Queue before using AnyIO worker threads, so one busy profile cannot fill them."""
        key = profile.name
        capacity = min(
            profile.max_concurrent_queries,
            profile.pool_size or profile.max_concurrent_queries,
        )
        started = time.monotonic()
        queued = False
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # Non-asyncio backend: keep short polling.
        waiter = (loop, asyncio.Event()) if loop is not None else None
        try:
            while True:
                with self._condition:
                    if waiter is not None:
                        # Clear under the lock: a release after this check sets it.
                        waiter[1].clear()
                    elapsed = time.monotonic() - started
                    if queued and elapsed >= profile.queue_timeout_ms / 1000:
                        raise QueryFailure(
                            "CONNECTION_BUSY",
                            "The connection queue wait limit was reached.",
                            "Reduce parallel calls and retry after a short delay.",
                            retryable=True,
                            connection=key,
                            phase="queue",
                        )
                    if self._active.get(key, 0) < capacity:
                        self._active[key] = self._active.get(key, 0) + 1
                        return QueryLease(self, key, round(elapsed * 1000))
                    if not queued:
                        if self._waiting.get(key, 0) >= profile.max_queued_queries:
                            raise QueryFailure(
                                "CONNECTION_BUSY",
                                "The connection queue is full.",
                                "Reduce parallel calls and retry after a short delay.",
                                retryable=True,
                                connection=key,
                                phase="queue",
                            )
                        self._waiting[key] = self._waiting.get(key, 0) + 1
                        if waiter is not None:
                            self._waiters.setdefault(key, set()).add(waiter)
                        queued = True
                remaining = profile.queue_timeout_ms / 1000 - elapsed
                if waiter is None:
                    await anyio.sleep(min(0.01, max(remaining, 0)))
                    continue
                with anyio.move_on_after(
                    min(_ADMISSION_WAKE_FALLBACK_SECONDS, max(remaining, 0))
                ):
                    await waiter[1].wait()
        finally:
            if queued:
                with self._condition:
                    self._waiting[key] -= 1
                    if not self._waiting[key]:
                        del self._waiting[key]
                    if waiter is not None:
                        waiters = self._waiters.get(key)
                        if waiters is not None:
                            waiters.discard(waiter)
                            if not waiters:
                                del self._waiters[key]


class QueryLease:
    """Transfer an admission slot to a worker atomically with cancellation."""

    def __init__(self, admission: QueryAdmission, key: str, wait_ms: int):
        self.admission, self.key, self.wait_ms = admission, key, wait_ms
        self._lock = threading.Lock()
        self._state = "pending"

    def release_pending(self) -> None:
        with self._lock:
            if self._state == "pending":
                self._state = "released"
                self.admission.release(self.key)

    @contextmanager
    def run(self):
        with self._lock:
            if self._state != "pending":
                raise RuntimeError("Query was cancelled before worker startup")
            self._state = "running"
        try:
            yield self.wait_ms
        finally:
            with self._lock:
                self._state = "released"
                self.admission.release(self.key)


query_admission = QueryAdmission()
ssh_tunnel_manager = SshTunnelManager()
connection_pool_manager = ConnectionPoolManager()


def close_runtime_resources() -> None:
    connection_pool_manager.clear()
    ssh_tunnel_manager.close_all()
    credential_cache.clear()


atexit.register(close_runtime_resources)
