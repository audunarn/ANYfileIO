"""Lazy discovery and validation of the optional OCCT CAD provider."""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Literal

from .cad import (
    BackendCompatibilityError,
    BackendDuplicateError,
    BackendLoadError,
    BackendUnavailableError,
    CadBackendProtocol,
    CadCapabilities,
    CadDiagnostic,
)

__all__ = ["BackendStatus", "backend_status"]

ENTRY_POINT_GROUP = "anyfileio.backends"
ENTRY_POINT_NAME = "occt"
ENTRY_POINT_TARGET = "anyfileio_occt.backend:get_backend"
BACKEND_ID = "occt"
CAD_BACKEND_PROTOCOL_VERSION = 1
BACKEND_COMPATIBILITY_VERSION = 1
INSTALL_HINT = 'pip install "ANYfileio-occt"'

BackendState = Literal["missing", "discovered", "duplicate", "ready", "broken", "incompatible"]


@dataclass(frozen=True, slots=True)
class BackendStatus:
    backend_id: str
    state: BackendState
    entry_point: str | None
    distribution: str | None
    expected_protocol_version: int
    observed_protocol_version: int | None
    expected_backend_compatibility_version: int
    observed_backend_compatibility_version: int | None
    capabilities: CadCapabilities | None
    diagnostic: CadDiagnostic | None


_LOCK = threading.RLock()
_ENUMERATED = False
_ENTRY_POINT: Any | None = None
_STATUS: BackendStatus | None = None
_BACKEND: CadBackendProtocol | None = None
_FAILURE: Exception | None = None


def _distribution_name(entry_point: Any) -> str | None:
    distribution = getattr(entry_point, "dist", None)
    if distribution is None:
        return None
    name = getattr(distribution, "name", None)
    if isinstance(name, str) and name:
        return name
    try:
        candidate = distribution.metadata["Name"]
    except Exception:
        return None
    return candidate if isinstance(candidate, str) and candidate else None


def _entry_point_text(entry_point: Any) -> str:
    value = getattr(entry_point, "value", None)
    return value if isinstance(value, str) and value else str(entry_point)


def _diagnostic(
    code: str,
    message: str,
    *,
    state: str,
    entry_point: str | None = None,
    distribution: str | None = None,
    observed_protocol: Any = None,
    observed_compatibility: Any = None,
    cause: BaseException | None = None,
) -> CadDiagnostic:
    details: dict[str, object] = {
        "backend_id": BACKEND_ID,
        "expected_backend_compatibility_version": BACKEND_COMPATIBILITY_VERSION,
        "expected_protocol_version": CAD_BACKEND_PROTOCOL_VERSION,
        "install_hint": INSTALL_HINT,
        "operation": "backend_load",
        "state": state,
    }
    if entry_point is not None:
        details["entry_point"] = entry_point
    if distribution is not None:
        details["distribution"] = distribution
    if observed_protocol is not None:
        details["observed_protocol_version"] = str(observed_protocol)
    if observed_compatibility is not None:
        details["observed_backend_compatibility_version"] = str(observed_compatibility)
    if cause is not None:
        details["cause"] = f"{type(cause).__name__}: {cause}"
    return CadDiagnostic(code, "error", message, details=details)


def _matching_entry_points() -> tuple[Any, ...]:
    entries = metadata.entry_points()
    select = getattr(entries, "select", None)
    if callable(select):
        matches = tuple(select(group=ENTRY_POINT_GROUP, name=ENTRY_POINT_NAME))
    else:
        matches = tuple(
            item
            for item in entries
            if getattr(item, "group", None) == ENTRY_POINT_GROUP and getattr(item, "name", None) == ENTRY_POINT_NAME
        )
    return tuple(sorted(matches, key=lambda item: (_distribution_name(item) or "", _entry_point_text(item))))


