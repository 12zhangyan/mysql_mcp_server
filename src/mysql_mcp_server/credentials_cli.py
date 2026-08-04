"""Interactive local management for credentials stored in the OS keyring."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Sequence

from .config import load_connection_registry
from .credential_store import (
    CredentialProviderError,
    delete_keyring_password,
    get_keyring_password,
    set_keyring_password,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="readonly-db-mcp credentials")
    parser.add_argument(
        "--profiles-file",
        help="Absolute path to mysql-connections.toml",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("set", "status", "delete"):
        command = subparsers.add_parser(action)
        command.add_argument("profile")
    return parser


def _keyring_target(profile_name: str):
    profile = load_connection_registry(force_reload=True).get(profile_name)
    if profile.credential_provider != "keyring":
        raise CredentialProviderError(
            f"Connection '{profile_name}' does not use credential_provider='keyring'"
        )
    return profile.credential_service, profile.credential_ref


def credentials_main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(list(argv))
    if args.profiles_file:
        os.environ["MYSQL_PROFILES_FILE"] = os.path.abspath(args.profiles_file)
    try:
        service, reference = _keyring_target(args.profile)
        if args.action == "set":
            password = getpass.getpass("Database password: ")
            if not password:
                raise CredentialProviderError("Password must not be empty")
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                raise CredentialProviderError("Passwords do not match")
            set_keyring_password(service, reference, password)
            print(f"Credential stored for profile '{args.profile}'.")
            return 0
        if args.action == "status":
            get_keyring_password(service, reference)
            print(f"Credential is available for profile '{args.profile}'.")
            return 0
        delete_keyring_password(service, reference)
        print(f"Credential deleted for profile '{args.profile}'.")
        return 0
    except (CredentialProviderError, ValueError) as exc:
        print(f"Credential operation failed: {exc}", file=sys.stderr)
        return 1
