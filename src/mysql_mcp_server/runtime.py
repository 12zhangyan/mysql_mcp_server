"""Reusable SSH tunnels, connection pools, and cooperative query cancellation."""

from __future__ import annotations

import atexit
import hashlib
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from mysql.connector import connect
from mysql.connector.pooling import MySQLConnectionPool

from .config import ConnectionProfile


@dataclass(frozen=True)
class TunnelEndpoint:
    host: str
    port: int
    tunneled: bool = False


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
    ) -> None:
        self._popen_factory = popen_factory
        self._port_allocator = port_allocator
        self._readiness_check = readiness_check
        self._lock = threading.RLock()
        self._tunnels: dict[str, _ManagedTunnel] = {}

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
            return TunnelEndpoint(profile.host, profile.port, False)

        self._validate(profile)
        fingerprint = self._fingerprint(profile)
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
                self._stop(existing)

            managed = self._start(profile, fingerprint)
            self._tunnels[profile.name] = managed
            return managed.endpoint

    def _start(self, profile: ConnectionProfile, fingerprint: str) -> _ManagedTunnel:
        ssh = profile.ssh
        local_port = ssh.local_port or self._port_allocator()
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
            if process.poll() is not None:
                raise RuntimeError(
                    f"SSH tunnel for '{profile.name}' exited early with status "
                    f"{process.poll()}"
                )
            if self._readiness_check("127.0.0.1", local_port):
                return _ManagedTunnel(
                    process=process,
                    endpoint=TunnelEndpoint("127.0.0.1", local_port, True),
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
            for managed in self._tunnels.values():
                self._stop(managed)
            self._tunnels.clear()


class ConnectionPoolManager:
    """Cache classic Connector/Python pools by effective connection config."""

    def __init__(self, pool_factory: Callable[..., Any] = MySQLConnectionPool) -> None:
        self._pool_factory = pool_factory
        self._lock = threading.RLock()
        self._pools: dict[str, Any] = {}

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
        value = (profile.name, endpoint.host, endpoint.port, redacted)
        return hashlib.sha256(repr(value).encode()).hexdigest()

    def get_connection(
        self,
        profile: ConnectionProfile,
        endpoint: TunnelEndpoint,
        config: dict[str, Any],
        *,
        connect_factory: Callable[..., Any] = connect,
    ) -> Any:
        if profile.pool_size == 0:
            return connect_factory(**config)

        key = self._key(profile, endpoint, config)
        with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                pool = self._pool_factory(
                    pool_name=f"mcp_{key[:20]}",
                    pool_size=profile.pool_size,
                    pool_reset_session=True,
                    **config,
                )
                self._pools[key] = pool
        return pool.get_connection()

    def clear(self) -> None:
        """Drop cached pool references; checked-out connections remain self-owned."""
        with self._lock:
            self._pools.clear()


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

    @staticmethod
    def _shutdown(connection: Any) -> None:
        try:
            connection.shutdown()
        except Exception:
            try:
                connection.close()
            except Exception:
                pass


ssh_tunnel_manager = SshTunnelManager()
connection_pool_manager = ConnectionPoolManager()


def close_runtime_resources() -> None:
    ssh_tunnel_manager.close_all()
    connection_pool_manager.clear()


atexit.register(close_runtime_resources)
