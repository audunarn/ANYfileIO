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

from .cad import FormatDescriptor
from .cad_backend import backend_status
from .calculix.dat import parse_dat
from .calculix.frd import parse_frd
from .calculix.inp import summarize_deck
from .diagnostics import FileFormatError
from .sesam.document import read_sesam_fem_document
from .sesam.sif import read_sesam_sif_stress

__all__ = [
    "FormatDescriptor",
    "READERS",
    "available_formats",
    "describe",
    "known_formats",
    "read",
    "supported_suffixes",
]


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

_BUILTIN_FORMATS = (
    FormatDescriptor("calculix-dat", (".dat",), "finite_element_results", frozenset({"read", "inspect"})),
    FormatDescriptor("sesam-fem", (".fem",), "finite_element_model", frozenset({"read", "inspect"})),
    FormatDescriptor("calculix-frd", (".frd",), "finite_element_results", frozenset({"read", "inspect"})),
    FormatDescriptor("calculix-inp", (".inp",), "finite_element_model", frozenset({"read", "inspect"})),
    FormatDescriptor("sesam-sif", (".sif",), "finite_element_results", frozenset({"read", "inspect"})),
)
_CAD_INSTALL_HINT = (
    "native CAD operations are not included in the ANYfileio 0.2.0 PyPI release; "
    "see https://github.com/audunarn/ANYfileIO#development"
)
_CAD_FORMATS = (
    FormatDescriptor(
        "step",
        (".step", ".stp"),
        "cad_brep",
        frozenset({"read", "write", "inspect", "assembly", "tessellate"}),
        "occt",
        "ANYfileio-occt",
        _CAD_INSTALL_HINT,
    ),
    FormatDescriptor(
        "iges",
        (".iges", ".igs"),
        "cad_brep",
        frozenset({"read", "write", "inspect", "tessellate"}),
        "occt",
        "ANYfileio-occt",
        _CAD_INSTALL_HINT,
    ),
    FormatDescriptor(
        "brep",
        (".brep",),
        "cad_brep_native",
        frozenset({"read", "write"}),
        "occt",
        "ANYfileio-occt",
        _CAD_INSTALL_HINT,
    ),
)
_KNOWN_FORMATS = tuple(sorted((*_BUILTIN_FORMATS, *_CAD_FORMATS), key=lambda item: item.name))
_SUFFIX_INDEX = {
    suffix: descriptor
    for descriptor in _KNOWN_FORMATS
    for suffix in descriptor.suffixes
}
if len(_SUFFIX_INDEX) != sum(len(item.suffixes) for item in _KNOWN_FORMATS):
    raise RuntimeError("format suffixes are ambiguous")


def supported_suffixes() -> Tuple[str, ...]:
    """Every suffix :func:`read` recognizes."""

    return tuple(sorted(_SUFFIX_INDEX))


def known_formats() -> tuple[FormatDescriptor, ...]:
    """Every built-in and optional format known without loading a provider."""

    return _KNOWN_FORMATS


def _has_capability(descriptor: FormatDescriptor, capabilities: Any) -> bool:
    checks = {
        "read": descriptor.name in capabilities.read_formats,
        "write": descriptor.name in capabilities.write_formats,
        "inspect": capabilities.inspect,
        "assembly": capabilities.assembly,
        "tessellate": capabilities.tessellate,
    }
    return all(checks[name] for name in descriptor.capabilities)


def available_formats() -> tuple[FormatDescriptor, ...]:
    """Formats usable from built-ins or an already-ready CAD provider."""

    status = backend_status()
    if status.state != "ready" or status.capabilities is None:
        return tuple(sorted(_BUILTIN_FORMATS, key=lambda item: item.name))
    cad = tuple(item for item in _CAD_FORMATS if _has_capability(item, status.capabilities))
    return tuple(sorted((*_BUILTIN_FORMATS, *cad), key=lambda item: item.name))


def describe(path: str | Path) -> str:
    """What :func:`read` would treat this file as."""

    suffix = Path(path).suffix.lower()
    descriptor = _SUFFIX_INDEX.get(suffix)
    if descriptor is None:
        raise FileFormatError(
            f"unrecognized suffix {suffix!r}; expected one of {', '.join(supported_suffixes())}",
            code="FEM010",
        )
    entry = READERS.get(suffix)
    if entry is not None:
        return entry[1]
    return f"{descriptor.name.upper()} {descriptor.kind.replace('_', ' ')} via optional {descriptor.provider_distribution} backend"


def read(path: str | Path, **options: Any) -> Any:
    """Read a file, dispatching on its suffix.

    Options are passed through to the format's own reader, so ``strict=False``
    reaches the SESAM readers that take it.
    """

    target = Path(path)
    suffix = target.suffix.lower()
    entry = READERS.get(suffix)
    if entry is None:
        descriptor = _SUFFIX_INDEX.get(suffix)
        if descriptor is not None and descriptor.backend_id is not None:
            from .cad_operations import read_cad

            return read_cad(target, **options)
        raise FileFormatError(
            f"unrecognized suffix {suffix!r}; expected one of {', '.join(supported_suffixes())}",
            code="FEM010",
        )
    if not target.is_file():
        raise FileNotFoundError(f"no such file: {target}")
    return entry[0](target, **options)
