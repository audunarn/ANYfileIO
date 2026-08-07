#!/usr/bin/env python
"""Run the ANYfileio inspector straight from a checkout.

Point an IDE's Run button at this file, or::

    python run_gui.py [file]

An optional path opens that file immediately, which is handy as an IDE run
configuration parameter when the same fixture is being looked at repeatedly.

``src`` is put on ``sys.path`` first, so this works in a fresh clone with nothing
installed.  The sibling packages ANYmesher and ANYmaterial still have to be
importable: their checkouts are added too when they sit alongside this one, so a
side-by-side clone of the family works with nothing installed at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_CANDIDATES = [
    _ROOT / "src",
    # Sibling checkouts, by repository name.  Absent ones are skipped, so an
    # installed sibling is used instead without anything special happening.
    _ROOT.parent / "ANYmesh" / "src",
    _ROOT.parent / "ANYmaterial" / "src",
]
for _path in _CANDIDATES:
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from anyfileio.gui import main  # noqa: E402  - import follows the path setup

if __name__ == "__main__":
    raise SystemExit(main())
