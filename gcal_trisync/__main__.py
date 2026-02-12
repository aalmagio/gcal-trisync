"""
Entry point for running gcal_trisync as a module.

Usage:
    python -m gcal_trisync --config config.yaml
"""

import sys
from .cli import main

if __name__ == '__main__':
    sys.exit(main())
