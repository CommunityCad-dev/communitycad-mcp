"""Run the local server over stdio: `communitycad-mcp` or `python -m communitycad_mcp`."""

from __future__ import annotations

import logging


def main() -> None:
    # stdout carries the MCP protocol; keep logs on stderr, and keep httpx quiet
    # so request details never end up in a log.
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from .server import build_server

    build_server().run("stdio")


if __name__ == "__main__":
    main()
