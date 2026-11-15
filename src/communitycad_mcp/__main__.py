"""`communitycad-mcp` (or `python -m communitycad_mcp`): run the local server over stdio.

With a command (install, uninstall, doctor) it sets the server up instead;
see `communitycad-mcp --help`.
"""

from __future__ import annotations

import logging
import sys


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        from .cli import run

        code = run(argv)
        if code is not None:
            sys.exit(code)
    # stdout carries the MCP protocol; keep logs on stderr, and keep httpx quiet
    # so request details never end up in a log.
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from .server import build_server

    build_server().run("stdio")


if __name__ == "__main__":
    main()
