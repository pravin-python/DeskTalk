"""argparse helpers shared by every entry point."""

import argparse


def port_number(value: str) -> int:
    """argparse ``type=`` for a TCP/UDP port (1-65535)."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("{!r} is not a valid number".format(value))
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port
