#!/usr/bin/env python3
"""Verify the mesh IRCd is NOT reachable from this (non-allowed) source.

Run this from a machine or network position that is NOT the lens, a mesh
agent or a federation link. It attempts one direct TCP connect to the mesh
IRCd and sends nothing.

Exit codes:
  0  the connect was refused, reset or timed out -> the restriction holds
  1  the connect SUCCEEDED -> the restriction is broken (o10)
  2  usage error (argparse; --host and --port are required, no defaults)

See docs/deployment-permissions.md.
"""

from __future__ import annotations

import argparse
import socket
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", required=True, help="mesh IRCd host to probe")
    parser.add_argument(
        "--port", required=True, type=int, help="mesh IRCd port to probe"
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="connect timeout, seconds"
    )
    args = parser.parse_args(argv)

    try:
        with socket.create_connection((args.host, args.port), timeout=args.timeout):
            pass
    except OSError as exc:
        print(
            f"OK: direct connect not possible from this source ({type(exc).__name__})."
        )
        return 0
    print(
        "FAIL: a direct connect to the mesh IRCd succeeded from a non-allowed "
        "source; the bind/firewall restriction is not in effect.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
