from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

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
        port_in_use=lambda host, port: False,
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
        port_in_use=lambda host, port: False,
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
        port_in_use=lambda host, port: False,
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
        port_in_use=lambda host, port: False,
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


def test_query_control_kills_a_c_extension_session_without_touching_it():
    import threading

    connection = MagicMock()
    connection.shutdown.side_effect = NotImplementedError
    kill_started = threading.Event()
    finish_kill = threading.Event()
    kill_threads = []

    def kill():
        kill_threads.append(threading.current_thread())
        kill_started.set()
        finish_kill.wait(5)

    control = QueryControl()
    control.bind(connection, kill)
    control.cancel()
    control.cancel()

    assert kill_started.wait(5)
    assert kill_threads == [kill_threads[0]]
    assert kill_threads[0] is not threading.current_thread()
    # Closing would hand the connection back while the worker still uses it.
    connection.close.assert_not_called()
    unbound = threading.Event()
    threading.Thread(target=lambda: (control.unbind(), unbound.set())).start()
    assert not unbound.wait(0.2)
    finish_kill.set()
    assert unbound.wait(5)


def test_query_control_refuses_to_bind_after_cancel():
    connection = MagicMock()
    kill = MagicMock()
    control = QueryControl()
    control.cancel()

    with pytest.raises(RuntimeError, match="cancelled before execution"):
        control.bind(connection, kill)

    control.unbind()
    connection.shutdown.assert_not_called()
    kill.assert_not_called()


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


@pytest.mark.parametrize("initial_size,new_size", [(1, 3), (3, 1)])
def test_pool_resize_rebuilds_capacity_and_reuses_replacement(initial_size, new_size):
    pools = []

    class FakePool:
        def __init__(self, **kwargs):
            self.size = kwargs["pool_size"]
            self.connection = object()
            self.closed = False
            pools.append(self)

        def get_connection(self):
            return self.connection

        def close(self):
            self.closed = True

    manager = ConnectionPoolManager(pool_factory=FakePool)
    profile = ConnectionProfile(
        name="test", host="db", port=3306, user="reader", pool_size=initial_size
    )
    endpoint = TunnelEndpoint("db", 3306)
    config = {"host": "db", "user": "reader"}
    first = manager.get_connection(profile, endpoint, config)
    resized = replace(profile, pool_size=new_size)
    second = manager.get_connection(resized, endpoint, config)
    reused = manager.get_connection(resized, endpoint, config)

    assert first is not second
    assert second is reused
    assert [pool.size for pool in pools] == [initial_size, new_size]
    assert pools[0].closed and not pools[1].closed
    assert len(manager._pools) == 1
    # Cleanup for an in-flight request from the old configuration must not
    # discard the replacement pool.
    manager.discard(profile, endpoint, config)
    assert not pools[1].closed
    assert manager.get_connection(resized, endpoint, config) is second
    manager.clear()
    assert pools[1].closed


class RecordingPool:
    """Fake pool whose connections point back to it, like PooledMySQLConnection."""

    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.disposals = 0
        RecordingPool.created.append(self)

    def get_connection(self):
        connection = MagicMock()
        connection._cnx_pool = self
        return connection

    def close(self):
        self.disposals += 1


def pooled_profile(**overrides):
    values = {
        "name": "test",
        "host": "db",
        "port": 3306,
        "user": "reader",
        "password": "secret",
        "pool_size": 2,
        "max_database_pools": 2,
    }
    return ConnectionProfile(**{**values, **overrides})


def test_switching_databases_reuses_one_pool_per_database():
    RecordingPool.created = []
    manager = ConnectionPoolManager(pool_factory=RecordingPool)
    profile = pooled_profile()
    endpoint = TunnelEndpoint("db", 3306)

    for database in ["a", "b", "a", "b", None, None]:
        manager.get_connection(profile, endpoint, {"host": "db", "database": database})

    # MySQL cannot unselect a database, so no database is a pool of its own.
    assert [pool.kwargs.get("database") for pool in RecordingPool.created] == [
        "a",
        "b",
        None,
    ]
    # None evicted the least recently used pool, a; b stays in use.
    assert [pool.disposals for pool in RecordingPool.created] == [1, 0, 0]
    assert len(manager._pools) == 2


def test_borrowed_connection_of_an_evicted_pool_is_closed_on_release():
    RecordingPool.created = []
    manager = ConnectionPoolManager(pool_factory=RecordingPool)
    profile = pooled_profile(max_database_pools=1)
    endpoint = TunnelEndpoint("db", 3306)
    borrowed = manager.get_connection(profile, endpoint, {"database": "a"})
    current = manager.get_connection(profile, endpoint, {"database": "b"})
    evicted, active = RecordingPool.created

    manager.release(borrowed)
    manager.release(current)

    borrowed.close.assert_called_once_with()
    # The evicted pool closes its idle connections again, now including the
    # returned one; the active pool keeps its connection.
    assert evicted.disposals == 2
    assert active.disposals == 0


def test_release_closes_a_direct_connection():
    connection = MagicMock(spec=["close"])
    ConnectionPoolManager().release(connection)
    connection.close.assert_called_once_with()


def test_slow_ssh_tunnel_start_does_not_block_other_profiles():
    import threading

    started = threading.Event()
    release = threading.Event()

    def readiness(host, port):
        started.set()
        release.wait(5)
        return True

    manager = SshTunnelManager(
        popen_factory=lambda *args, **kwargs: FakeProcess(),
        port_allocator=lambda: 43123,
        readiness_check=readiness,
        port_in_use=lambda host, port: False,
    )
    slow = threading.Thread(target=manager.endpoint, args=(ssh_profile(),))
    slow.start()
    try:
        assert started.wait(5)
        direct = ConnectionProfile(
            name="direct", host="db", port=3306, user="reader", password="x"
        )
        finished = threading.Event()
        threading.Thread(
            target=lambda: (manager.endpoint(direct), finished.set())
        ).start()
        assert finished.wait(1)
    finally:
        release.set()
        slow.join(5)
    manager.close_all()


def test_ssh_tunnel_refuses_a_local_port_already_in_use():
    popen = MagicMock()
    manager = SshTunnelManager(
        popen_factory=popen,
        readiness_check=lambda host, port: True,
        port_in_use=lambda host, port: True,
    )
    profile = ssh_profile()
    profile = replace(profile, ssh=replace(profile.ssh, local_port=3330))

    with pytest.raises(RuntimeError, match="3330 .* already in use"):
        manager.endpoint(profile)

    popen.assert_not_called()


def test_ssh_tunnel_that_exits_while_port_answers_is_not_ready():
    process = FakeProcess()
    process.returncode = 255
    manager = SshTunnelManager(
        popen_factory=lambda *args, **kwargs: process,
        port_allocator=lambda: 43123,
        # Another listener answers on the port that ssh failed to bind.
        readiness_check=lambda host, port: True,
        port_in_use=lambda host, port: False,
    )

    with pytest.raises(RuntimeError, match="exited early"):
        manager.endpoint(ssh_profile())
    assert manager._tunnels == {}
