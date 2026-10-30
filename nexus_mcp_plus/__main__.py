"""CLI wrapper: keeps `python -m nexus_mcp_plus` working alongside the
`nexus-mcp-plus` console script. Upstream keeps its argparse in the same
module; ours lives in server.main()."""
import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
