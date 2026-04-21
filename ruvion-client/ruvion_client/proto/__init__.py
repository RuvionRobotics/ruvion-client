"""Generated FlatBuffers bindings.

The generated code uses absolute imports (`from ruvion.motion...`), so we
prepend this directory to sys.path. Keeps the generator call simple and
avoids post-processing every file.
"""
import sys as _sys
from pathlib import Path as _Path

_this = str(_Path(__file__).resolve().parent)
if _this not in _sys.path:
    _sys.path.insert(0, _this)
