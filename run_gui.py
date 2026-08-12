#!/usr/bin/env python
"""Run the ANYfileio inspector straight from a checkout.

Point an IDE's Run button at this file, or::

    python run_gui.py [file]

An optional path opens that file immediately, which is handy as an IDE run
configuration parameter when the same fixture is being looked at repeatedly.

``src`` is put on ``sys.path`` first, so the NumPy-only inspector works from a
fresh clone without injecting optional semantic sibling checkouts.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SOURCE_ROOT = _ROOT / "src"
if _SOURCE_ROOT.is_dir() and str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from anyfileio.gui import main  # noqa: E402  - import follows the path setup

if __name__ == "__main__":
    raise SystemExit(main())
