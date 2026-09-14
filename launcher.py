"""Entry point for PyInstaller bundle.

When frozen, __package__ may be "" (empty string) which still trips the
`from .xxx import` branch in main.py. We force the flat-import fallback
by nuking __package__ unconditionally and pointing sys.path at the
bundle root.
"""

import sys
import os

# Point sys.path at the directory containing this script (and all .py files)
_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

# Force the flat-import (`from xxx import`) branch in main.py/router.py
# by clearing __package__ on builtins. PyInstaller sets it to "" which
# still triggers the relative-import branch in some code paths.
import builtins
builtins.__package__ = None

import main  # noqa: E402  (must run after __package__ reset above)

main.main()
