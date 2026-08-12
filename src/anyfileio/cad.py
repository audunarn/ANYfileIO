"""Backend-neutral CAD records and provider protocol.

This module is deliberately limited to stdlib and NumPy.  Provider loading,
filesystem operation orchestration, and the preview-artifact codec live in
separate modules/slices.
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, ContextManager, Literal, Mapping, Protocol, TypeAlias

import numpy as np

__all__ = [
    "BackendCompatibilityError",
    "BackendDuplicateError",
    "BackendLoadError",
    "BackendUnavailableError",
    "CadArtifactError",
    "CadAssetWriteReport",
    "CadBackendError",
    "CadBackendProtocol",
    "CadCapabilities",
    "CadDiagnostic",
    "CadDocument",
    "CadEntityRef",
    "CadError",
    "CadManifest",
    "CadOccurrenceRecord",
    "CadOperationCancelled",
    "CadOperationError",
    "CadPrototypeMesh",
    "CadPrototypeRecord",
    "CadReadOptions",
    "CadShapeRecord",
    "CadTessellation",
    "CadTessellationOptions",
    "CadTessellationResult",
    "CadValidationError",
    "CadWriteOptions",
    "CancellationCheck",
    "FormatDescriptor",
    "LengthUnit",
]


LengthUnit: TypeAlias = Literal["um", "mm", "cm", "m", "km", "in", "ft"]
CancellationCheck: TypeAlias = Callable[[], bool] | None
Bounds: TypeAlias = tuple[float, float, float, float, float, float]
JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | tuple["JSONValue", ...] | Mapping[str, "JSONValue"]

_DOCUMENT_ID_RE = re.compile(r"^cad-(?:import|geometry)-v1:[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^cad-tessellation-source-v1:[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENTITY_KINDS = (
    "prototype",
    "occurrence",
    "part",
    "sheet",
    "member",
    "solid",
    "shell",
    "face",
    "wire",
    "edge",
    "vertex",
)
_TOPOLOGY_KINDS = ("solid", "shell", "face", "wire", "edge", "vertex")
_SOURCE_FORMATS = frozenset({"step", "iges", "brep"})
_IMPORT_MODES = frozenset({"manifest_only", "preview", "live"})
_UNIT_SCALES: Mapping[str, float] = MappingProxyType(
    {"um": 1e-6, "mm": 1e-3, "cm": 1e-2, "m": 1.0, "km": 1000.0, "in": 0.0254, "ft": 0.3048}
)
_UNIT_ALIASES = {
    "um": "um",
    "µm": "um",
    "μm": "um",
    "micrometre": "um",
    "micrometres": "um",
    "micrometer": "um",
    "micrometers": "um",
    "mm": "mm",
    "millimetre": "mm",
    "millimetres": "mm",
    "millimeter": "mm",
    "millimeters": "mm",
    "cm": "cm",
    "centimetre": "cm",
    "centimetres": "cm",
    "centimeter": "cm",
    "centimeters": "cm",
    "m": "m",
    "metre": "m",
    "metres": "m",
    "meter": "m",
    "meters": "m",
    "km": "km",
    "kilometre": "km",
    "kilometres": "km",
    "kilometer": "km",
    "kilometers": "km",
    "in": "in",
    "inch": "in",
    "inches": "in",
    "ft": "ft",
    "foot": "ft",
    "feet": "ft",
}


def _require_nonempty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be bool")
    return value


def _positive_finite(value: Any, field_name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{field_name} must be positive and finite")
    return number


def _normalize_length_unit(value: Any) -> LengthUnit:
    if not isinstance(value, str):
        raise TypeError("length unit must be a string")
    canonical = _UNIT_ALIASES.get(value.strip().casefold())
    if canonical is None:
        raise ValueError(f"unsupported length unit {value!r}")
    return canonical  # type: ignore[return-value]


def _freeze_json(value: Any) -> JSONValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON metadata numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen = {str(key): _freeze_json(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
        return MappingProxyType(frozen)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"metadata value {value!r} is not a JSON scalar or tuple")


def _freeze_string_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, JSONValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    frozen = _freeze_json(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _normalize_bounds(value: Bounds | None, field_name: str) -> Bounds | None:
    if value is None:
        return None
    if len(value) != 6:
        raise ValueError(f"{field_name} must have six values")
    bounds = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in bounds):
        raise ValueError(f"{field_name} must be finite")
    if any(bounds[index] > bounds[index + 3] for index in range(3)):
        raise ValueError(f"{field_name} minima must not exceed maxima")
    return bounds  # type: ignore[return-value]


def _normalize_topology_counts(value: Mapping[str, int]) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(_TOPOLOGY_KINDS):
        raise ValueError(f"topology counts must contain exactly {_TOPOLOGY_KINDS!r}")
    normalized: dict[str, int] = {}
    for key in _TOPOLOGY_KINDS:
        count = value[key]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"topology count {key!r} must be a non-negative integer")
        normalized[key] = count
    return MappingProxyType(normalized)


def _readonly_array(
    value: Any,
    *,
    dtype: np.dtype[Any] | type[Any],
    shape: tuple[int | None, ...],
    field_name: str,
    finite: bool = False,
) -> np.ndarray[Any, Any]:
    array = np.asarray(value, dtype=dtype)
    if array.ndim != len(shape) or any(expected is not None and actual != expected for actual, expected in zip(array.shape, shape)):
        raise ValueError(f"{field_name} has invalid shape {array.shape!r}")
    if finite and not np.isfinite(array).all():
        raise ValueError(f"{field_name} must be finite")
    native_dtype = array.dtype.newbyteorder("=")
    array = np.array(array, dtype=native_dtype, order="C", copy=True)
    array.flags.writeable = False
    return array


def _normalize_affine(value: Any, field_name: str) -> np.ndarray[Any, Any]:
    matrix = _readonly_array(value, dtype=np.float64, shape=(4, 4), field_name=field_name, finite=True)
    if not np.array_equal(matrix[3], np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64)):
        raise ValueError(f"{field_name} must have affine last row (0, 0, 0, 1)")
    if not math.isfinite(float(np.linalg.det(matrix[:3, :3]))) or float(np.linalg.det(matrix[:3, :3])) == 0.0:
        raise ValueError(f"{field_name} must have a nonsingular linear block")
    return matrix


def _entity_sort_key(value: "CadEntityRef") -> tuple[str, int, str]:
    return (value.kind, value.local_id, value.document_id)


@dataclass(frozen=True, slots=True)
class FormatDescriptor:
    name: str
    suffixes: tuple[str, ...]
    kind: str
    capabilities: frozenset[str]
    backend_id: str | None = None
    provider_distribution: str | None = None
    install_hint: str | None = None

    def __post_init__(self) -> None:
        name = _require_nonempty_string(self.name, "name").casefold()
        suffixes = tuple(item.casefold() for item in self.suffixes)
        if not suffixes or len(set(suffixes)) != len(suffixes) or any(not item.startswith(".") or item != item.lower() for item in suffixes):
            raise ValueError("suffixes must be unique lower-case values with a leading dot")
        capabilities = frozenset(_require_nonempty_string(item, "capability").casefold() for item in self.capabilities)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "suffixes", suffixes)
        object.__setattr__(self, "kind", _require_nonempty_string(self.kind, "kind"))
        object.__setattr__(self, "capabilities", capabilities)
        for field_name in ("backend_id", "provider_distribution", "install_hint"):
            field_value = getattr(self, field_name)
            if field_value is not None:
                _require_nonempty_string(field_value, field_name)


@dataclass(frozen=True, slots=True)
class CadEntityRef:
    document_id: str
    kind: str
    local_id: int

    def __post_init__(self) -> None:
        if not _DOCUMENT_ID_RE.fullmatch(self.document_id):
            raise ValueError("document_id is not a canonical CAD document identity")
        if self.kind not in _ENTITY_KINDS:
            raise ValueError(f"unsupported CAD entity kind {self.kind!r}")
        _require_positive_int(self.local_id, "local_id")


@dataclass(frozen=True, slots=True)
class CadDiagnostic:
    code: str
    severity: Literal["info", "warning", "error", "fatal"]
    message: str
    entities: tuple[CadEntityRef, ...] = ()
    details: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty_string(self.code, "code")
        if self.severity not in {"info", "warning", "error", "fatal"}:
            raise ValueError(f"invalid diagnostic severity {self.severity!r}")
        _require_nonempty_string(self.message, "message")
        entities = tuple(sorted(tuple(self.entities), key=_entity_sort_key))
        if len(set(entities)) != len(entities):
            raise ValueError("diagnostic entities must be unique")
        object.__setattr__(self, "entities", entities)
        object.__setattr__(self, "details", _freeze_string_mapping(self.details, "details"))


class CadError(RuntimeError):
    default_code = "cad.error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        diagnostic: CadDiagnostic | None = None,
    ) -> None:
        selected_code = code or self.default_code
        if diagnostic is None:
            diagnostic = CadDiagnostic(selected_code, "error", message or selected_code)
        elif code is not None and diagnostic.code != code:
            raise ValueError("error code and diagnostic code disagree")
        self.code = diagnostic.code
        self.diagnostic = diagnostic
        super().__init__(f"{self.code}: {diagnostic.message}")


class CadBackendError(CadError):
    default_code = "cad.backend.error"


class BackendUnavailableError(CadBackendError):
    default_code = "cad.backend.missing"


class BackendDuplicateError(CadBackendError):
    default_code = "cad.backend.duplicate"


class BackendLoadError(CadBackendError):
    default_code = "cad.backend.load_failed"


class BackendCompatibilityError(CadBackendError):
    default_code = "cad.backend.incompatible"


class CadOperationError(CadError):
    default_code = "cad.operation.failed"


class CadValidationError(CadOperationError):
    default_code = "cad.validation.failed"


class CadOperationCancelled(CadOperationError):
    default_code = "cad.operation.cancelled"


class CadArtifactError(CadError):
    default_code = "cad.artifact.invalid"


@dataclass(frozen=True, slots=True)
class CadCapabilities:
    read_formats: frozenset[str] = frozenset()
    write_formats: frozenset[str] = frozenset()
    import_modes: frozenset[str] = frozenset()
    inspect: bool = False
    assembly: bool = False
    tessellate: bool = False
    preserve: bool = False
    translate: bool = False

    def __post_init__(self) -> None:
        read_formats = frozenset(str(item).casefold() for item in self.read_formats)
        write_formats = frozenset(str(item).casefold() for item in self.write_formats)
        import_modes = frozenset(str(item).casefold() for item in self.import_modes)
        if not read_formats <= _SOURCE_FORMATS or not write_formats <= _SOURCE_FORMATS:
            raise ValueError("capability formats must be step, iges, or brep")
        if not import_modes <= _IMPORT_MODES:
            raise ValueError("capability import modes are invalid")
        object.__setattr__(self, "read_formats", read_formats)
        object.__setattr__(self, "write_formats", write_formats)
        object.__setattr__(self, "import_modes", import_modes)
        for field_name in ("inspect", "assembly", "tessellate", "preserve", "translate"):
            _require_bool(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class CadReadOptions:
    mode: Literal["manifest_only", "preview", "live"] = "preview"
    retain_source: bool = True
    source_length_unit_override: LengthUnit | None = None
    heal: bool = False

    def __post_init__(self) -> None:
        if self.mode not in _IMPORT_MODES:
            raise ValueError(f"invalid CAD read mode {self.mode!r}")
        _require_bool(self.retain_source, "retain_source")
        _require_bool(self.heal, "heal")
        if self.source_length_unit_override is not None:
            object.__setattr__(self, "source_length_unit_override", _normalize_length_unit(self.source_length_unit_override))


@dataclass(frozen=True, slots=True)
class CadWriteOptions:
    mode: Literal["preserve", "translate"]
    target_format: Literal["step", "iges", "brep"] | None = None
    target_length_unit: LengthUnit | None = None
    heal: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"preserve", "translate"}:
            raise ValueError(f"invalid CAD write mode {self.mode!r}")
        if self.target_format is not None:
            normalized_format = str(self.target_format).casefold()
            if normalized_format not in _SOURCE_FORMATS:
                raise ValueError(f"invalid target format {self.target_format!r}")
            object.__setattr__(self, "target_format", normalized_format)
        if self.target_length_unit is not None:
            object.__setattr__(self, "target_length_unit", _normalize_length_unit(self.target_length_unit))
        _require_bool(self.heal, "heal")


@dataclass(frozen=True, slots=True)
class CadTessellationOptions:
    linear_deflection: float = 0.001
    angular_deflection: float = 0.35
    relative_deflection: bool = False
    parallel: bool = False
    include_edges: bool = False
    generate_normals: bool = True
    precision_policy: Literal["auto", "float32", "float64"] = "auto"

    def __post_init__(self) -> None:
        object.__setattr__(self, "linear_deflection", _positive_finite(self.linear_deflection, "linear_deflection"))
        object.__setattr__(self, "angular_deflection", _positive_finite(self.angular_deflection, "angular_deflection"))
        for field_name in ("relative_deflection", "parallel", "include_edges", "generate_normals"):
            _require_bool(getattr(self, field_name), field_name)
        if self.precision_policy not in {"auto", "float32", "float64"}:
            raise ValueError(f"invalid precision policy {self.precision_policy!r}")


@dataclass(frozen=True, slots=True)
class CadPrototypeRecord:
    id: int
    cad_ref: CadEntityRef
    name: str
    shape_type: str
    local_bounds_m: Bounds | None
    topology_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "prototype id")
        if self.cad_ref.kind != "prototype" or self.cad_ref.local_id != self.id:
            raise ValueError("prototype record id must equal its prototype reference id")
        if not isinstance(self.name, str):
            raise TypeError("prototype name must be a string")
        _require_nonempty_string(self.shape_type, "shape_type")
        object.__setattr__(self, "local_bounds_m", _normalize_bounds(self.local_bounds_m, "local_bounds_m"))
        object.__setattr__(self, "topology_counts", _normalize_topology_counts(self.topology_counts))


@dataclass(frozen=True, slots=True)
class CadOccurrenceRecord:
    id: int
    cad_ref: CadEntityRef
    prototype_id: int
    parent_id: int | None
    local_transform: np.ndarray[Any, Any]
    accumulated_transform: np.ndarray[Any, Any]
    world_bounds_m: Bounds | None
    name: str
    visible: bool

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "occurrence id")
        if self.cad_ref.kind != "occurrence" or self.cad_ref.local_id != self.id:
            raise ValueError("occurrence record id must equal its occurrence reference id")
        _require_positive_int(self.prototype_id, "prototype_id")
        if self.parent_id is not None:
            _require_positive_int(self.parent_id, "parent_id")
            if self.parent_id == self.id:
                raise ValueError("occurrence cannot parent itself")
        object.__setattr__(self, "local_transform", _normalize_affine(self.local_transform, "local_transform"))
        object.__setattr__(self, "accumulated_transform", _normalize_affine(self.accumulated_transform, "accumulated_transform"))
        object.__setattr__(self, "world_bounds_m", _normalize_bounds(self.world_bounds_m, "world_bounds_m"))
        if not isinstance(self.name, str):
            raise TypeError("occurrence name must be a string")
        _require_bool(self.visible, "visible")


@dataclass(frozen=True, slots=True)
class CadShapeRecord:
    cad_ref: CadEntityRef
    prototype_id: int
    occurrence_id: int | None
    parent_ref: CadEntityRef | None
    name: str
    shape_type: str
    prototype_local_bounds_m: Bounds | None
    world_bounds_m: Bounds | None
    color_rgba: tuple[float, float, float, float] | None
    layers: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.prototype_id, "prototype_id")
        if self.occurrence_id is not None:
            _require_positive_int(self.occurrence_id, "occurrence_id")
        if self.parent_ref is not None and self.parent_ref.document_id != self.cad_ref.document_id:
            raise ValueError("parent_ref belongs to another document")
        if not isinstance(self.name, str):
            raise TypeError("shape name must be a string")
        _require_nonempty_string(self.shape_type, "shape_type")
        object.__setattr__(self, "prototype_local_bounds_m", _normalize_bounds(self.prototype_local_bounds_m, "prototype_local_bounds_m"))
        object.__setattr__(self, "world_bounds_m", _normalize_bounds(self.world_bounds_m, "world_bounds_m"))
        if self.color_rgba is not None:
            if len(self.color_rgba) != 4:
                raise ValueError("color_rgba must have four values")
            color = tuple(float(item) for item in self.color_rgba)
            if not all(math.isfinite(item) and 0.0 <= item <= 1.0 for item in color):
                raise ValueError("color_rgba values must be finite in [0, 1]")
            object.__setattr__(self, "color_rgba", color)
        layers = tuple(sorted(set(self.layers)))
        if any(not isinstance(item, str) or not item for item in layers):
            raise ValueError("layer names must be non-empty strings")
        object.__setattr__(self, "layers", layers)


@dataclass(frozen=True, slots=True)
class CadManifest:
    document_id: str
    source_sha256: str
    source_name: str
    source_format: Literal["step", "iges", "brep"]
    source_length_unit: LengthUnit | Literal["unknown"]
    source_to_metre_scale: float | None
    internal_length_unit: Literal["m"]
    root_occurrence_ids: tuple[int, ...]
    prototypes: tuple[CadPrototypeRecord, ...]
    occurrences: tuple[CadOccurrenceRecord, ...]
    shapes: tuple[CadShapeRecord, ...]
    world_bounds_m: Bounds | None
    topology_counts: Mapping[str, int]
    external_references: tuple[str, ...]
    diagnostics: tuple[CadDiagnostic, ...]
    normalized_read_options: CadReadOptions
    backend_id: Literal["occt"]
    backend_version: str
    backend_compatibility_version: Literal[1]
    binding_distribution: Literal["cadquery-ocp-novtk"]
    binding_version: str
    occt_version: str

    def __post_init__(self) -> None:
        if not _DOCUMENT_ID_RE.fullmatch(self.document_id):
            raise ValueError("manifest document_id is invalid")
        if not _SHA256_RE.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be lower-case SHA-256")
        _require_nonempty_string(self.source_name, "source_name")
        if "/" in self.source_name or "\\" in self.source_name:
            raise ValueError("source_name must be a basename")
        if self.source_format not in _SOURCE_FORMATS:
            raise ValueError("source_format is invalid")
        if self.source_length_unit == "unknown":
            if self.source_to_metre_scale is not None:
                raise ValueError("unknown source units require a null scale")
        else:
            unit = _normalize_length_unit(self.source_length_unit)
            object.__setattr__(self, "source_length_unit", unit)
            scale = _positive_finite(self.source_to_metre_scale, "source_to_metre_scale")
            if scale != _UNIT_SCALES[unit]:
                raise ValueError("source_to_metre_scale disagrees with source_length_unit")
            object.__setattr__(self, "source_to_metre_scale", scale)
        if self.internal_length_unit != "m":
            raise ValueError("internal_length_unit must be 'm'")
        if not isinstance(self.normalized_read_options, CadReadOptions):
            raise TypeError("normalized_read_options must be CadReadOptions")
        if self.backend_id != "occt" or self.backend_compatibility_version != 1:
            raise ValueError("manifest backend identity is incompatible")
        if self.binding_distribution != "cadquery-ocp-novtk":
            raise ValueError("manifest binding_distribution is invalid")
        for field_name in ("backend_version", "binding_version", "occt_version"):
            _require_nonempty_string(getattr(self, field_name), field_name)

        prototypes = tuple(sorted(tuple(self.prototypes), key=lambda item: item.id))
        occurrences = tuple(sorted(tuple(self.occurrences), key=lambda item: item.id))
        shapes = tuple(sorted(tuple(self.shapes), key=lambda item: _entity_sort_key(item.cad_ref)))
        if len({item.id for item in prototypes}) != len(prototypes):
            raise ValueError("duplicate prototype ids")
        if len({item.id for item in occurrences}) != len(occurrences):
            raise ValueError("duplicate occurrence ids")
        if len({item.cad_ref for item in shapes}) != len(shapes):
            raise ValueError("duplicate shape references")
        prototype_by_id = {item.id: item for item in prototypes}
        occurrence_by_id = {item.id: item for item in occurrences}
        for item in prototypes:
            if item.cad_ref.document_id != self.document_id:
                raise ValueError("prototype belongs to another document")
        for item in occurrences:
            if item.cad_ref.document_id != self.document_id or item.prototype_id not in prototype_by_id:
                raise ValueError("occurrence foreign key is invalid")
            if item.parent_id is not None and item.parent_id not in occurrence_by_id:
                raise ValueError("occurrence parent foreign key is invalid")
            expected = item.local_transform if item.parent_id is None else occurrence_by_id[item.parent_id].accumulated_transform @ item.local_transform
            if not np.allclose(item.accumulated_transform, expected, rtol=0.0, atol=1e-12):
                raise ValueError("occurrence accumulated transform is inconsistent")
        roots = tuple(item.id for item in occurrences if item.parent_id is None)
        normalized_roots = tuple(sorted(self.root_occurrence_ids))
        if normalized_roots != roots or len(set(normalized_roots)) != len(normalized_roots):
            raise ValueError("root_occurrence_ids disagree with occurrence parents")
        for item in shapes:
            if item.cad_ref.document_id != self.document_id or item.prototype_id not in prototype_by_id:
                raise ValueError("shape foreign key is invalid")
            if item.occurrence_id is not None and item.occurrence_id not in occurrence_by_id:
                raise ValueError("shape occurrence foreign key is invalid")

        object.__setattr__(self, "root_occurrence_ids", normalized_roots)
        object.__setattr__(self, "prototypes", prototypes)
        object.__setattr__(self, "occurrences", occurrences)
        object.__setattr__(self, "shapes", shapes)
        object.__setattr__(self, "world_bounds_m", _normalize_bounds(self.world_bounds_m, "world_bounds_m"))
        object.__setattr__(self, "topology_counts", _normalize_topology_counts(self.topology_counts))
        external = tuple(sorted(set(self.external_references)))
        if any(not isinstance(item, str) or not item for item in external):
            raise ValueError("external references must be non-empty strings")
        object.__setattr__(self, "external_references", external)
        diagnostics = tuple(self.diagnostics)
        if any(entity.document_id != self.document_id for diagnostic in diagnostics for entity in diagnostic.entities):
            raise ValueError("manifest diagnostic entity belongs to another document")
        object.__setattr__(self, "diagnostics", diagnostics)


def _normalize_indices(value: Any, *, field_name: str, width: int, upper_bound: int) -> np.ndarray[Any, Any]:
    raw = np.asarray(value)
    if raw.ndim != 2 or raw.shape[1] != width or raw.dtype.kind not in {"u", "i"}:
        raise ValueError(f"{field_name} must be an integer array of shape (n, {width})")
    if raw.size and (int(raw.min()) < 0 or int(raw.max()) >= upper_bound):
        raise ValueError(f"{field_name} contains an out-of-range index")
    maximum = max(upper_bound - 1, int(raw.max()) if raw.size else 0)
    dtype = np.uint32 if maximum <= np.iinfo(np.uint32).max else np.uint64
    return _readonly_array(raw, dtype=dtype, shape=(None, width), field_name=field_name)


def _normalize_offsets(value: Any, *, owners: int, entries: int, field_name: str) -> np.ndarray[Any, Any]:
    offsets = _readonly_array(value, dtype=np.int64, shape=(owners + 1,), field_name=field_name)
    if offsets[0] != 0 or offsets[-1] != entries or np.any(np.diff(offsets) < 0):
        raise ValueError(f"{field_name} is not a complete monotonic range table")
    if owners and np.any(np.diff(offsets) <= 0):
        raise ValueError(f"{field_name} owners must have non-empty ranges")
    return offsets


@dataclass(frozen=True, slots=True)
class CadTessellation:
    origin: np.ndarray[Any, Any]
    positions: np.ndarray[Any, Any]
    triangles: np.ndarray[Any, Any]
    normals: np.ndarray[Any, Any] | None
    face_owners: tuple[CadEntityRef, ...]
    face_offsets: np.ndarray[Any, Any]
    edge_indices: np.ndarray[Any, Any] | None
    edge_owners: tuple[CadEntityRef, ...]
    edge_offsets: np.ndarray[Any, Any]
    precision: Literal["float32", "float64"]

    def __post_init__(self) -> None:
        if self.precision not in {"float32", "float64"}:
            raise ValueError("precision must be float32 or float64")
        position_dtype = np.float32 if self.precision == "float32" else np.float64
        origin = _readonly_array(self.origin, dtype=np.float64, shape=(3,), field_name="origin", finite=True)
        positions = _readonly_array(self.positions, dtype=position_dtype, shape=(None, 3), field_name="positions", finite=True)
        triangles = _normalize_indices(self.triangles, field_name="triangles", width=3, upper_bound=len(positions))
        if self.normals is None:
            normals = None
        else:
            normals = _readonly_array(self.normals, dtype=np.float32, shape=(len(positions), 3), field_name="normals", finite=True)
        face_owners = tuple(self.face_owners)
        if len(set(face_owners)) != len(face_owners) or any(item.kind != "face" for item in face_owners):
            raise ValueError("face owners must be unique face references")
        face_offsets = _normalize_offsets(self.face_offsets, owners=len(face_owners), entries=len(triangles), field_name="face_offsets")
        if bool(face_owners) != bool(len(triangles)):
            raise ValueError("triangle ranges and face owners must both be empty or non-empty")
        if self.edge_indices is None:
            if self.edge_owners:
                raise ValueError("edge owners require edge indices")
            edge_indices = None
            edge_owners: tuple[CadEntityRef, ...] = ()
            edge_offsets = _normalize_offsets(self.edge_offsets, owners=0, entries=0, field_name="edge_offsets")
        else:
            edge_indices = _normalize_indices(self.edge_indices, field_name="edge_indices", width=2, upper_bound=len(positions))
            edge_owners = tuple(self.edge_owners)
            if len(set(edge_owners)) != len(edge_owners) or any(item.kind != "edge" for item in edge_owners):
                raise ValueError("edge owners must be unique edge references")
            edge_offsets = _normalize_offsets(self.edge_offsets, owners=len(edge_owners), entries=len(edge_indices), field_name="edge_offsets")
            if bool(edge_owners) != bool(len(edge_indices)):
                raise ValueError("edge ranges and edge owners must both be empty or non-empty")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "triangles", triangles)
        object.__setattr__(self, "normals", normals)
        object.__setattr__(self, "face_owners", face_owners)
        object.__setattr__(self, "face_offsets", face_offsets)
        object.__setattr__(self, "edge_indices", edge_indices)
        object.__setattr__(self, "edge_owners", edge_owners)
        object.__setattr__(self, "edge_offsets", edge_offsets)


@dataclass(frozen=True, slots=True)
class CadPrototypeMesh:
    prototype_id: int
    tessellation: CadTessellation
    local_bounds_m: Bounds | None
    diagnostics: tuple[CadDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        _require_positive_int(self.prototype_id, "prototype_id")
        if not isinstance(self.tessellation, CadTessellation):
            raise TypeError("tessellation must be CadTessellation")
        object.__setattr__(self, "local_bounds_m", _normalize_bounds(self.local_bounds_m, "local_bounds_m"))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


@dataclass(frozen=True, slots=True)
class CadTessellationResult:
    source_identity: str
    options: CadTessellationOptions
    prototype_meshes: tuple[CadPrototypeMesh, ...]

    def __post_init__(self) -> None:
        if not _SOURCE_ID_RE.fullmatch(self.source_identity):
            raise ValueError("source_identity is invalid")
        if not isinstance(self.options, CadTessellationOptions):
            raise TypeError("options must be CadTessellationOptions")
        meshes = tuple(sorted(tuple(self.prototype_meshes), key=lambda item: item.prototype_id))
        if len({item.prototype_id for item in meshes}) != len(meshes):
            raise ValueError("duplicate prototype meshes")
        object.__setattr__(self, "prototype_meshes", meshes)


def _read_options_payload(options: CadReadOptions) -> Mapping[str, Any]:
    return {
        "heal": options.heal,
        "mode": options.mode,
        "retain_source": options.retain_source,
        "source_length_unit_override": options.source_length_unit_override,
    }


def _source_identity_for_manifest(manifest: CadManifest) -> str:
    payload = {
        "backend_compatibility_version": manifest.backend_compatibility_version,
        "backend_id": manifest.backend_id,
        "backend_version": manifest.backend_version,
        "binding_distribution": manifest.binding_distribution,
        "binding_version": manifest.binding_version,
        "effective_source_length_unit": manifest.source_length_unit,
        "normalized_read_options": _read_options_payload(manifest.normalized_read_options),
        "occt_version": manifest.occt_version,
        "source_format": manifest.source_format,
        "source_name": manifest.source_name,
        "source_sha256": manifest.source_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return f"cad-tessellation-source-v1:{hashlib.sha256(encoded).hexdigest()}"


def _bind_tessellation(
    manifest: CadManifest,
    options: CadTessellationOptions,
    meshes: tuple[CadPrototypeMesh, ...],
) -> CadTessellationResult:
    expected_ids = tuple(item.id for item in manifest.prototypes)
    actual_ids = tuple(item.prototype_id for item in sorted(meshes, key=lambda item: item.prototype_id))
    if actual_ids != expected_ids:
        raise CadValidationError("prototype meshes do not exactly cover the manifest", code="cad.tessellation.prototype_mismatch")
    prototype_ids = set(expected_ids)
    for mesh in meshes:
        for owner in (*mesh.tessellation.face_owners, *mesh.tessellation.edge_owners):
            if owner.document_id != manifest.document_id:
                raise CadValidationError("mesh owner belongs to another document", code="cad.tessellation.owner_mismatch")
        if mesh.prototype_id not in prototype_ids:
            raise CadValidationError("mesh prototype is not in the manifest", code="cad.tessellation.prototype_mismatch")
    return CadTessellationResult(_source_identity_for_manifest(manifest), options, tuple(meshes))


@dataclass(frozen=True, slots=True)
class CadAssetWriteReport:
    source_document_id: str
    mode: Literal["preserve", "translate"]
    source_format: Literal["step", "iges", "brep"]
    target_format: Literal["step", "iges", "brep"]
    source_length_unit: LengthUnit | Literal["unknown"]
    target_length_unit: LengthUnit | Literal["unknown"]
    backend_id: Literal["occt"]
    backend_version: str
    backend_compatibility_version: Literal[1]
    binding_version: str | None
    occt_version: str | None
    output_sha256: str
    byte_identical: bool
    source_topology_counts: Mapping[str, int]
    output_topology_counts: Mapping[str, int]
    healing_applied: bool
    geometry_changed: bool
    exported_entities: tuple[CadEntityRef, ...]
    unsupported_entities: tuple[CadEntityRef, ...]
    approximations: tuple[str, ...]
    metadata_losses: tuple[str, ...]
    diagnostics: tuple[CadDiagnostic, ...]
    execution_mode: Literal["preserve_copy", "provider_translation"]

    def __post_init__(self) -> None:
        if not _DOCUMENT_ID_RE.fullmatch(self.source_document_id):
            raise ValueError("source_document_id is invalid")
        if self.mode not in {"preserve", "translate"} or self.source_format not in _SOURCE_FORMATS or self.target_format not in _SOURCE_FORMATS:
            raise ValueError("write report mode or format is invalid")
        for field_name in ("source_length_unit", "target_length_unit"):
            value = getattr(self, field_name)
            if value != "unknown":
                object.__setattr__(self, field_name, _normalize_length_unit(value))
        if self.backend_id != "occt" or self.backend_compatibility_version != 1:
            raise ValueError("write report backend identity is incompatible")
        _require_nonempty_string(self.backend_version, "backend_version")
        if not _SHA256_RE.fullmatch(self.output_sha256):
            raise ValueError("output_sha256 must be lower-case SHA-256")
        for field_name in ("byte_identical", "healing_applied", "geometry_changed"):
            _require_bool(getattr(self, field_name), field_name)
        object.__setattr__(self, "source_topology_counts", _normalize_topology_counts(self.source_topology_counts))
        object.__setattr__(self, "output_topology_counts", _normalize_topology_counts(self.output_topology_counts))
        for field_name in ("exported_entities", "unsupported_entities"):
            object.__setattr__(self, field_name, tuple(sorted(set(getattr(self, field_name)), key=_entity_sort_key)))
        for field_name in ("approximations", "metadata_losses"):
            values = tuple(sorted(set(getattr(self, field_name))))
            if any(not isinstance(item, str) or not item for item in values):
                raise ValueError(f"{field_name} must contain non-empty strings")
            object.__setattr__(self, field_name, values)
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        if self.mode == "preserve":
            if self.execution_mode != "preserve_copy" or not self.byte_identical or self.healing_applied or self.geometry_changed:
                raise ValueError("preserve report invariants are inconsistent")
            if self.source_format != self.target_format or dict(self.source_topology_counts) != dict(self.output_topology_counts):
                raise ValueError("preserve report format/topology must be unchanged")
            if self.approximations or self.metadata_losses or self.binding_version is not None or self.occt_version is not None:
                raise ValueError("provider-free preserve cannot report provider versions or losses")
        elif self.execution_mode != "provider_translation" or self.byte_identical:
            raise ValueError("translation report invariants are inconsistent")
        elif not self.binding_version or not self.occt_version:
            raise ValueError("provider translation requires binding and OCCT versions")


class CadBackendProtocol(Protocol):
    backend_id: Literal["occt"]
    protocol_version: Literal[1]
    backend_compatibility_version: Literal[1]
    backend_version: str
    capabilities: CadCapabilities

    def read(
        self,
        source_snapshot: pathlib.Path,
        *,
        source_sha256: str,
        source_name: str,
        options: CadReadOptions,
        tessellation_options: CadTessellationOptions | None,
        cancellation: CancellationCheck,
    ) -> "CadDocument": ...

    def tessellate(
        self,
        document: "CadDocument",
        *,
        options: CadTessellationOptions,
        cancellation: CancellationCheck,
    ) -> tuple[CadPrototypeMesh, ...]: ...

    def translate(
        self,
        document: "CadDocument",
        destination_temporary: pathlib.Path,
        *,
        options: CadWriteOptions,
        cancellation: CancellationCheck,
    ) -> CadAssetWriteReport: ...


class CadDocument:
    """Immutable published CAD data plus optional private owned resources."""

    __slots__ = (
        "_backend_state",
        "_borrowers",
        "_close_backend_state",
        "_closed",
        "_condition",
        "_loader",
        "_loader_error",
        "_manifest",
        "_owner_thread_id",
        "_release_requested",
        "_source_snapshot",
        "_tessellation",
        "_tessellation_options",
    )

    def __init__(self) -> None:
        raise TypeError("CadDocument instances are created by protocol factories")

    @classmethod
    def _allocate(
        cls,
        *,
        manifest: CadManifest,
        source_snapshot: pathlib.Path | None,
        backend_state: object | None,
        close_backend_state: Callable[[object], None] | None,
        owner_thread_id: int | None,
    ) -> "CadDocument":
        if not isinstance(manifest, CadManifest):
            raise TypeError("manifest must be CadManifest")
        document = object.__new__(cls)
        document._manifest = manifest
        document._source_snapshot = pathlib.Path(source_snapshot) if source_snapshot is not None else None
        if document._source_snapshot is not None and not document._source_snapshot.is_file():
            raise CadValidationError("source snapshot is not a regular file", code="cad.source.invalid_snapshot")
        document._backend_state = backend_state
        document._close_backend_state = close_backend_state
        document._owner_thread_id = owner_thread_id
        document._closed = backend_state is None
        document._condition = threading.Condition(threading.RLock())
        document._borrowers = 0
        document._release_requested = False
        document._loader = None
        document._loader_error = None
        document._tessellation = None
        document._tessellation_options = None
        return document

    @classmethod
    def _from_backend(
        cls,
        *,
        manifest: CadManifest,
        tessellation_options: CadTessellationOptions | None = None,
        prototype_meshes: tuple[CadPrototypeMesh, ...] = (),
        source_snapshot: pathlib.Path | None = None,
        backend_state: object | None = None,
        close_backend_state: Callable[[object], None] | None = None,
        owner_thread_id: int | None = None,
    ) -> "CadDocument":
        state_parts = (backend_state is not None, close_backend_state is not None, owner_thread_id is not None)
        if any(state_parts) and not all(state_parts):
            raise CadValidationError("backend state, close callback, and owner thread are atomic", code="cad.session.incomplete")
        if close_backend_state is not None and not callable(close_backend_state):
            raise TypeError("close_backend_state must be callable")
        if tessellation_options is None and prototype_meshes:
            raise CadValidationError("prototype meshes require tessellation options", code="cad.tessellation.options_required")
        document = cls._allocate(
            manifest=manifest,
            source_snapshot=source_snapshot,
            backend_state=backend_state,
            close_backend_state=close_backend_state,
            owner_thread_id=owner_thread_id,
        )
        if tessellation_options is not None:
            document._tessellation_options = tessellation_options
            document._tessellation = _bind_tessellation(manifest, tessellation_options, tuple(prototype_meshes))
        return document

    @classmethod
    def _from_preview_artifact(
        cls,
        *,
        manifest: CadManifest,
        tessellation_options: CadTessellationOptions,
        prototype_mesh_loader: Callable[[], tuple[CadPrototypeMesh, ...]],
        source_snapshot: pathlib.Path | None = None,
    ) -> "CadDocument":
        if not isinstance(tessellation_options, CadTessellationOptions) or not callable(prototype_mesh_loader):
            raise TypeError("artifact factory requires normalized options and a callable loader")
        document = cls._allocate(
            manifest=manifest,
            source_snapshot=source_snapshot,
            backend_state=None,
            close_backend_state=None,
            owner_thread_id=None,
        )
        document._tessellation_options = tessellation_options
        document._loader = prototype_mesh_loader
        return document

    @property
    def manifest(self) -> CadManifest:
        return self._manifest

    def _ensure_preview_loaded(self) -> None:
        if self._loader is None:
            if self._loader_error is not None:
                raise self._loader_error
            return
        with self._condition:
            if self._loader is None:
                if self._loader_error is not None:
                    raise self._loader_error
                return
            loader = self._loader
            try:
                meshes = tuple(loader())
                assert self._tessellation_options is not None
                self._tessellation = _bind_tessellation(self._manifest, self._tessellation_options, meshes)
            except CadArtifactError as error:
                self._loader_error = error
                raise
            except Exception as error:
                wrapped = CadArtifactError(
                    "preview prototype arrays are invalid",
                    code="cad.preview.load_failed",
                    diagnostic=CadDiagnostic(
                        "cad.preview.load_failed",
                        "error",
                        "preview prototype arrays are invalid",
                        details={"cause": f"{type(error).__name__}: {error}"},
                    ),
                )
                self._loader_error = wrapped
                raise wrapped from error
            finally:
                self._loader = None

    @property
    def tessellation(self) -> CadTessellationResult | None:
        self._ensure_preview_loaded()
        return self._tessellation

    @property
    def prototype_meshes(self) -> tuple[CadPrototypeMesh, ...]:
        result = self.tessellation
        return () if result is None else result.prototype_meshes

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def source_available(self) -> bool:
        with self._condition:
            return self._source_snapshot is not None and not self._release_requested

    @property
    def owner_thread_id(self) -> int | None:
        return self._owner_thread_id

    def close(self) -> None:
        if self._backend_state is None:
            self._closed = True
            return
        if self._owner_thread_id != threading.get_ident():
            raise CadOperationError("native session must close on its owner thread", code="cad.session.wrong_thread")
        state = self._backend_state
        callback = self._close_backend_state
        assert callback is not None
        callback(state)
        self._backend_state = None
        self._close_backend_state = None
        self._owner_thread_id = None
        self._closed = True

    def release_source(self) -> None:
        with self._condition:
            if self._source_snapshot is None:
                self._release_requested = True
                return
            self._release_requested = True
            while self._borrowers:
                self._condition.wait()
            source = self._source_snapshot
            self._source_snapshot = None
        try:
            source.unlink(missing_ok=True)
        except OSError as error:
            raise CadOperationError("owned source snapshot could not be released", code="cad.source.release_failed") from error

    def _backend_state_for(self, backend_id: str) -> object | None:
        if backend_id != self._manifest.backend_id:
            raise CadValidationError("backend id does not own this document", code="cad.backend.id_mismatch")
        if self._backend_state is None or self._closed:
            return None
        if self._owner_thread_id != threading.get_ident():
            raise CadOperationError("native session is thread-affine", code="cad.session.wrong_thread")
        return self._backend_state

    @contextmanager
    def _borrow_source_snapshot(self) -> ContextManager[pathlib.Path]:
        with self._condition:
            if self._source_snapshot is None or self._release_requested:
                raise CadOperationError("retained source is unavailable", code="cad.source.unavailable")
            self._borrowers += 1
            source = self._source_snapshot
        try:
            yield source
        finally:
            with self._condition:
                self._borrowers -= 1
                if self._borrowers == 0:
                    self._condition.notify_all()

    def __enter__(self) -> "CadDocument":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def __reduce_ex__(self, protocol: int) -> Any:
        if self._backend_state is not None:
            raise CadOperationError("live provider state cannot be pickled", code="cad.document.live_pickle_forbidden")
        raise CadOperationError("CadDocument persistence uses the preview artifact codec", code="cad.document.pickle_unsupported")
