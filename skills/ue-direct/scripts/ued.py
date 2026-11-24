#!/usr/bin/env python3
"""UE-Direct CLI entry point: python ued.py <command> ...  (see SKILL.md)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from uedirect.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
