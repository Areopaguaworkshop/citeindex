"""Console entry point for the optional MCP server.

Kept separate from ``server.py`` so a base ``citeindex`` install does not fail
at import time when the ``mcp`` extra is absent.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    try:
        from .server import main as server_main
    except ModuleNotFoundError as exc:
        if exc.name == "mcp":
            print(
                "citeindex-mcp requires the optional MCP dependencies.\n"
                "Install them with: pip install 'citeindex[mcp]'",
                file=sys.stderr,
            )
            return 2
        raise
    return server_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
