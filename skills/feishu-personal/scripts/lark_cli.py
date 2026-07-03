#!/usr/bin/env python3
"""Backward-compat shim: forwards to lark_tools.cli.main().

The real entry point lives at lark_tools/cli.py and is exposed as the `lark`
console script after `pip install -e .`. This shim keeps existing callers like
`python3 scripts/lark_cli.py ...` working.
"""
import os
import sys

# When run as a script (not installed), make sure the repo root is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lark_tools.cli import main

if __name__ == '__main__':
    main()
