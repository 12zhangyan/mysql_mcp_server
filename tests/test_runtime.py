from unittest.mock import MagicMock

from mysql_mcp_server.config import ConnectionProfile, SshConfig
from mysql_mcp_server.runtime import (
    ConnectionPoolManager,
    QueryControl,
    SshTunnelManager,
    TunnelEndpoint,
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


def test_connection_pool_is_reused_and_password_rotation_changes_key():
    pools = []

    class FakePool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.connection = object()
            pools.append(self)

        def get_connection(self):
            return self.connection

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
    assert "one" not in repr(pools[0].kwargs["pool_name"])


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
