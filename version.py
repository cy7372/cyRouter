"""Single source of truth for cyRouter's version.

Format: MAJOR.MINOR.PATCH.BUILD
- MAJOR.MINOR.PATCH are maintained manually (semantic versioning).
- BUILD is auto-incremented by build.ps1 after a successful exe build.

Do NOT edit BUILD by hand unless you know what you're doing.
"""

MAJOR = 1
MINOR = 2
PATCH = 0
BUILD = 17

VERSION_TUPLE = (MAJOR, MINOR, PATCH, BUILD)

# Always four segments (e.g. "1.0.0.1"); Windows exe resources and Inno
# Setup both expect the full quad-numeric version.
__version__ = f"{MAJOR}.{MINOR}.{PATCH}.{BUILD}"
__display_version__ = __version__
