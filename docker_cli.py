"""Backward-compatible alias for :mod:`app.cli.main`."""

from importlib import import_module
import sys


_implementation = import_module("app.cli.main")

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