def _discover_locked() -> BackendStatus:
    global _ENUMERATED, _ENTRY_POINT, _STATUS
    if _ENUMERATED:
        assert _STATUS is not None
        return _STATUS
    matches = _matching_entry_points()
    _ENUMERATED = True
    if not matches:
        diagnostic = _diagnostic(
            "cad.backend.missing",
            f"CAD backend {BACKEND_ID!r} is not installed; {INSTALL_HINT}",
            state="missing",
        )
        _STATUS = BackendStatus(
            BACKEND_ID,
            "missing",
            None,
            None,
            CAD_BACKEND_PROTOCOL_VERSION,
            None,
            BACKEND_COMPATIBILITY_VERSION,
            None,
            None,
            diagnostic,
        )
    elif len(matches) > 1:
        texts = tuple(_entry_point_text(item) for item in matches)
        diagnostic = _diagnostic(
            "cad.backend.duplicate",
            "more than one OCCT backend entry point is installed",
            state="duplicate",
            entry_point=", ".join(texts),
        )
        _STATUS = BackendStatus(
            BACKEND_ID,
            "duplicate",
            ", ".join(texts),
            None,
            CAD_BACKEND_PROTOCOL_VERSION,
            None,
            BACKEND_COMPATIBILITY_VERSION,
            None,
            None,
            diagnostic,
        )
    else:
        _ENTRY_POINT = matches[0]
        _STATUS = BackendStatus(
            BACKEND_ID,
            "discovered",
            _entry_point_text(_ENTRY_POINT),
            _distribution_name(_ENTRY_POINT),
            CAD_BACKEND_PROTOCOL_VERSION,
            None,
            BACKEND_COMPATIBILITY_VERSION,
            None,
            None,
            None,
        )
    return _STATUS


def backend_status() -> BackendStatus:
    """Return cached provider status without importing the provider."""

    with _LOCK:
        return _discover_locked()


def _required_capabilities(capabilities: CadCapabilities) -> bool:
    return (
        {"step", "iges"} <= capabilities.read_formats
        and {"step", "iges"} <= capabilities.write_formats
        and _IMPORT_MODES <= capabilities.import_modes
        and capabilities.inspect
        and capabilities.assembly
        and capabilities.tessellate
        and capabilities.preserve
        and capabilities.translate
    )


_IMPORT_MODES = frozenset({"manifest_only", "preview", "live"})


def _method_has_exact_shape(
    method: Any,
    positional: tuple[str, ...],
    keyword_only: tuple[str, ...],
) -> bool:
    if not callable(method):
        return False
    try:
        parameters = tuple(inspect.signature(method).parameters.values())
    except (TypeError, ValueError):
        return False
    if tuple(parameter.name for parameter in parameters) != (*positional, *keyword_only):
        return False
    return all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in parameters[: len(positional)]
    ) and all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters[len(positional) :]
    )


def _set_broken_status(
    *,
    entry_text: str,
    distribution: str | None,
    message: str,
    cause: BaseException | None = None,
    observed_protocol: Any = None,
    observed_compatibility: Any = None,
    capabilities: CadCapabilities | None = None,
) -> BackendLoadError:
    global _FAILURE, _STATUS
    diagnostic = _diagnostic(
        "cad.backend.load_failed",
        message,
        state="broken",
        entry_point=entry_text,
        distribution=distribution,
        observed_protocol=observed_protocol,
        observed_compatibility=observed_compatibility,
        cause=cause,
    )
    failure = BackendLoadError(diagnostic=diagnostic)
    if cause is not None:
        failure.__cause__ = cause
    _FAILURE = failure
    _STATUS = BackendStatus(
        BACKEND_ID,
        "broken",
        entry_text,
        distribution,
        CAD_BACKEND_PROTOCOL_VERSION,
        observed_protocol if isinstance(observed_protocol, int) else None,
        BACKEND_COMPATIBILITY_VERSION,
        observed_compatibility if isinstance(observed_compatibility, int) else None,
        capabilities,
        diagnostic,
    )
    return failure


def _raise_cached_failure(status: BackendStatus) -> None:
    if _FAILURE is not None:
        raise _FAILURE
    assert status.diagnostic is not None
    if status.state == "missing":
        raise BackendUnavailableError(diagnostic=status.diagnostic)
    if status.state == "duplicate":
        raise BackendDuplicateError(diagnostic=status.diagnostic)
    if status.state == "incompatible":
        raise BackendCompatibilityError(diagnostic=status.diagnostic)
    raise BackendLoadError(diagnostic=status.diagnostic)


