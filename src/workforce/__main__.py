"""Allows `python -m workforce ...` (fallback if the .exe launcher is blocked)."""

import sys

from workforce.cli import main

sys.exit(main())
