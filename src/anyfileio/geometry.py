"""Editable geometry interchange, with lazy owner and native-provider imports.

Native files delegate their entire schema to ANYgeometry. CAD files use the
optional ANYfileio-occt geometry adapter. Importing this module loads neither.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import ExitStack, nullcontext
import importlib
import math
import os
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .cad import CadOperationError, CadReadOptions, CadWriteOptions
from .cad_operations import (
    _check_cancelled, _cleanup_after_failure, _copy_source_snapshot,
    _create_output_temporary, _flush_owned_output, _sha256_file,
    _unlink_owned, _validate_owned_output,
)

__all__ = [
    "GeometryReadOptions", "GeometryWriteOptions", "GeometryDiagnostic",
    "GeometryApproximation", "GeometryImportReport", "GeometryWriteReport",
    "GeometryImportResult", "GeometryInterchangeError", "GeometryLeaseReleaseError", "read_geometry", "write_geometry",
]


class GeometryInterchangeError(CadOperationError):
    default_code = "geometry.operation.failed"


class GeometryLeaseReleaseError(GeometryInterchangeError):
    """A lease failed to release; the report makes publication status explicit."""
    default_code = "geometry.lease.release_failed"

    def __init__(self, report, *, output_committed: bool):
        super().__init__("read lease release failed" + (" after output publication (output committed)" if output_committed else " before output publication"))
        self.report = report
        self.output_committed = output_committed


def _validate_options(options: Any) -> None:
    if options.fidelity not in {"exact", "approximate"}:
        raise ValueError("fidelity must be exact or approximate")
    if options.validation_mode not in {"committed", "certified"}:
        raise ValueError("validation_mode must be committed or certified")
    if options.coordinate_space not in {"model_local", "external"}:
        raise ValueError("coordinate_space must be model_local or external")
    if options.max_deviation_m is not None:
        if isinstance(options.max_deviation_m, bool) or not isinstance(options.max_deviation_m, (int, float)) or not math.isfinite(options.max_deviation_m) or options.max_deviation_m <= 0:
            raise ValueError("max_deviation_m must be positive and finite")
    if options.fidelity == "approximate" and options.max_deviation_m is None:
        raise ValueError("approximate fidelity requires max_deviation_m")
    for name in ("max_segments", "max_depth", "max_topology_work"):
        value = getattr(options, name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class GeometryReadOptions:
    fidelity: str = "exact"
    max_deviation_m: float | None = None
    max_segments: int = 10000
    max_depth: int = 24
    source_length_unit_override: str | None = None
    validation_mode: str = "committed"
    coordinate_space: str = "model_local"
    max_topology_work: int = 1000000

    def __post_init__(self) -> None:
        _validate_options(self)
        normalized = CadReadOptions(source_length_unit_override=self.source_length_unit_override)
        object.__setattr__(self, "source_length_unit_override", normalized.source_length_unit_override)


@dataclass(frozen=True, slots=True)
class GeometryWriteOptions:
    fidelity: str = "exact"
    max_deviation_m: float | None = None
    max_segments: int = 10000
    max_depth: int = 24
    output_length_unit: str | None = None
    validation_mode: str = "committed"
    coordinate_space: str = "model_local"
    max_topology_work: int = 1000000

    def __post_init__(self) -> None:
        _validate_options(self)
        normalized = CadWriteOptions(mode="translate", target_length_unit=self.output_length_unit)
        object.__setattr__(self, "output_length_unit", normalized.target_length_unit)


@dataclass(frozen=True, slots=True)
class GeometryDiagnostic:
    code: str
    message: str
    entities: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entities", tuple(self.entities))


@dataclass(frozen=True, slots=True)
class GeometryApproximation:
    source: object
    max_deviation_m: float
    bound_m: float
    method: str

    def __post_init__(self) -> None:
        if not (math.isfinite(self.max_deviation_m) and self.max_deviation_m > 0
                and math.isfinite(self.bound_m) and 0 <= self.bound_m <= self.max_deviation_m):
            raise ValueError("approximation bound must be finite and within the requested limit")


def _freeze_report(report: Any, mapping: str) -> None:
    values = dict(getattr(report, mapping))
    values = {key: tuple(value) if isinstance(value, (list, tuple)) else value for key, value in values.items()}
    object.__setattr__(report, mapping, MappingProxyType(values))
    for name in ("approximations", "metadata_losses", "diagnostics"):
        object.__setattr__(report, name, tuple(getattr(report, name)))


@dataclass(frozen=True, slots=True)
class GeometryWriteReport:
    format: str
    source_model_id: object
    source_revision: int
    output_sha256: str
    source_unit: str
    output_unit: str
    geometry_to_cad: Mapping = field(default_factory=dict)
    approximations: tuple = ()
    metadata_losses: tuple[str, ...] = ()
    diagnostics: tuple = ()
    backend_version: str | None = None
    binding_version: str | None = None
    occt_version: str | None = None
    output_document_id: str | None = None

    def __post_init__(self) -> None:
        _freeze_report(self, "geometry_to_cad")


@dataclass(frozen=True, slots=True)
class GeometryImportReport:
    format: str
    source_sha256: str
    model_id: object
    revision: int
    source_unit: str
    output_unit: str = "m"
    cad_to_geometry: Mapping = field(default_factory=dict)
    approximations: tuple = ()
    metadata_losses: tuple = ()
    diagnostics: tuple = ()
    backend_version: str | None = None
    binding_version: str | None = None
    occt_version: str | None = None

    def __post_init__(self) -> None:
        _freeze_report(self, "cad_to_geometry")


@dataclass(frozen=True, slots=True)
class GeometryImportResult:
    geometry: Any
    report: GeometryImportReport


def _owner() -> Any:
    try:
        return importlib.import_module("anygeometry")
    except ImportError as error:
        raise GeometryInterchangeError(
            "editable geometry requires a compatible ANYgeometry installation",
            code="geometry.owner.unavailable",
        ) from error


def _provider(module: str) -> Any:
    try:
        return importlib.import_module(f"anyfileio_occt.{module}")
    except ImportError as error:
        raise GeometryInterchangeError(
            "CAD geometry conversion requires ANYfileio-occt[geometry]",
            code="geometry.provider.unavailable",
        ) from error


def geometry_format(path: str | Path, format: str | None = None) -> str:
    """Resolve compound native suffixes without claiming every JSON/gzip file."""
    name = Path(path).name.casefold()
    inferred = None
    if name.endswith((".anygeometry.json", ".anygeometry.json.gz")):
        inferred = "anygeometry"
    else:
        inferred = {".step": "step", ".stp": "step", ".iges": "iges", ".igs": "iges", ".brep": "brep"}.get(Path(path).suffix.casefold())
    if format is None:
        if inferred is None:
            raise GeometryInterchangeError("unrecognized geometry filename; specify format explicitly", code="geometry.format.unknown")
        return inferred
    selected = str(format).casefold()
    if selected in {"native", "json", "json.gz"}:
        selected = "anygeometry"
    if selected not in {"anygeometry", "step", "iges", "brep"}:
        raise GeometryInterchangeError(f"unsupported geometry format {format!r}", code="geometry.format.unknown")
    if selected != "anygeometry" and inferred is None:
        raise GeometryInterchangeError("CAD geometry requires a matching CAD filename suffix", code="geometry.format.mismatch")
    if inferred is not None and inferred != selected:
        raise GeometryInterchangeError("filename and explicit format disagree", code="geometry.format.mismatch")
    return selected


def _local_only(options: Any) -> None:
    if options.coordinate_space != "model_local":
        raise GeometryInterchangeError("external/CRS geometry conversion is not enabled", code="geometry.external_transform_undefined")


def read_geometry(path: str | Path, *, format: str | None = None,
                  options: GeometryReadOptions | None = None, cancellation=None) -> GeometryImportResult:
    options = GeometryReadOptions() if options is None else options
    if not isinstance(options, GeometryReadOptions):
        raise TypeError("options must be GeometryReadOptions")
    _local_only(options)
    _check_cancelled(cancellation)
    target = Path(path)
    selected = geometry_format(target, format)
    if selected == "anygeometry":
        owner = _owner()
        snapshot, digest, identity = _copy_source_snapshot(target, cancellation=cancellation)
        try:
            geometry = owner.read_geometry(snapshot)
            if options.source_length_unit_override is not None and options.source_length_unit_override != geometry.units:
                raise GeometryInterchangeError("native source units cannot be overridden", code="geometry.native.unit_override")
            if options.validation_mode == "certified":
                # Reuse the owner's exact certified gate; no duplicate audit policy.
                owner.to_dict(geometry, certified=True)
            _check_cancelled(cancellation)
            if _sha256_file(snapshot, cancellation) != digest:
                raise GeometryInterchangeError("native source snapshot changed", code="geometry.source.changed")
            return GeometryImportResult(geometry, GeometryImportReport(
                selected, digest, geometry.model_id, geometry.revision,
                geometry.units, geometry.units,
            ))
        finally:
            _unlink_owned(snapshot, identity)
    adapter = _provider("geometry_import")
    from .cad_operations import read_cad
    document = read_cad(target, options=CadReadOptions(
        mode="live", retain_source=False,
        source_length_unit_override=options.source_length_unit_override,
    ), cancellation=cancellation)
    try:
        result = adapter.import_geometry(document, options=options, cancellation=cancellation)
        _check_cancelled(cancellation)
        if not isinstance(result, GeometryImportResult):
            raise GeometryInterchangeError("provider returned an invalid editable import result")
        owner = _owner()
        if not isinstance(result.geometry, owner.GeometryModel) or (
            result.report.model_id, result.report.revision, result.report.format,
            result.report.source_sha256,
        ) != (result.geometry.model_id, result.geometry.revision, selected, document.manifest.source_sha256):
            raise GeometryInterchangeError("provider import report is not bound to its model/source", code="geometry.report.mismatch")
        if options.fidelity == "exact" and result.report.approximations:
            raise GeometryInterchangeError("provider approximated an exact import", code="geometry.fidelity.mismatch")
        if options.validation_mode == "certified":
            owner.to_dict(result.geometry, certified=True)
        return result
    finally:
        document.close()


def write_geometry(path: str | Path, geometry: Any, *, format: str | None = None,
                   options: GeometryWriteOptions | None = None, read_lease=None,
                   cancellation=None) -> GeometryWriteReport:
    options = GeometryWriteOptions() if options is None else options
    if not isinstance(options, GeometryWriteOptions):
        raise TypeError("options must be GeometryWriteOptions")
    _local_only(options)
    _check_cancelled(cancellation)
    target = Path(path)
    selected = geometry_format(target, format)
    owner = _owner()
    if not isinstance(geometry, owner.GeometryModel):
        raise TypeError("geometry must be an ANYgeometry GeometryModel")
    if selected == "anygeometry" and options.output_length_unit is not None and options.output_length_unit != geometry.units:
        raise GeometryInterchangeError("native persistence preserves the model's units", code="geometry.native.unit_conversion")
    adapter = None if selected == "anygeometry" else _provider("geometry_export")
    backend = None
    if adapter is not None:
        from .cad_backend import _load_backend
        backend = _load_backend()
    temporary, identity = _create_output_temporary(target)
    leases = ExitStack()
    published = False
    report = None
    try:
        token = leases.enter_context(read_lease) if read_lease is not None else None
        if read_lease is not None and (token.geometry is not geometry or token.model_id != geometry.model_id or token.revision != geometry.revision):
            raise GeometryInterchangeError("read lease does not match geometry", code="geometry.lease.mismatch")
        source_id, source_revision, source_unit = geometry.model_id, geometry.revision, geometry.units
        if adapter is None:
            revision = geometry.revision
            model_id = geometry.model_id
            owner.write_geometry(temporary, geometry, certified=options.validation_mode == "certified")
            if (geometry.model_id, geometry.revision) != (model_id, revision):
                raise GeometryInterchangeError("geometry changed during serialization", code="geometry.revision.changed")
            report = GeometryWriteReport(selected, model_id, revision, _sha256_file(temporary, cancellation), geometry.units, geometry.units)
        else:
            export = getattr(adapter, "_export_geometry_to_temporary", adapter.export_geometry)
            report = export(geometry, temporary, options=options,
                read_lease=nullcontext(token) if read_lease is not None else None, cancellation=cancellation)
            if not isinstance(report, GeometryWriteReport):
                raise GeometryInterchangeError("provider returned an invalid editable export report")
        _validate_owned_output(temporary, identity)
        _flush_owned_output(temporary)
        digest = _sha256_file(temporary, cancellation)
        if digest != report.output_sha256:
            raise GeometryInterchangeError("provider output checksum mismatch", code="geometry.output.changed")
        if report.format != selected:
            raise GeometryInterchangeError("provider output format mismatch", code="geometry.report.mismatch")
        if adapter is not None:
            _validate_cad_write_identity(report, geometry, backend, digest, selected,
                options.output_length_unit or "m", source_id, source_revision, source_unit, cancellation)
        if options.fidelity == "exact" and report.approximations:
            raise GeometryInterchangeError("provider approximated an exact export", code="geometry.fidelity.mismatch")
        _check_cancelled(cancellation)
        if geometry.model_id != report.source_model_id or geometry.revision != report.source_revision:
            raise GeometryInterchangeError("geometry changed during export", code="geometry.revision.changed")
        _validate_owned_output(temporary, identity)
        os.replace(temporary, target)
        published = True
        return report
    except BaseException as error:
        _cleanup_after_failure(temporary, identity, error)
        raise
    finally:
        pending = sys.exc_info()
        try:
            leases.__exit__(*pending)
        except Exception as release_error:
            if pending[1] is not None:
                pending[1].add_note(f"read lease release also failed: {release_error}")
            else:
                raise GeometryLeaseReleaseError(report, output_committed=published) from release_error


def _validate_cad_write_identity(report, geometry, backend, digest, selected,
                                 output_unit, source_id, revision, source_unit, cancellation):
    """Validate actual-file identity and owner-bound mappings before publication."""
    from .cad import CadEntityRef, cad_import_document_id
    try:
        from anygeometry.identity import EntityHandle, ResolutionStatus
    except ImportError as error:
        raise GeometryInterchangeError("owner public handle validation is unavailable", code="geometry.owner.capability_missing") from error
    if not callable(getattr(geometry, "resolve_handle", None)):
        raise GeometryInterchangeError("owner public handle resolution is unavailable", code="geometry.owner.capability_missing")
    def mismatch(message):
        raise GeometryInterchangeError(message, code="geometry.report.mismatch")
    def checkpoint():
        _check_cancelled(cancellation)
        if (geometry.model_id, geometry.revision) != (source_id, revision):
            raise GeometryInterchangeError("geometry changed during export", code="geometry.revision.changed")
    checkpoint()
    if type(report.source_revision) is not int:
        mismatch("provider source revision must be an integer")
    if (report.source_model_id, report.source_revision, report.source_unit,
        report.output_unit, report.backend_version) != (
        source_id, revision, source_unit, output_unit, backend.backend_version):
        mismatch("provider source, unit or backend metadata mismatch")
    expected = cad_import_document_id(source_sha256=digest, source_format=selected,
        effective_source_length_unit=output_unit, heal=False, backend_id=backend.backend_id,
        backend_compatibility_version=backend.backend_compatibility_version)
    if not isinstance(report.output_document_id, str) or report.output_document_id != expected:
        mismatch("provider output document identity is not bound to the actual file")
    for handle, references in report.geometry_to_cad.items():
        checkpoint()
        if not isinstance(handle, EntityHandle) or handle.model_id != source_id:
            mismatch("provider mapping source must be a model-bound owner handle")
        resolution = geometry.resolve_handle(handle)
        checkpoint()
        if resolution.status is not ResolutionStatus.ACTIVE or resolution.resolved != (handle,):
            mismatch("provider mapping source handle is not active")
        if not isinstance(references, tuple) or not references:
            mismatch("provider mapping values must be nonempty tuples")
        for reference in references:
            checkpoint()
            if not isinstance(reference, CadEntityRef) or reference.document_id != expected:
                mismatch("provider mapping target is not bound to the output document")
