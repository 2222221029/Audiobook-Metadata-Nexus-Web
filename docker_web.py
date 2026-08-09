"""Backward-compatible alias for :mod:`app.web.server`."""

from importlib import import_module
import sys


_implementation = import_module("app.web.server")

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
