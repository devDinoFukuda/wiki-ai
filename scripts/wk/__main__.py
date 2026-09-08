#!/usr/bin/env python3
"""Entry point for python -m wk"""

import sys
from . import cli

if __name__ == "__main__":
    sys.exit(cli.main())
