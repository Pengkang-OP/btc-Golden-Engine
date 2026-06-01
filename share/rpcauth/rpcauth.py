#!/usr/bin/env python3
"""Generate rpcauth credentials for Bitcoin Core JSON-RPC access.

Creates a salted HMAC-SHA256 password hash for bitcoin.conf
rpcauth directive. Supports random password generation,
interactive password entry, and JSON output.

用法:
    python share/rpcauth/rpcauth.py <username>
    python share/rpcauth/rpcauth.py <username> <password> --json
"""

# Copyright (c) 2015-2021 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

import hmac
from argparse import ArgumentParser
from getpass import getpass
from secrets import token_hex, token_urlsafe


def generate_salt(size: int) -> str:
    """Create size byte hex salt."""
    return token_hex(size)


def generate_password() -> str:
    """Create 32 byte b64 password."""
    return token_urlsafe(32)


def password_to_hmac(salt: str, password: str) -> str:
    """Return hex HMAC-SHA256 of password with salt."""
    m = hmac.new(salt.encode("utf-8"), password.encode("utf-8"), "SHA256")
    return m.hexdigest()


def main() -> None:
    """CLI entry point: generate rpcauth credentials."""
    parser = ArgumentParser(description="Create login credentials for a JSON-RPC user")
    parser.add_argument("username", help="the username for authentication")
    parser.add_argument(
        "password",
        help='leave empty to generate a random password or specify "-" to prompt for password',
        nargs="?",
    )
    parser.add_argument(
        "-j",
        "--json",
        help="output to json instead of plain-text",
        action="store_true",
    )
    args = parser.parse_args()

    if not args.password:
        args.password = generate_password()
    elif args.password == "-":
        args.password = getpass()

    # Create 16 byte hex salt
    salt = generate_salt(16)
    password_to_hmac(salt, args.password)

    if args.json:
        pass
    else:
        pass


if __name__ == "__main__":
    main()
