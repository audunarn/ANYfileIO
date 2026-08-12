"""Lazy loading for the optional semantic mesh and material runtime."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module, metadata
import re
from typing import Any, Callable

from .diagnostics import FemDiagnostic, SemanticDependencyError

_INSTALL_HINT = 'pip install "ANYfileio[semantics]"'
_NUMERIC_RELEASE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")


@dataclass(frozen=True)
class _SemanticCapabilities:
    Mesh: type[Any]
    MaterialSpec: type[Any]
    elastic_compliance_matrix: Callable[[Any], Any]
    material_symmetry: Callable[[Any], Any]


_DISTRIBUTIONS = (
    ("ANYmesher", "anymesher", (0, 2), (0, 3), ("Mesh",)),
    (
        "ANYmaterial",
        "anymaterial",
        (0, 1),
        (0, 2),
        ("MaterialSpec", "elastic_compliance_matrix", "material_symmetry"),
    ),
)

_cached_capabilities: _SemanticCapabilities | None = None


def _failure(code: str, message: str, context: dict[str, Any]) -> SemanticDependencyError:
    details = {
        **context,
        "extra": "semantics",
        "install_hint": _INSTALL_HINT,
    }
    diagnostic = FemDiagnostic(code=code, message=message, context=details)
    return SemanticDependencyError(message, code=code, diagnostics=(diagnostic,))


def _normalised_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _numeric_release(version: str) -> tuple[int, ...] | None:
    if not _NUMERIC_RELEASE.fullmatch(version):
        return None
    return tuple(int(component) for component in version.split("."))


def _within(release: tuple[int, ...], lower: tuple[int, ...], upper: tuple[int, ...]) -> bool:
    width = max(len(release), len(lower), len(upper))
    value = release + (0,) * (width - len(release))
    minimum = lower + (0,) * (width - len(lower))
    maximum = upper + (0,) * (width - len(upper))
    return minimum <= value < maximum


def _required_range(lower: tuple[int, ...], upper: tuple[int, ...]) -> str:
    return f">={'.'.join(map(str, lower))},<{'.'.join(map(str, upper))}"


def require_semantics() -> _SemanticCapabilities:
    """Return validated semantic capabilities, loading them only on demand."""

    global _cached_capabilities
    if _cached_capabilities is not None:
        return _cached_capabilities

    versions: dict[str, str] = {}
    missing: list[str] = []
    for distribution, _module, _lower, _upper, _symbols in _DISTRIBUTIONS:
        try:
            versions[distribution] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            missing.append(distribution)

    if missing:
        ordered = sorted(missing, key=_normalised_distribution_name)
        message = (
            f"optional semantics distributions are missing: {', '.join(ordered)}; "
            f"install with {_INSTALL_HINT}"
        )
        raise _failure("SEM001", message, {"missing_distributions": tuple(ordered)})

    for distribution, _module, lower, upper, _symbols in _DISTRIBUTIONS:
        observed = versions[distribution]
        release = _numeric_release(observed)
        if release is None or not _within(release, lower, upper):
            required = _required_range(lower, upper)
            message = (
                f"optional semantics distribution {distribution} has version {observed!r}; "
                f"required {required}; install with {_INSTALL_HINT}"
            )
            raise _failure(
                "SEM002",
                message,
                {
                    "distribution": distribution,
                    "required_range": required,
                    "observed_version": observed,
                },
            )

    loaded: dict[str, Any] = {}
    for distribution, module_name, _lower, _upper, symbols in _DISTRIBUTIONS:
        try:
            module = import_module(module_name)
            loaded.update({symbol: getattr(module, symbol) for symbol in symbols})
        except Exception as exc:
            message = (
                f"optional semantics module {module_name!r} could not provide "
                f"{', '.join(symbols)}: {type(exc).__name__}: {exc}; "
                f"install with {_INSTALL_HINT}"
            )
            raise _failure(
                "SEM003",
                message,
                {
                    "distribution": distribution,
                    "import_name": module_name,
                    "required_symbols": symbols,
                    "cause_type": type(exc).__name__,
                    "cause_message": str(exc),
                },
            ) from exc

    _cached_capabilities = _SemanticCapabilities(
        Mesh=loaded["Mesh"],
        MaterialSpec=loaded["MaterialSpec"],
        elastic_compliance_matrix=loaded["elastic_compliance_matrix"],
        material_symmetry=loaded["material_symmetry"],
    )
    return _cached_capabilities


def _reset_semantics_cache() -> None:
    """Clear the successful capability cache for focused tests."""

    global _cached_capabilities
    _cached_capabilities = None
