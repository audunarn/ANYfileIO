"""Reading a file without being told what it is.

``read(path)`` dispatches on the suffix and returns whatever that format's reader
returns.  It is a convenience over the per-format readers, not a lowest common
denominator: a ``.fem`` gives a typed document and a ``.frd`` gives result
fields, and flattening those into one shape would throw away most of both.

Dispatch is by suffix rather than by sniffing content. A file named ``.frd`` that
contains a FEM document is a mislabelled file, and guessing past the label would
hide that from whoever has to fix it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Tuple

from .calculix.dat import parse_dat
from .calculix.frd import parse_frd
from .calculix.inp import summarize_deck
from .diagnostics import FileFormatError
from .sesam.document import read_sesam_fem_document
from .sesam.sif import read_sesam_sif_stress

__all__ = ["READERS", "describe", "read", "supported_suffixes"]


def _read_fem(path: Path, **options: Any) -> Any:
    return read_sesam_fem_document(path, **options)


def _read_sif(path: Path, **options: Any) -> Any:
    return read_sesam_sif_stress(path, **options)


def _read_frd(path: Path, **options: Any) -> Any:
    return parse_frd(path, **options)


def _read_dat(path: Path, **options: Any) -> Any:
    return parse_dat(path, **options)


def _read_inp(path: Path, **options: Any) -> Any:
    return summarize_deck(path, **options)


READERS: Mapping[str, Tuple[Callable[..., Any], str]] = {
    ".fem": (_read_fem, "SESAM formatted FEM model"),
    ".sif": (_read_sif, "SESAM SIF results"),
    ".frd": (_read_frd, "CalculiX FRD results"),
    ".dat": (_read_dat, "CalculiX DAT results"),
    ".inp": (_read_inp, "CalculiX input deck summary"),
}


def supported_suffixes() -> Tuple[str, ...]:
    """Every suffix :func:`read` recognizes."""

    return tuple(sorted(READERS))


def describe(path: str | Path) -> str:
    """What :func:`read` would treat this file as."""

    suffix = Path(path).suffix.lower()
    entry = READERS.get(suffix)
    if entry is None:
        raise FileFormatError(
            f"unrecognized suffix {suffix!r}; expected one of {', '.join(supported_suffixes())}",
            code="FEM010",
        )
    return entry[1]


def read(path: str | Path, **options: Any) -> Any:
    """Read a file, dispatching on its suffix.

    Options are passed through to the format's own reader, so ``strict=False``
    reaches the SESAM readers that take it.
    """

    target = Path(path)
    suffix = target.suffix.lower()
    entry = READERS.get(suffix)
    if entry is None:
        raise FileFormatError(
            f"unrecognized suffix {suffix!r}; expected one of {', '.join(supported_suffixes())}",
            code="FEM010",
        )
    if not target.is_file():
        raise FileNotFoundError(f"no such file: {target}")
    return entry[0](target, **options)
