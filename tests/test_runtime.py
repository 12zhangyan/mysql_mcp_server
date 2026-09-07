from dataclasses import replace
from unittest.mock import MagicMock, patch

from mysql_mcp_server.config import ConnectionProfile, SshConfig
from mysql_mcp_server.runtime import (
    ConnectionPoolManager,
    QueryControl,
    SshTunnelManager,
    TunnelEndpoint,
    close_runtime_resources,
)


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stderr = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def ssh_profile(**overrides):
    ssh = SshConfig(
        enabled=True,
        host="bastion",
        user="reader",
        key_path="key",
        remote_host="mysql",
        local_port=0,
    )
    return ConnectionProfile(
        name="prod",
        host="mysql",
        port=3306,
        user="reader",
        password="secret",
        ssh=ssh,
        **overrides,
    )


def test_ssh_tunnel_is_dynamic_reused_and_cleaned_up():
    processes = []

    def popen(*args, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    manager = SshTunnelManager(
        popen_factory=popen,
        port_allocator=lambda: 43123,
        readiness_check=lambda host, port: True,
    )

    first = manager.endpoint(ssh_profile())
    second = manager.endpoint(ssh_profile())

    assert first == TunnelEndpoint("127.0.0.1", 43123, True)
    assert second == first
    assert len(processes) == 1

    manager.close_all()
    assert processes[0].terminated is True


def test_ssh_tunnel_restarts_after_process_exit():
    processes = []

    def popen(*args, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    manager = SshTunnelManager(
        popen_factory=popen,
        port_allocator=lambda: 43123,
        readiness_check=lambda host, port: True,
    )
    manager.endpoint(ssh_profile())
    processes[0].returncode = 255

    manager.endpoint(ssh_profile())

    assert len(processes) == 2


def test_restarted_ssh_tunnel_invalidates_pool_even_when_port_is_reused():
    processes = []
    pools = []

    def popen(*args, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    class FakePool:
        def __init__(self, **kwargs):
            self.closed = False
            self.connection = object()
            pools.append(self)

        def get_connection(self):
            return self.connection

        def close(self):
            self.closed = True

    tunnel_manager = SshTunnelManager(
        popen_factory=popen,
        port_allocator=lambda: 43123,
        readiness_check=lambda host, port: True,
    )
    pool_manager = ConnectionPoolManager(pool_factory=FakePool)
    profile = ssh_profile(pool_size=2)
    config = {"host": "127.0.0.1", "port": 43123, "password": "secret"}
    first_endpoint = tunnel_manager.endpoint(profile)
    first_connection = pool_manager.get_connection(profile, first_endpoint, config)
    processes[0].returncode = 255

    restarted_endpoint = tunnel_manager.endpoint(profile)
    restarted_connection = pool_manager.get_connection(
        profile, restarted_endpoint, config
    )

    assert restarted_endpoint == first_endpoint
    assert restarted_endpoint.generation != first_endpoint.generation
    assert restarted_connection is not first_connection
    assert pools[0].closed is True
    assert len(pool_manager._pools) == 1


def test_ssh_tunnel_is_stopped_when_profile_switches_to_direct_connection():
    process = FakeProcess()
    manager = SshTunnelManager(
        popen_factory=lambda *args, **kwargs: process,
        port_allocator=lambda: 43123,
        readiness_check=lambda host, port: True,
    )
    profile = ssh_profile()
    manager.endpoint(profile)

    direct = manager.endpoint(replace(profile, ssh=SshConfig(enabled=False)))

    assert direct == TunnelEndpoint("mysql", 3306, False)
    assert process.terminated is True
    assert manager._tunnels == {}


def test_connection_pool_is_reused_and_password_rotation_changes_key():
    pools = []

    class FakePool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.connection = object()
            self.closed = False
            pools.append(self)

        def get_connection(self):
            return self.connection

        def close(self):
            self.closed = True

    manager = ConnectionPoolManager(pool_factory=FakePool)
    profile = ConnectionProfile(
        name="test",
        host="db",
        port=3306,
        user="reader",
        password="one",
        pool_size=2,
    )
    endpoint = TunnelEndpoint("db", 3306)
    config = {"host": "db", "user": "reader", "password": "one"}

    first = manager.get_connection(profile, endpoint, config)
    second = manager.get_connection(profile, endpoint, config)
    rotated = manager.get_connection(
        profile,
        endpoint,
        {**config, "password": "two"},
    )

    assert first is second
    assert rotated is not first
    assert len(pools) == 2
    assert pools[0].closed is True
    assert len(manager._pools) == 1
    assert "one" not in repr(pools[0].kwargs["pool_name"])


def test_connection_pool_is_closed_when_profile_switches_to_direct_mode():
    class FakePool:
        def __init__(self, **kwargs):
            self.closed = False

        def get_connection(self):
            return "pooled"

        def close(self):
            self.closed = True

    manager = ConnectionPoolManager(pool_factory=FakePool)
    profile = ConnectionProfile(
        name="test",
        host="db",
        port=3306,
        user="reader",
        password="secret",
        pool_size=2,
    )
    endpoint = TunnelEndpoint("db", 3306)
    config = {"host": "db", "user": "reader", "password": "secret"}
    manager.get_connection(profile, endpoint, config)
    pool = next(iter(manager._pools.values()))
    connector = MagicMock(return_value="direct")

    result = manager.get_connection(
        replace(profile, pool_size=0),
        endpoint,
        config,
        connect_factory=connector,
    )

    assert result == "direct"
    assert pool.closed is True
    assert manager._pools == {}
    assert manager._profile_keys == {}


def test_discard_closes_and_removes_matching_pool():
    class FakePool:
        def __init__(self, **kwargs):
            self.closed = False

        def get_connection(self):
            return "pooled"

        def close(self):
            self.closed = True

    manager = ConnectionPoolManager(pool_factory=FakePool)
    profile = ConnectionProfile(
        name="test",
        host="db",
        port=3306,
        user="reader",
        password="secret",
        pool_size=2,
    )
    endpoint = TunnelEndpoint("db", 3306)
    config = {"host": "db", "user": "reader", "password": "secret"}
    manager.get_connection(profile, endpoint, config)
    pool = next(iter(manager._pools.values()))

    manager.discard(profile, endpoint, config)

    assert pool.closed is True
    assert manager._pools == {}
    assert manager._profile_keys == {}


def test_pool_size_zero_uses_direct_connector():
    connector = MagicMock(return_value="connection")
    manager = ConnectionPoolManager()
    profile = ConnectionProfile(
        name="legacy",
        host="db",
        port=3306,
        user="reader",
        password="secret",
        pool_size=0,
    )

    result = manager.get_connection(
        profile,
        TunnelEndpoint("db", 3306),
        {"host": "db"},
        connect_factory=connector,
    )

    assert result == "connection"
    connector.assert_called_once_with(host="db")


def test_query_control_shutdowns_bound_connection():
    connection = MagicMock()
    control = QueryControl()
    control.bind(connection)

    control.cancel()

    connection.shutdown.assert_called_once_with()


def test_runtime_cleanup_closes_pools_before_ssh_tunnels():
    calls = []
    with (
        patch(
            "mysql_mcp_server.runtime.connection_pool_manager.clear",
            side_effect=lambda: calls.append("pools"),
        ),
        patch(
            "mysql_mcp_server.runtime.ssh_tunnel_manager.close_all",
            side_effect=lambda: calls.append("tunnels"),
        ),
    ):
        close_runtime_resources()

    assert calls == ["pools", "tunnels"]
