# tests/conftest.py
import pytest
import os
import mysql.connector
from mysql.connector import Error


@pytest.fixture(autouse=True)
def legacy_test_configuration(monkeypatch):
    """Give unit tests a harmless legacy profile unless they override it."""
    monkeypatch.delenv("MYSQL_PROFILES_FILE", raising=False)
    monkeypatch.delenv("MYSQL_CONNECTIONS_FILE", raising=False)
    monkeypatch.setenv("MYSQL_USER", os.getenv("MYSQL_USER", "readonly_test"))
    monkeypatch.setenv("MYSQL_PASSWORD", os.getenv("MYSQL_PASSWORD", "readonly_test"))


@pytest.fixture(scope="session")
def mysql_connection():
    """Open an optional read-only integration-test connection."""
    try:
        connection = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", "testpassword"),
            database=os.getenv("MYSQL_DATABASE", "test_db"),
        )

        if connection.is_connected():
            cursor = connection.cursor()
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION READ ONLY")
            try:
                yield connection
            finally:
                connection.rollback()
                cursor.close()
                connection.close()

    except Error as e:
        pytest.skip(f"Read-only MySQL integration connection unavailable: {e}")


@pytest.fixture(scope="session")
def mysql_cursor(mysql_connection):
    """Create a test cursor."""
    cursor = mysql_connection.cursor()
    yield cursor
    cursor.close()