def _load_backend() -> CadBackendProtocol:
    """Load and validate the provider for a later CAD operation.

    This private boundary performs no CAD I/O.  Success and terminal failure are
    both process-cached.
    """

    global _BACKEND, _FAILURE, _STATUS
    with _LOCK:
        status = _discover_locked()
        if _BACKEND is not None:
            return _BACKEND
        if status.state in {"missing", "duplicate", "broken", "incompatible"}:
            _raise_cached_failure(status)
        assert _ENTRY_POINT is not None
        entry_text = _entry_point_text(_ENTRY_POINT)
        distribution = _distribution_name(_ENTRY_POINT)
        if entry_text != ENTRY_POINT_TARGET or distribution is None or distribution.casefold() != "anyfileio-occt":
            raise _set_broken_status(
                entry_text=entry_text,
                distribution=distribution,
                message="OCCT backend entry-point target or distribution is invalid",
            )
        try:
            factory = _ENTRY_POINT.load()
            if not callable(factory):
                raise TypeError("entry-point target is not callable")
            backend = factory()
        except Exception as error:
            failure = _set_broken_status(
                entry_text=entry_text,
                distribution=distribution,
                message="OCCT backend could not be loaded",
                cause=error,
            )
            raise failure

        observed_id = getattr(backend, "backend_id", None)
        observed_protocol = getattr(backend, "protocol_version", None)
        observed_compatibility = getattr(backend, "backend_compatibility_version", None)
        capabilities = getattr(backend, "capabilities", None)
        backend_version = getattr(backend, "backend_version", None)
        identity_compatible = (
            observed_id == BACKEND_ID
            and observed_protocol == CAD_BACKEND_PROTOCOL_VERSION
            and observed_compatibility == BACKEND_COMPATIBILITY_VERSION
        )
        if not identity_compatible:
            diagnostic = _diagnostic(
                "cad.backend.incompatible",
                "OCCT backend identity or protocol is incompatible",
                state="incompatible",
                entry_point=entry_text,
                distribution=distribution,
                observed_protocol=observed_protocol,
                observed_compatibility=observed_compatibility,
            )
            failure = BackendCompatibilityError(diagnostic=diagnostic)
            _FAILURE = failure
            _STATUS = BackendStatus(
                BACKEND_ID,
                "incompatible",
                entry_text,
                distribution,
                CAD_BACKEND_PROTOCOL_VERSION,
                observed_protocol if isinstance(observed_protocol, int) else None,
                BACKEND_COMPATIBILITY_VERSION,
                observed_compatibility if isinstance(observed_compatibility, int) else None,
                capabilities if isinstance(capabilities, CadCapabilities) else None,
                diagnostic,
            )
            raise failure

        valid_loaded_backend = (
            isinstance(backend_version, str)
            and bool(backend_version)
            and isinstance(capabilities, CadCapabilities)
            and _required_capabilities(capabilities)
            and _method_has_exact_shape(
                getattr(backend, "read", None),
                ("source_snapshot",),
                ("source_sha256", "source_name", "options", "tessellation_options", "cancellation"),
            )
            and _method_has_exact_shape(
                getattr(backend, "tessellate", None),
                ("document",),
                ("options", "cancellation"),
            )
            and _method_has_exact_shape(
                getattr(backend, "translate", None),
                ("document", "destination_temporary"),
                ("options", "cancellation"),
            )
        )
        if not valid_loaded_backend:
            raise _set_broken_status(
                entry_text=entry_text,
                distribution=distribution,
                message="OCCT backend metadata, capabilities, or call shapes are invalid",
                observed_protocol=observed_protocol,
                observed_compatibility=observed_compatibility,
                capabilities=capabilities if isinstance(capabilities, CadCapabilities) else None,
            )

        _BACKEND = backend
        _STATUS = BackendStatus(
            BACKEND_ID,
            "ready",
            entry_text,
            distribution,
            CAD_BACKEND_PROTOCOL_VERSION,
            observed_protocol,
            BACKEND_COMPATIBILITY_VERSION,
            observed_compatibility,
            capabilities,
            None,
        )
        return backend


def _reset_backend_cache_for_tests() -> None:
    """Reset process discovery state.  Private and test-only."""

    global _ENUMERATED, _ENTRY_POINT, _STATUS, _BACKEND, _FAILURE
    with _LOCK:
        _ENUMERATED = False
        _ENTRY_POINT = None
        _STATUS = None
        _BACKEND = None
        _FAILURE = None
