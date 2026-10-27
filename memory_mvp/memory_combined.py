"""Loader: the one file a scenario names to get the combined memory layer.

    plugin_files:
      - memory_combined.py

Pulls in the git-backed store, the write contract, the access policy, the
observation lane, the roles and the stages. Keeping it to one file means a
scenario does not have to know how the package is laid out.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from gitmem import layer as _layer            # noqa: E402,F401  registers git.v1
from gitmem import roles as _roles            # noqa: E402,F401  registers roles
from gitmem import validators as _validators  # noqa: E402,F401  registers stages

__all__ = ["_layer", "_roles", "_validators"]
