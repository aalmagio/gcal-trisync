"""Windows GUI launcher for PyInstaller and direct execution."""

import sys

from gcal_trisync.cli import main as cli_main
from gcal_trisync.gui import main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        raise SystemExit(cli_main(sys.argv[2:]))
    raise SystemExit(main())
