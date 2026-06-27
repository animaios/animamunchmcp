"""jDocMunch MCP - structured documentation retrieval server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jdocmunch-mcp")
except PackageNotFoundError:
    __version__ = "unknown"
