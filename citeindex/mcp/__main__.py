"""Support ``python -m citeindex.mcp`` for the optional MCP server."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
