"""nexus-mcp-plus: upstream Nexus Mods MCP (140 tools) + free-account download.

Package layout:
  server.py        imports upstream `nexus_mcp._server.mcp`, registers the
                   three nexus_free_* tools, stdio entry + CLI modes
  free_download.py engine: cookie readers, nxm mint, download chain (CLI-able)
  tests/           stdlib unittest suite
"""
