"""Deterministic, backend-free persistence for CAD preview records."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import stat
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Sequence, TypeAlias

import numpy as np

from .cad import (
    CadArtifactError,
    CadDiagnostic,
    CadDocument,
    CadEntityRef,
    CadManifest,
    CadOccurrenceRecord,
    CadPrototypeMesh,
    CadPrototypeRecord,
    CadReadOptions,
    CadShapeRecord,
    CadTessellation,
    CadTessellationOptions,
    CadTessellationResult,
    CancellationCheck,
    _bind_tessellation,
    _source_identity_for_manifest,
)
from .cad_operations import (
    _check_cancelled,
    _cleanup_after_failure,
    _copy_source_snapshot,
    _file_identity,
    _path_handle_anchor,
    _stat_signature,
)

__all__ = ["open_preview_artifact", "write_preview_artifact"]

PathLike: TypeAlias = str | os.PathLike[str]

_SCHEMA_NAME = "anyfileio.cad-preview"
_SCHEMA_VERSION = 1
_PROTOCOL_VERSION = 1
_CACHE_PREFIX = "cad-preview-key-v1:"
_BUFFER_SIZE = 1024 * 1024
_MAX_UINT64 = (1 << 64) - 1

# Local reader policy.  These are deliberately not schema-validity limits.
_MAX_ARTIFACT_BYTES = 1_073_741_824
_MAX_MANIFEST_BYTES = 8_388_608
_MAX_NPY_HEADER_BYTES = 65_536
_MAX_MEMBER_BYTES = 268_435_456
_MAX_STORED_MEMBER_BYTES = 805_306_368
_MAX_ARRAY_BYTES = 268_435_456
_MAX_ARRAY_AGGREGATE_BYTES = 805_306_368
_MAX_MEMBER_COUNT = 16_384
_MAX_ARRAY_ELEMENTS = 67_108_864
_MAX_ARRAY_AGGREGATE_ELEMENTS = 134_217_728
_MAX_JSON_DEPTH = 64

_READER_LIMITS = {
    "artifact_bytes": _MAX_ARTIFACT_BYTES,
    "manifest_bytes": _MAX_MANIFEST_BYTES,
    "npy_header_bytes": _MAX_NPY_HEADER_BYTES,
    "member_bytes": _MAX_MEMBER_BYTES,
    "aggregate_stored_member_bytes": _MAX_STORED_MEMBER_BYTES,
    "array_data_bytes": _MAX_ARRAY_BYTES,
    "aggregate_array_bytes": _MAX_ARRAY_AGGREGATE_BYTES,
    "member_count": _MAX_MEMBER_COUNT,
    "array_element_count": _MAX_ARRAY_ELEMENTS,
    "aggregate_array_elements": _MAX_ARRAY_AGGREGATE_ELEMENTS,
    "json_depth": _MAX_JSON_DEPTH,
}

_OCCURRENCE_MEMBERS = (
    "occurrences/accumulated_transforms.npy",
    "occurrences/local_transforms.npy",
    "occurrences/parent_ids.npy",
    "occurrences/prototype_ids.npy",
    "occurrences/visibility.npy",
)


@dataclass(frozen=True, slots=True)
class _ArraySpec:
    dtype: np.dtype[Any] | tuple[np.dtype[Any], ...]
    shape: tuple[int | None, ...]


@dataclass(frozen=True, slots=True)
class _ZipEntry:
    name: str
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class _OpenedArtifact:
    path: pathlib.Path
    manifest_bytes: bytes
    manifest_payload: Mapping[str, Any]
    manifest: CadManifest
    options: CadTessellationOptions
    inventory: tuple[str, ...]
    entries: Mapping[str, str]
    mesh_payloads: tuple[Mapping[str, Any], ...]


def _artifact_error(message: str, *, code: str = "cad.artifact.invalid", **details: Any) -> CadArtifactError:
    return CadArtifactError(
        message,
        code=code,
        diagnostic=CadDiagnostic(code, "error", message, details=details),
    )


def _resource_limit(resource: str, limit: int, observed: int) -> CadArtifactError:
    return _artifact_error(
        "CAD preview exceeds the local reader resource policy",
        code="cad.preview.resource_limit",
        resource=resource,
        limit=limit,
        observed=observed,
    )


def _require_at_most(resource: str, observed: int, limit: int) -> None:
    if observed > limit:
        raise _resource_limit(resource, limit, observed)


def _require_reader_limit(resource: str, observed: int) -> None:
    _require_at_most(resource, observed, _READER_LIMITS[resource])


def _checked_reader_add(resource: str, total: int, value: int) -> int:
    return _checked_add(total, value, _READER_LIMITS[resource], resource)


def _checked_reader_product(resource: str, values: Sequence[int]) -> int:
    return _checked_product(values, _READER_LIMITS[resource], resource)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _artifact_error("CAD preview JSON is not canonicalizable") from error


def _strict_json_equal(left: Any, right: Any) -> bool:
    """Compare canonical JSON values without Python's bool/int/float aliasing."""

    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return (
            set(left) == set(right)
            and all(_strict_json_equal(left[key], right[key]) for key in left)
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return bool(left == right)


def _require_strict_projection(actual: Any, expected: Any, name: str) -> None:
    if (
        not _strict_json_equal(actual, expected)
        or _canonical_json(actual) != _canonical_json(expected)
    ):
        raise _artifact_error(f"{name} projection is noncanonical or type-inexact")


def _require_exact_int(value: Any, expected: int, name: str) -> None:
    if type(value) is not int or value != expected:
        raise _artifact_error(f"{name} must be integer {expected}")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _preflight_json_depth(data: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            _require_reader_limit("json_depth", depth)
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise _artifact_error("manifest.json has unbalanced containers")
    if in_string or depth != 0:
        raise _artifact_error("manifest.json has an unterminated string or container")


def _decode_canonical_json(data: bytes) -> Mapping[str, Any]:
    _preflight_json_depth(data)
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, CadArtifactError):
            raise
        raise _artifact_error("manifest.json is invalid") from error
    if not isinstance(value, Mapping):
        raise _artifact_error("manifest.json must contain an object")
    if _canonical_json(value) != data:
        raise _artifact_error("manifest.json is not canonical")
    return value


def _require_keys(value: Any, keys: Iterable[str], name: str) -> Mapping[str, Any]:
    expected = set(keys)
    if not isinstance(value, Mapping) or set(value) != expected:
        raise _artifact_error(f"{name} must contain exactly {sorted(expected)!r}")
    return value


def _as_tuple(value: Any, name: str) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise _artifact_error(f"{name} must be a JSON array")
    return tuple(value)


def _mapping_json(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _mapping_json(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _entity_payload(entity: CadEntityRef) -> dict[str, Any]:
    return {
        "document_id": entity.document_id,
        "kind": entity.kind,
        "local_id": entity.local_id,
    }


def _entity_from_payload(value: Any) -> CadEntityRef:
    item = _require_keys(value, ("document_id", "kind", "local_id"), "entity reference")
    return CadEntityRef(item["document_id"], item["kind"], item["local_id"])


def _diagnostic_payload(value: CadDiagnostic) -> dict[str, Any]:
    return {
        "code": value.code,
        "severity": value.severity,
        "message": value.message,
        "entities": [_entity_payload(item) for item in value.entities],
        "details": _mapping_json(value.details),
    }


def _diagnostic_from_payload(value: Any) -> CadDiagnostic:
    item = _require_keys(value, ("code", "severity", "message", "entities", "details"), "diagnostic")
    if not isinstance(item["details"], Mapping):
        raise _artifact_error("diagnostic details must be an object")
    return CadDiagnostic(
        item["code"],
        item["severity"],
        item["message"],
        tuple(_entity_from_payload(entity) for entity in _as_tuple(item["entities"], "diagnostic entities")),
        item["details"],
    )


def _read_options_payload(options: CadReadOptions) -> dict[str, Any]:
    return {
        "mode": options.mode,
        "retain_source": options.retain_source,
        "source_length_unit_override": options.source_length_unit_override,
        "heal": options.heal,
    }


def _read_options_from_payload(value: Any) -> CadReadOptions:
    item = _require_keys(
        value,
        ("mode", "retain_source", "source_length_unit_override", "heal"),
        "read options",
    )
    return CadReadOptions(item["mode"], item["retain_source"], item["source_length_unit_override"], item["heal"])


def _tessellation_options_payload(options: CadTessellationOptions) -> dict[str, Any]:
    return {
        "linear_deflection": options.linear_deflection,
        "angular_deflection": options.angular_deflection,
        "relative_deflection": options.relative_deflection,
        "parallel": options.parallel,
        "include_edges": options.include_edges,
        "generate_normals": options.generate_normals,
        "precision_policy": options.precision_policy,
    }


def _tessellation_options_from_payload(value: Any) -> CadTessellationOptions:
    item = _require_keys(
        value,
        (
            "linear_deflection",
            "angular_deflection",
            "relative_deflection",
            "parallel",
            "include_edges",
            "generate_normals",
            "precision_policy",
        ),
        "tessellation options",
    )
    return CadTessellationOptions(
        item["linear_deflection"],
        item["angular_deflection"],
        item["relative_deflection"],
        item["parallel"],
        item["include_edges"],
        item["generate_normals"],
        item["precision_policy"],
    )


def _cache_payload(manifest: CadManifest, options: CadTessellationOptions, source_identity: str) -> dict[str, Any]:
    long_payload = {
        "source_sha256": manifest.source_sha256,
        "source_name": manifest.source_name,
        "source_format": manifest.source_format,
        "effective_source_length_unit": manifest.source_length_unit,
        "normalized_read_options": _read_options_payload(manifest.normalized_read_options),
        "backend_id": manifest.backend_id,
        "backend_version": manifest.backend_version,
        "backend_compatibility_version": manifest.backend_compatibility_version,
        "binding_distribution": manifest.binding_distribution,
        "binding_version": manifest.binding_version,
        "occt_version": manifest.occt_version,
        "source_identity": source_identity,
        "normalized_tessellation_options": _tessellation_options_payload(options),
        "preview_artifact_schema_name": _SCHEMA_NAME,
        "preview_artifact_schema_version": _SCHEMA_VERSION,
    }
    cache_id = _CACHE_PREFIX + hashlib.sha256(_canonical_json(long_payload)).hexdigest()
    return {
        "id": cache_id,
        "source_sha256": manifest.source_sha256,
        "source_name": manifest.source_name,
        "source_format": manifest.source_format,
        "effective_source_length_unit": manifest.source_length_unit,
        "read_options": long_payload["normalized_read_options"],
        "tessellation_options": long_payload["normalized_tessellation_options"],
        "backend_id": manifest.backend_id,
        "backend_version": manifest.backend_version,
        "backend_compatibility_version": manifest.backend_compatibility_version,
        "binding_distribution": manifest.binding_distribution,
        "binding_version": manifest.binding_version,
        "occt_version": manifest.occt_version,
        "source_identity": source_identity,
        "artifact_schema": _SCHEMA_NAME,
        "artifact_version": _SCHEMA_VERSION,
    }


def _prototype_payload(value: CadPrototypeRecord) -> dict[str, Any]:
    return {
        "id": value.id,
        "cad_ref": _entity_payload(value.cad_ref),
        "name": value.name,
        "shape_type": value.shape_type,
        "local_bounds_m": None if value.local_bounds_m is None else list(value.local_bounds_m),
        "topology_counts": dict(value.topology_counts),
    }


def _prototype_from_payload(value: Any) -> CadPrototypeRecord:
    item = _require_keys(
        value,
        ("id", "cad_ref", "name", "shape_type", "local_bounds_m", "topology_counts"),
        "prototype",
    )
    if not isinstance(item["topology_counts"], Mapping):
        raise _artifact_error("prototype topology_counts must be an object")
    bounds = None if item["local_bounds_m"] is None else tuple(_as_tuple(item["local_bounds_m"], "prototype bounds"))
    return CadPrototypeRecord(
        item["id"],
        _entity_from_payload(item["cad_ref"]),
        item["name"],
        item["shape_type"],
        bounds,
        item["topology_counts"],
    )


def _shape_payload(value: CadShapeRecord) -> dict[str, Any]:
    return {
        "cad_ref": _entity_payload(value.cad_ref),
        "prototype_id": value.prototype_id,
        "occurrence_id": value.occurrence_id,
        "parent_ref": None if value.parent_ref is None else _entity_payload(value.parent_ref),
        "name": value.name,
        "shape_type": value.shape_type,
        "prototype_local_bounds_m": None if value.prototype_local_bounds_m is None else list(value.prototype_local_bounds_m),
        "world_bounds_m": None if value.world_bounds_m is None else list(value.world_bounds_m),
        "color_rgba": None if value.color_rgba is None else list(value.color_rgba),
        "layers": list(value.layers),
    }


def _shape_from_payload(value: Any) -> CadShapeRecord:
    item = _require_keys(
        value,
        (
            "cad_ref",
            "prototype_id",
            "occurrence_id",
            "parent_ref",
            "name",
            "shape_type",
            "prototype_local_bounds_m",
            "world_bounds_m",
            "color_rgba",
            "layers",
        ),
        "shape",
    )
    return CadShapeRecord(
        _entity_from_payload(item["cad_ref"]),
        item["prototype_id"],
        item["occurrence_id"],
        None if item["parent_ref"] is None else _entity_from_payload(item["parent_ref"]),
        item["name"],
        item["shape_type"],
        None if item["prototype_local_bounds_m"] is None else tuple(_as_tuple(item["prototype_local_bounds_m"], "shape local bounds")),
        None if item["world_bounds_m"] is None else tuple(_as_tuple(item["world_bounds_m"], "shape world bounds")),
        None if item["color_rgba"] is None else tuple(_as_tuple(item["color_rgba"], "shape color")),
        tuple(_as_tuple(item["layers"], "shape layers")),
    )


def _document_payload(manifest: CadManifest) -> dict[str, Any]:
    occurrence_payloads = []
    for row, occurrence in enumerate(manifest.occurrences):
        occurrence_payloads.append(
            {
                "id": occurrence.id,
                "cad_ref": _entity_payload(occurrence.cad_ref),
                "array_row": row,
                "world_bounds_m": None if occurrence.world_bounds_m is None else list(occurrence.world_bounds_m),
                "name": occurrence.name,
            }
        )
    return {
        "document_id": manifest.document_id,
        "source_sha256": manifest.source_sha256,
        "source_name": manifest.source_name,
        "source_format": manifest.source_format,
        "source_length_unit": manifest.source_length_unit,
        "source_to_metre_scale": manifest.source_to_metre_scale,
        "internal_length_unit": manifest.internal_length_unit,
        "root_occurrence_ids": list(manifest.root_occurrence_ids),
        "prototypes": [_prototype_payload(item) for item in manifest.prototypes],
        "occurrences": occurrence_payloads,
        "shapes": [_shape_payload(item) for item in manifest.shapes],
        "world_bounds_m": None if manifest.world_bounds_m is None else list(manifest.world_bounds_m),
        "topology_counts": dict(manifest.topology_counts),
        "external_references": list(manifest.external_references),
        "diagnostics": [_diagnostic_payload(item) for item in manifest.diagnostics],
        "normalized_read_options": _read_options_payload(manifest.normalized_read_options),
        "backend_id": manifest.backend_id,
        "backend_version": manifest.backend_version,
        "backend_compatibility_version": manifest.backend_compatibility_version,
        "binding_distribution": manifest.binding_distribution,
        "binding_version": manifest.binding_version,
        "occt_version": manifest.occt_version,
    }


def _normalize_array(value: Any) -> np.ndarray[Any, Any]:
    array = np.asarray(value)
    if array.dtype.hasobject or array.dtype.byteorder not in ("=", "|"):
        array = array.astype(array.dtype.newbyteorder("="), copy=True)
    return np.ascontiguousarray(array)


def _npy_bytes(value: Any, cancellation: CancellationCheck = None) -> bytes:
    _check_cancelled(cancellation)
    array = _normalize_array(value)
    _check_cancelled(cancellation)
    if array.dtype.hasobject:
        raise _artifact_error("object arrays are forbidden")
    _require_reader_limit("array_element_count", int(array.size))
    _require_reader_limit("array_data_bytes", int(array.nbytes))
    stream = io.BytesIO()
    np.lib.format.write_array(stream, array, version=(2, 0), allow_pickle=False)
    _check_cancelled(cancellation)
    data = stream.getvalue()
    _preflight_npy_bytes(data)
    return data


def _checked_add(total: int, value: int, limit: int, resource: str) -> int:
    observed = total + value
    if observed > limit:
        raise _resource_limit(resource, limit, observed)
    return observed


def _checked_product(values: Sequence[int], limit: int, resource: str) -> int:
    total = 1
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _artifact_error("NPY shape contains an invalid dimension")
        if value and total > limit // value:
            raise _resource_limit(resource, limit, limit + 1)
        total *= value
    return total


def _npy_header(data: bytes) -> tuple[np.dtype[Any], tuple[int, ...], bool, int]:
    stream = io.BytesIO(data)
    try:
        version = np.lib.format.read_magic(stream)
        if version != (2, 0):
            raise _artifact_error("NPY member must use format 2.0")
        size_bytes = stream.read(4)
        if len(size_bytes) != 4:
            raise _artifact_error("NPY header length is truncated")
        declared_header_bytes = struct.unpack("<I", size_bytes)[0]
        _require_reader_limit("npy_header_bytes", declared_header_bytes)
        if declared_header_bytes > len(data) - stream.tell():
            raise _artifact_error("NPY header is truncated")
        stream.seek(8)
        shape, fortran_order, dtype = np.lib.format.read_array_header_2_0(
            stream,
            max_header_size=_MAX_NPY_HEADER_BYTES,
        )
    except (ValueError, EOFError) as error:
        if isinstance(error, CadArtifactError):
            raise
        raise _artifact_error("NPY header is invalid") from error
    return np.dtype(dtype), tuple(shape), bool(fortran_order), stream.tell()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(min(_BUFFER_SIZE, size - len(chunks)))
        if not block:
            break
        chunks.extend(block)
    if len(chunks) != size:
        raise _artifact_error("NPY member is truncated")
    return bytes(chunks)


def _preflight_npy_stream(stream: BinaryIO, total_size: int) -> tuple[np.dtype[Any], tuple[int, ...], int, int]:
    prefix = _read_exact(stream, 12)
    if prefix[:6] != b"\x93NUMPY" or prefix[6:8] != b"\x02\x00":
        raise _artifact_error("NPY member must use format 2.0")
    declared_header_bytes = struct.unpack("<I", prefix[8:12])[0]
    _require_reader_limit("npy_header_bytes", declared_header_bytes)
    if declared_header_bytes > total_size - 12:
        raise _artifact_error("NPY header is truncated")
    header = _read_exact(stream, declared_header_bytes)
    dtype, shape, fortran_order, offset = _npy_header(prefix + header)
    if dtype.hasobject or fortran_order or dtype.byteorder not in ("=", "|"):
        raise _artifact_error("NPY dtype or storage order is invalid")
    count = _checked_reader_product("array_element_count", shape)
    size = _checked_reader_product("array_data_bytes", (count, dtype.itemsize))
    if offset + size != total_size:
        raise _artifact_error("NPY member length disagrees with its header")
    return dtype, shape, count, size


def _preflight_npy_bytes(data: bytes) -> tuple[np.dtype[Any], tuple[int, ...], int, int]:
    return _preflight_npy_stream(io.BytesIO(data), len(data))


def _preflight_npy_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[np.dtype[Any], tuple[int, ...], int, int]:
    with archive.open(info, "r") as stream:
        return _preflight_npy_stream(stream, info.file_size)


def _read_npy(data: bytes, spec: _ArraySpec, name: str) -> np.ndarray[Any, Any]:
    dtype, shape, _count, _size = _preflight_npy_bytes(data)
    allowed = spec.dtype if isinstance(spec.dtype, tuple) else (spec.dtype,)
    if dtype not in tuple(np.dtype(item) for item in allowed):
        raise _artifact_error(f"{name} has invalid dtype")
    if len(shape) != len(spec.shape) or any(expected is not None and actual != expected for actual, expected in zip(shape, spec.shape)):
        raise _artifact_error(f"{name} has invalid shape")
    try:
        array = np.load(io.BytesIO(data), allow_pickle=False)
    except (ValueError, EOFError) as error:
        raise _artifact_error(f"{name} is not a valid NPY array") from error
    array.flags.writeable = False
    return array


def _mesh_array_names(prototype_id: int, tessellation: CadTessellation) -> dict[str, str | None]:
    prefix = f"prototypes/{prototype_id}"
    return {
        "origin": f"{prefix}/origin.npy",
        "positions": f"{prefix}/positions.npy",
        "triangles": f"{prefix}/triangles.npy",
        "normals": None if tessellation.normals is None else f"{prefix}/normals.npy",
        "face_offsets": f"{prefix}/face_offsets.npy",
        "edge_indices": None if tessellation.edge_indices is None else f"{prefix}/edge_indices.npy",
        "edge_offsets": f"{prefix}/edge_offsets.npy",
    }


def _mesh_payload(mesh: CadPrototypeMesh) -> dict[str, Any]:
    arrays = _mesh_array_names(mesh.prototype_id, mesh.tessellation)
    return {
        "prototype_id": mesh.prototype_id,
        "local_bounds_m": None if mesh.local_bounds_m is None else list(mesh.local_bounds_m),
        "precision": mesh.tessellation.precision,
        "diagnostics": [_diagnostic_payload(item) for item in mesh.diagnostics],
        "face_owners": [_entity_payload(item) for item in mesh.tessellation.face_owners],
        "edge_owners": [_entity_payload(item) for item in mesh.tessellation.edge_owners],
        "arrays": arrays,
    }


def _mesh_members(
    mesh: CadPrototypeMesh,
    cancellation: CancellationCheck = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    prefix = f"prototypes/{mesh.prototype_id}"
    arrays = _mesh_array_names(mesh.prototype_id, mesh.tessellation)
    values = {
        "origin": mesh.tessellation.origin,
        "positions": mesh.tessellation.positions,
        "triangles": mesh.tessellation.triangles,
        "normals": mesh.tessellation.normals,
        "face_offsets": mesh.tessellation.face_offsets,
        "edge_indices": mesh.tessellation.edge_indices,
        "edge_offsets": mesh.tessellation.edge_offsets,
    }
    members: dict[str, bytes] = {}
    for key, name in arrays.items():
        if name is not None:
            _check_cancelled(cancellation)
            members[name] = _npy_bytes(values[key], cancellation)
    payload = _mesh_payload(mesh)
    return members, payload


def _occurrence_members(
    manifest: CadManifest,
    cancellation: CancellationCheck = None,
) -> dict[str, bytes]:
    _check_cancelled(cancellation)
    occurrences = manifest.occurrences
    encoded_ids = [item.id for item in occurrences]
    encoded_ids.extend(item.prototype_id for item in occurrences)
    encoded_ids.extend(0 if item.parent_id is None else item.parent_id for item in occurrences)
    maximum = max(encoded_ids, default=0)
    if maximum > _MAX_UINT64:
        raise _artifact_error("occurrence id exceeds schema-1 unsigned range")
    dtype = np.uint32 if maximum <= np.iinfo(np.uint32).max else np.uint64
    values = {
        "occurrences/prototype_ids.npy": np.asarray([item.prototype_id for item in occurrences], dtype=dtype),
        "occurrences/parent_ids.npy": np.asarray([0 if item.parent_id is None else item.parent_id for item in occurrences], dtype=dtype),
        "occurrences/local_transforms.npy": np.asarray([item.local_transform for item in occurrences], dtype=np.float64).reshape((len(occurrences), 4, 4)),
        "occurrences/accumulated_transforms.npy": np.asarray([item.accumulated_transform for item in occurrences], dtype=np.float64).reshape((len(occurrences), 4, 4)),
        "occurrences/visibility.npy": np.asarray([item.visible for item in occurrences], dtype=np.bool_),
    }
    result: dict[str, bytes] = {}
    for name, value in values.items():
        _check_cancelled(cancellation)
        result[name] = _npy_bytes(value, cancellation)
    return result


def _manifest_payload(
    manifest: CadManifest,
    options: CadTessellationOptions,
    source_identity: str,
    mesh_payloads: Sequence[Mapping[str, Any]],
    members: Mapping[str, bytes],
) -> dict[str, Any]:
    return {
        "schema": _SCHEMA_NAME,
        "version": _SCHEMA_VERSION,
        "protocol_version": _PROTOCOL_VERSION,
        "backend": {
            "id": manifest.backend_id,
            "version": manifest.backend_version,
            "compatibility_version": manifest.backend_compatibility_version,
            "binding_distribution": manifest.binding_distribution,
            "binding_version": manifest.binding_version,
            "occt_version": manifest.occt_version,
        },
        "cache_key": _cache_payload(manifest, options, source_identity),
        "document": _document_payload(manifest),
        "meshes": list(mesh_payloads),
        "entries": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(members.items())},
    }


def _preflight_members(members: Mapping[str, bytes], manifest_bytes: bytes) -> None:
    if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise _resource_limit("manifest_bytes", _MAX_MANIFEST_BYTES, len(manifest_bytes))
    count = len(members) + 1
    if count > _MAX_MEMBER_COUNT:
        raise _resource_limit("member_count", _MAX_MEMBER_COUNT, count)
    if count > 65_535:
        raise _artifact_error("schema-1 archive would require ZIP64")
    stored_total = 0
    array_bytes = 0
    array_elements = 0
    for name, data in members.items():
        if len(data) > _MAX_MEMBER_BYTES:
            raise _resource_limit("member_bytes", _MAX_MEMBER_BYTES, len(data))
        stored_total = _checked_reader_add("aggregate_stored_member_bytes", stored_total, len(data))
        _dtype, _shape, count_value, bytes_value = _preflight_npy_bytes(data)
        array_elements = _checked_reader_add("aggregate_array_elements", array_elements, count_value)
        array_bytes = _checked_reader_add("aggregate_array_bytes", array_bytes, bytes_value)
        if not _safe_member_name(name):
            raise _artifact_error("schema-1 member name is unsafe")
    names = ["manifest.json", *sorted(members)]
    sizes = [len(manifest_bytes), *(len(members[name]) for name in sorted(members))]
    local_offset = 0
    for name, size in zip(names, sizes):
        encoded = name.encode("ascii")
        if size > 0xFFFF_FFFF or local_offset > 0xFFFF_FFFF:
            raise _artifact_error("schema-1 archive would require ZIP64")
        local_offset += 30 + len(encoded) + size
    central_size = sum(46 + len(name.encode("ascii")) for name in names)
    if local_offset > 0xFFFF_FFFF or central_size > 0xFFFF_FFFF:
        raise _artifact_error("schema-1 archive would require ZIP64")
    total = local_offset + central_size + 22
    if total > _MAX_ARTIFACT_BYTES:
        raise _resource_limit("artifact_bytes", _MAX_ARTIFACT_BYTES, total)


def _safe_member_name(name: str) -> bool:
    if not isinstance(name, str) or not name or "\\" in name or name.startswith("/"):
        return False
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        return False
    return all(component not in {"", ".", ".."} for component in name.split("/"))


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.flag_bits = 0
    info.internal_attr = 0
    info.external_attr = 0o100600 << 16
    info.extra = b""
    info.comment = b""
    return info


def _write_zip(
    path: pathlib.Path,
    identity: tuple[int, int],
    manifest_bytes: bytes,
    members: Mapping[str, bytes],
    cancellation: CancellationCheck,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    path_before = path.lstat()
    if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(path_before.st_mode) or _file_identity(path_before) != identity:
        raise _artifact_error("preview temporary identity changed before writing")
    with path.open("r+b") as stream:
        handle_before = os.fstat(stream.fileno())
        path_opened = path.lstat()
        if (
            _file_identity(handle_before) != identity
            or _file_identity(path_opened) != identity
            or _stat_signature(path_before) != _stat_signature(path_opened)
            or _path_handle_anchor(handle_before) != _path_handle_anchor(path_opened)
        ):
            raise _artifact_error("preview temporary changed while opening for write")
        stream.seek(0)
        stream.truncate(0)
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            archive.comment = b""
            for name, data in (("manifest.json", manifest_bytes), *((key, members[key]) for key in sorted(members))):
                _check_cancelled(cancellation)
                info = _zip_info(name)
                info.file_size = len(data)
                with archive.open(info, "w", force_zip64=False) as writer:
                    view = memoryview(data)
                    for offset in range(0, len(view), _BUFFER_SIZE):
                        writer.write(view[offset:offset + _BUFFER_SIZE])
                        _check_cancelled(cancellation)
        stream.flush()
        os.fsync(stream.fileno())
        handle_after = os.fstat(stream.fileno())
        path_after = path.lstat()
        if (
            _file_identity(handle_after) != identity
            or _file_identity(path_after) != identity
            or stat.S_ISLNK(path_after.st_mode)
            or not stat.S_ISREG(path_after.st_mode)
            or _path_handle_anchor(handle_after) != _path_handle_anchor(path_after)
        ):
            raise _artifact_error("preview temporary changed while writing")
        return _stat_signature(handle_after), _stat_signature(path_after)


def _hash_stream(
    stream: BinaryIO,
    limit: int,
    resource: str,
    cancellation: CancellationCheck = None,
) -> tuple[bytes, str]:
    digest = hashlib.sha256()
    result = bytearray()
    while True:
        block = stream.read(min(_BUFFER_SIZE, limit + 1 - len(result)))
        if not block:
            break
        result.extend(block)
        digest.update(block)
        _check_cancelled(cancellation)
        if len(result) > limit:
            raise _resource_limit(resource, limit, len(result))
    return bytes(result), digest.hexdigest()


def _digest_stream(
    stream: BinaryIO,
    limit: int,
    resource: str,
    cancellation: CancellationCheck = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    observed = 0
    while True:
        block = stream.read(_BUFFER_SIZE)
        if not block:
            break
        observed = _checked_add(observed, len(block), limit, resource)
        digest.update(block)
        _check_cancelled(cancellation)
    return digest.hexdigest(), observed


def _read_at(stream: BinaryIO, offset: int, size: int) -> bytes:
    stream.seek(offset)
    return _read_exact(stream, size)


def _validate_raw_zip(stream: BinaryIO, artifact_size: int, infos: Sequence[zipfile.ZipInfo]) -> None:
    if artifact_size < 22:
        raise _artifact_error("archive has trailing bytes or a noncanonical EOCD")
    eocd = _read_at(stream, artifact_size - 22, 22)
    if eocd[:4] != b"PK\x05\x06":
        raise _artifact_error("archive has trailing bytes or a noncanonical EOCD")
    disk, central_disk, disk_count, total_count, central_size, central_offset, comment_length = struct.unpack_from("<HHHHIIH", eocd, 4)
    if disk_count == 0xFFFF or total_count == 0xFFFF or central_size == 0xFFFF_FFFF or central_offset == 0xFFFF_FFFF:
        raise _artifact_error("ZIP64 is forbidden")
    if (disk, central_disk, disk_count, total_count, comment_length) != (0, 0, len(infos), len(infos), 0):
        raise _artifact_error("archive EOCD is noncanonical")
    if central_offset + central_size != artifact_size - 22:
        raise _artifact_error("archive central directory bounds are invalid")
    expected_local_offset = 0
    for info in infos:
        offset = info.header_offset
        if offset != expected_local_offset or offset + 30 > central_offset:
            raise _artifact_error("archive local header is invalid")
        local = _read_at(stream, offset, 30)
        if local[:4] != b"PK\x03\x04":
            raise _artifact_error("archive local header is invalid")
        version, flags, method, mtime, mdate, crc, compressed, size, name_length, extra_length = struct.unpack_from("<HHHHHIIIHH", local, 4)
        if compressed == 0xFFFF_FFFF or size == 0xFFFF_FFFF:
            raise _artifact_error("ZIP64 is forbidden")
        encoded_name = _read_at(stream, offset + 30, name_length)
        if (
            version != 20
            or flags != 0
            or method != zipfile.ZIP_STORED
            or mtime != 0
            or mdate != 33
            or crc != info.CRC
            or compressed != info.compress_size
            or size != info.file_size
            or extra_length != 0
            or encoded_name != info.filename.encode("ascii")
        ):
            raise _artifact_error("archive local header is noncanonical")
        expected_local_offset = offset + 30 + name_length + size
    if expected_local_offset != central_offset:
        raise _artifact_error("archive local payload boundary is invalid")

    central_cursor = central_offset
    for info in infos:
        fixed = _read_at(stream, central_cursor, 46)
        if fixed[:4] != b"PK\x01\x02":
            raise _artifact_error("archive central header is invalid")
        (
            version_made,
            version_needed,
            flags,
            method,
            mtime,
            mdate,
            crc,
            compressed,
            size,
            name_length,
            extra_length,
            comment_length,
            disk_start,
            internal_attr,
            external_attr,
            local_offset,
        ) = struct.unpack_from("<6H3I5H2I", fixed, 4)
        if compressed == 0xFFFF_FFFF or size == 0xFFFF_FFFF or local_offset == 0xFFFF_FFFF or disk_start == 0xFFFF:
            raise _artifact_error("ZIP64 is forbidden")
        encoded_name = _read_at(stream, central_cursor + 46, name_length)
        if (
            version_made != (3 << 8) | 20
            or version_needed != 20
            or flags != 0
            or method != zipfile.ZIP_STORED
            or mtime != 0
            or mdate != 33
            or crc != info.CRC
            or compressed != info.compress_size
            or size != info.file_size
            or extra_length != 0
            or comment_length != 0
            or disk_start != 0
            or internal_attr != 0
            or external_attr != (0o100600 << 16)
            or local_offset != info.header_offset
            or encoded_name != info.filename.encode("ascii")
        ):
            raise _artifact_error("archive central header is noncanonical")
        central_cursor += 46 + name_length
    if central_cursor != central_offset + central_size:
        raise _artifact_error("archive central directory contains gaps or trailing fields")


def _preflight_eocd(stream: BinaryIO, artifact_size: int) -> None:
    if artifact_size < 22:
        raise _artifact_error("archive has no canonical EOCD")
    eocd = _read_at(stream, artifact_size - 22, 22)
    if eocd[:4] != b"PK\x05\x06":
        raise _artifact_error("archive has trailing bytes or a noncanonical EOCD")
    disk, central_disk, disk_count, total_count, central_size, central_offset, comment_length = struct.unpack_from("<HHHHIIH", eocd, 4)
    if disk_count == 0xFFFF or total_count == 0xFFFF or central_size == 0xFFFF_FFFF or central_offset == 0xFFFF_FFFF:
        raise _artifact_error("ZIP64 is forbidden")
    if disk != 0 or central_disk != 0 or disk_count != total_count or comment_length != 0:
        raise _artifact_error("archive EOCD is noncanonical")
    _require_reader_limit("member_count", total_count)
    if central_offset + central_size != artifact_size - 22:
        raise _artifact_error("archive central directory bounds are invalid")
    if central_size < total_count * 46:
        raise _artifact_error("archive central directory is too short for its member count")
    central_end = central_offset + central_size
    cursor = central_offset
    for _index in range(total_count):
        if cursor + 46 > central_end:
            raise _artifact_error("archive central directory record is truncated")
        fixed = _read_at(stream, cursor, 46)
        if fixed[:4] != b"PK\x01\x02":
            raise _artifact_error("archive central directory record is invalid")
        (
            _version_made,
            _version_needed,
            _flags,
            _method,
            _mtime,
            _mdate,
            _crc,
            compressed_size,
            file_size,
            name_length,
            extra_length,
            member_comment_length,
            disk_start,
            _internal_attr,
            _external_attr,
            local_offset,
        ) = struct.unpack_from("<6H3I5H2I", fixed, 4)
        if (
            compressed_size == 0xFFFF_FFFF
            or file_size == 0xFFFF_FFFF
            or disk_start == 0xFFFF
            or local_offset == 0xFFFF_FFFF
        ):
            raise _artifact_error("ZIP64 is forbidden")
        variable_size = name_length + extra_length + member_comment_length
        if variable_size > central_end - (cursor + 46):
            raise _artifact_error("archive central directory variable fields are truncated")
        cursor += 46 + variable_size
    if cursor != central_end:
        raise _artifact_error("archive central directory count or extent is noncanonical")


def _validate_zip_info(info: zipfile.ZipInfo) -> None:
    if (
        info.compress_type != zipfile.ZIP_STORED
        or info.date_time != (1980, 1, 1, 0, 0, 0)
        or info.create_system != 3
        or info.create_version != 20
        or info.extract_version != 20
        or info.flag_bits != 0
        or info.internal_attr != 0
        or info.external_attr != (0o100600 << 16)
        or info.extra != b""
        or info.comment != b""
        or info.compress_size != info.file_size
    ):
        raise _artifact_error("archive member metadata is noncanonical")


def _read_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    manifest: bool = False,
    cancellation: CancellationCheck = None,
) -> tuple[bytes, str]:
    limit = _MAX_MANIFEST_BYTES if manifest else _MAX_MEMBER_BYTES
    resource = "manifest_bytes" if manifest else "member_bytes"
    with archive.open(info, "r") as stream:
        data, digest = _hash_stream(stream, limit, resource, cancellation)
    if len(data) != info.file_size:
        raise _artifact_error("archive member length changed while reading")
    return data, digest


def _parse_occurrences(
    payloads: Sequence[Any],
    arrays: Mapping[str, np.ndarray[Any, Any]],
    document_id: str,
) -> tuple[CadOccurrenceRecord, ...]:
    count = len(payloads)
    expected_shapes = {
        "occurrences/prototype_ids.npy": (count,),
        "occurrences/parent_ids.npy": (count,),
        "occurrences/local_transforms.npy": (count, 4, 4),
        "occurrences/accumulated_transforms.npy": (count, 4, 4),
        "occurrences/visibility.npy": (count,),
    }
    for name, shape in expected_shapes.items():
        if arrays[name].shape != shape:
            raise _artifact_error(f"{name} row count is invalid")
    id_dtype = arrays["occurrences/prototype_ids.npy"].dtype
    if id_dtype not in (np.dtype(np.uint32), np.dtype(np.uint64)) or arrays["occurrences/parent_ids.npy"].dtype != id_dtype:
        raise _artifact_error("occurrence id arrays must share canonical unsigned dtype")
    records: list[CadOccurrenceRecord] = []
    ids: list[int] = []
    for row, raw in enumerate(payloads):
        item = _require_keys(raw, ("id", "cad_ref", "array_row", "world_bounds_m", "name"), "occurrence")
        _require_exact_int(item["array_row"], row, "occurrence array_row")
        identifier = item["id"]
        if type(identifier) is not int or identifier <= 0 or identifier > _MAX_UINT64:
            raise _artifact_error("occurrence id is outside schema-1 range")
        ids.append(identifier)
        parent_encoded = int(arrays["occurrences/parent_ids.npy"][row])
        records.append(
            CadOccurrenceRecord(
                identifier,
                _entity_from_payload(item["cad_ref"]),
                int(arrays["occurrences/prototype_ids.npy"][row]),
                None if parent_encoded == 0 else parent_encoded,
                arrays["occurrences/local_transforms.npy"][row],
                arrays["occurrences/accumulated_transforms.npy"][row],
                None if item["world_bounds_m"] is None else tuple(_as_tuple(item["world_bounds_m"], "occurrence bounds")),
                item["name"],
                bool(arrays["occurrences/visibility.npy"][row]),
            )
        )
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise _artifact_error("occurrence ids are not unique ascending values")
    maximum = max(
        (*ids, *(item.prototype_id for item in records), *(0 if item.parent_id is None else item.parent_id for item in records)),
        default=0,
    )
    expected_dtype = np.dtype(np.uint32 if maximum <= np.iinfo(np.uint32).max else np.uint64)
    if id_dtype != expected_dtype:
        raise _artifact_error("occurrence id arrays do not use the canonical dtype")
    if any(item.cad_ref.document_id != document_id for item in records):
        raise _artifact_error("occurrence belongs to another document")
    return tuple(records)


def _parse_document(value: Any, arrays: Mapping[str, np.ndarray[Any, Any]]) -> CadManifest:
    keys = (
        "document_id",
        "source_sha256",
        "source_name",
        "source_format",
        "source_length_unit",
        "source_to_metre_scale",
        "internal_length_unit",
        "root_occurrence_ids",
        "prototypes",
        "occurrences",
        "shapes",
        "world_bounds_m",
        "topology_counts",
        "external_references",
        "diagnostics",
        "normalized_read_options",
        "backend_id",
        "backend_version",
        "backend_compatibility_version",
        "binding_distribution",
        "binding_version",
        "occt_version",
    )
    item = _require_keys(value, keys, "document")
    document_id = item["document_id"]
    prototypes = tuple(_prototype_from_payload(raw) for raw in _as_tuple(item["prototypes"], "prototypes"))
    occurrences = _parse_occurrences(_as_tuple(item["occurrences"], "occurrences"), arrays, document_id)
    shapes = tuple(_shape_from_payload(raw) for raw in _as_tuple(item["shapes"], "shapes"))
    if not isinstance(item["topology_counts"], Mapping):
        raise _artifact_error("document topology_counts must be an object")
    return CadManifest(
        document_id,
        item["source_sha256"],
        item["source_name"],
        item["source_format"],
        item["source_length_unit"],
        item["source_to_metre_scale"],
        item["internal_length_unit"],
        tuple(_as_tuple(item["root_occurrence_ids"], "root occurrence ids")),
        prototypes,
        occurrences,
        shapes,
        None if item["world_bounds_m"] is None else tuple(_as_tuple(item["world_bounds_m"], "document bounds")),
        item["topology_counts"],
        tuple(_as_tuple(item["external_references"], "external references")),
        tuple(_diagnostic_from_payload(raw) for raw in _as_tuple(item["diagnostics"], "document diagnostics")),
        _read_options_from_payload(item["normalized_read_options"]),
        item["backend_id"],
        item["backend_version"],
        item["backend_compatibility_version"],
        item["binding_distribution"],
        item["binding_version"],
        item["occt_version"],
    )


def _expected_inventory(meshes: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    names = set(_OCCURRENCE_MEMBERS)
    prototype_ids: list[int] = []
    for raw in meshes:
        item = _require_keys(
            raw,
            ("prototype_id", "local_bounds_m", "precision", "diagnostics", "face_owners", "edge_owners", "arrays"),
            "mesh",
        )
        identifier = item["prototype_id"]
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
            raise _artifact_error("mesh prototype_id is invalid")
        prototype_ids.append(identifier)
        arrays = _require_keys(
            item["arrays"],
            ("origin", "positions", "triangles", "normals", "face_offsets", "edge_indices", "edge_offsets"),
            "mesh arrays",
        )
        prefix = f"prototypes/{identifier}"
        required = {
            "origin": f"{prefix}/origin.npy",
            "positions": f"{prefix}/positions.npy",
            "triangles": f"{prefix}/triangles.npy",
            "face_offsets": f"{prefix}/face_offsets.npy",
            "edge_offsets": f"{prefix}/edge_offsets.npy",
        }
        for key, expected in required.items():
            if arrays[key] != expected:
                raise _artifact_error("mesh array member projection is invalid")
            names.add(expected)
        for key in ("normals", "edge_indices"):
            expected = f"{prefix}/{key}.npy"
            if arrays[key] is not None:
                if arrays[key] != expected:
                    raise _artifact_error("optional mesh member projection is invalid")
                names.add(expected)
    if prototype_ids != sorted(prototype_ids) or len(prototype_ids) != len(set(prototype_ids)):
        raise _artifact_error("mesh descriptors are not unique ascending values")
    return ("manifest.json", *sorted(names))


def _validate_repeated_payload(payload: Mapping[str, Any], manifest: CadManifest, options: CadTessellationOptions) -> None:
    for field_name, expected in (("version", _SCHEMA_VERSION), ("protocol_version", _PROTOCOL_VERSION)):
        _require_exact_int(payload[field_name], expected, field_name)
    if type(payload["schema"]) is not str or payload["schema"] != _SCHEMA_NAME:
        raise _artifact_error("preview schema name is unsupported")
    backend = _require_keys(
        payload["backend"],
        ("id", "version", "compatibility_version", "binding_distribution", "binding_version", "occt_version"),
        "backend",
    )
    expected_backend = {
        "id": manifest.backend_id,
        "version": manifest.backend_version,
        "compatibility_version": manifest.backend_compatibility_version,
        "binding_distribution": manifest.binding_distribution,
        "binding_version": manifest.binding_version,
        "occt_version": manifest.occt_version,
    }
    _require_strict_projection(backend, expected_backend, "backend")
    source_identity = _source_identity_for_manifest(manifest)
    expected_cache = _cache_payload(manifest, options, source_identity)
    cache = _require_keys(payload["cache_key"], expected_cache, "cache_key")
    _require_strict_projection(cache, expected_cache, "cache_key")


def _read_artifact_metadata_view(
    path: pathlib.Path,
    handle: BinaryIO,
    artifact_size: int,
    cancellation: CancellationCheck,
) -> _OpenedArtifact:
    handle.seek(0)
    _artifact_digest, observed = _digest_stream(
        handle,
        _MAX_ARTIFACT_BYTES,
        "artifact_bytes",
        cancellation,
    )
    if observed != artifact_size:
        raise _artifact_error("CAD preview artifact length changed while hashing")
    _check_cancelled(cancellation)
    _preflight_eocd(handle, artifact_size)
    handle.seek(0)
    with zipfile.ZipFile(handle, "r") as archive:
        infos = archive.infolist()
        if archive.comment != b"" or len(infos) > _MAX_MEMBER_COUNT:
            if len(infos) > _MAX_MEMBER_COUNT:
                raise _resource_limit("member_count", _MAX_MEMBER_COUNT, len(infos))
            raise _artifact_error("archive comment is forbidden")
        if not infos or infos[0].filename != "manifest.json":
            raise _artifact_error("manifest.json must be the first member")
        names = tuple(info.filename for info in infos)
        if names != ("manifest.json", *sorted(names[1:])) or len(set(names)) != len(names):
            raise _artifact_error("archive member order or uniqueness is invalid")
        if any(not _safe_member_name(name) for name in names):
            raise _artifact_error("archive contains an unsafe member name")
        stored_total = 0
        for info in infos:
            _validate_zip_info(info)
            if info.filename == "manifest.json":
                _require_reader_limit("manifest_bytes", info.file_size)
            else:
                _require_reader_limit("member_bytes", info.file_size)
                stored_total = _checked_reader_add(
                    "aggregate_stored_member_bytes",
                    stored_total,
                    info.file_size,
                )
        _validate_raw_zip(handle, artifact_size, infos)
        by_name = {info.filename: info for info in infos}
        manifest_bytes, _manifest_digest = _read_member(
            archive,
            by_name["manifest.json"],
            manifest=True,
            cancellation=cancellation,
        )
        payload = _decode_canonical_json(manifest_bytes)
        _require_keys(
            payload,
            ("schema", "version", "protocol_version", "backend", "cache_key", "document", "meshes", "entries"),
            "manifest",
        )
        mesh_payloads = _as_tuple(payload["meshes"], "meshes")
        if names != _expected_inventory(mesh_payloads):
            raise _artifact_error("archive inventory disagrees with the manifest")
        entries = payload["entries"]
        if not isinstance(entries, Mapping) or set(entries) != set(names[1:]):
            raise _artifact_error("manifest entries do not exactly cover non-manifest members")
        document_payload = _require_keys(payload["document"], (
            "document_id", "source_sha256", "source_name", "source_format", "source_length_unit", "source_to_metre_scale",
            "internal_length_unit", "root_occurrence_ids", "prototypes", "occurrences", "shapes", "world_bounds_m",
            "topology_counts", "external_references", "diagnostics", "normalized_read_options", "backend_id", "backend_version",
            "backend_compatibility_version", "binding_distribution", "binding_version", "occt_version"), "document")
        count = len(_as_tuple(document_payload["occurrences"], "occurrences"))
        specs = {
            "occurrences/prototype_ids.npy": _ArraySpec((np.dtype(np.uint32), np.dtype(np.uint64)), (count,)),
            "occurrences/parent_ids.npy": _ArraySpec((np.dtype(np.uint32), np.dtype(np.uint64)), (count,)),
            "occurrences/local_transforms.npy": _ArraySpec(np.dtype(np.float64), (count, 4, 4)),
            "occurrences/accumulated_transforms.npy": _ArraySpec(np.dtype(np.float64), (count, 4, 4)),
            "occurrences/visibility.npy": _ArraySpec(np.dtype(np.bool_), (count,)),
        }
        arrays: dict[str, np.ndarray[Any, Any]] = {}
        array_bytes = 0
        array_elements = 0
        for name in _OCCURRENCE_MEMBERS:
            _check_cancelled(cancellation)
            dtype, shape, count_value, size_value = _preflight_npy_member(archive, by_name[name])
            array_elements = _checked_reader_add("aggregate_array_elements", array_elements, count_value)
            array_bytes = _checked_reader_add("aggregate_array_bytes", array_bytes, size_value)
            allowed = specs[name].dtype if isinstance(specs[name].dtype, tuple) else (specs[name].dtype,)
            if dtype not in tuple(np.dtype(item) for item in allowed) or len(shape) != len(specs[name].shape) or any(
                selected is not None and actual != selected for actual, selected in zip(shape, specs[name].shape)
            ):
                raise _artifact_error(f"{name} has invalid dtype or shape")
        for name in _OCCURRENCE_MEMBERS:
            data, digest = _read_member(
                archive,
                by_name[name],
                cancellation=cancellation,
            )
            if entries[name] != digest:
                raise _artifact_error("occurrence member hash does not match the manifest")
            arrays[name] = _read_npy(data, specs[name], name)
            _check_cancelled(cancellation)
        manifest = _parse_document(payload["document"], arrays)
        _require_strict_projection(payload["document"], _document_payload(manifest), "document")
        cache_payload = _require_keys(payload["cache_key"], (
            "id", "source_sha256", "source_name", "source_format", "effective_source_length_unit", "read_options",
            "tessellation_options", "backend_id", "backend_version", "backend_compatibility_version", "binding_distribution",
            "binding_version", "occt_version", "source_identity", "artifact_schema", "artifact_version"), "cache_key")
        options = _tessellation_options_from_payload(cache_payload["tessellation_options"])
        _validate_repeated_payload(payload, manifest, options)
        if tuple(item.id for item in manifest.prototypes) != tuple(item["prototype_id"] for item in mesh_payloads):
            raise _artifact_error("mesh descriptors do not cover every prototype")
        return _OpenedArtifact(path, manifest_bytes, payload, manifest, options, names, dict(entries), tuple(mesh_payloads))


def _open_artifact_metadata(
    path: pathlib.Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    cancellation: CancellationCheck = None,
) -> _OpenedArtifact:
    try:
        before = path.lstat()
    except OSError as error:
        raise _artifact_error("CAD preview artifact could not be inspected") from error
    if not stat.S_ISREG(before.st_mode):
        raise _artifact_error("CAD preview artifact must be a regular file")
    _require_reader_limit("artifact_bytes", int(before.st_size))
    if expected_identity is not None and _file_identity(before) != expected_identity:
        raise _artifact_error("CAD preview artifact ownership changed before open")
    try:
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            path_opened = path.lstat()
            if (
                _path_handle_anchor(opened_before) != _path_handle_anchor(before)
                or _stat_signature(path_opened) != _stat_signature(before)
                or (expected_identity is not None and _file_identity(opened_before) != expected_identity)
            ):
                raise _artifact_error("CAD preview artifact changed before open")
            result = _read_artifact_metadata_view(
                path,
                handle,
                int(opened_before.st_size),
                cancellation,
            )
            opened_after = os.fstat(handle.fileno())
            path_after = path.lstat()
            if (
                _stat_signature(opened_before) != _stat_signature(opened_after)
                or _stat_signature(path_opened) != _stat_signature(path_after)
                or (expected_identity is not None and _file_identity(opened_after) != expected_identity)
            ):
                raise _artifact_error("CAD preview artifact changed during open")
            return result
    except CadArtifactError:
        raise
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as error:
        raise _artifact_error("CAD preview artifact is invalid") from error


def _prototype_specs(
    mesh: Mapping[str, Any],
    headers: Mapping[str, tuple[np.dtype[Any], tuple[int, ...], int, int]],
) -> Mapping[str, _ArraySpec]:
    array_names = mesh["arrays"]
    positions_name = array_names["positions"]
    positions_dtype, positions_shape, _count, _bytes = headers[positions_name]
    precision = mesh["precision"]
    expected_position_dtype = np.dtype(np.float32 if precision == "float32" else np.float64)
    if precision not in {"float32", "float64"} or positions_dtype != expected_position_dtype or len(positions_shape) != 2 or positions_shape[1] != 3:
        raise _artifact_error("prototype positions disagree with precision")
    vertex_count = positions_shape[0]
    index_dtype = np.dtype(np.uint32 if max(vertex_count - 1, 0) <= np.iinfo(np.uint32).max else np.uint64)
    face_owners = _as_tuple(mesh["face_owners"], "face owners")
    edge_owners = _as_tuple(mesh["edge_owners"], "edge owners")
    specs: dict[str, _ArraySpec] = {
        array_names["origin"]: _ArraySpec(np.dtype(np.float64), (3,)),
        positions_name: _ArraySpec(expected_position_dtype, (None, 3)),
        array_names["triangles"]: _ArraySpec(index_dtype, (None, 3)),
        array_names["face_offsets"]: _ArraySpec(np.dtype(np.int64), (len(face_owners) + 1,)),
        array_names["edge_offsets"]: _ArraySpec(np.dtype(np.int64), (len(edge_owners) + 1,)),
    }
    if array_names["normals"] is not None:
        specs[array_names["normals"]] = _ArraySpec(np.dtype(np.float32), (vertex_count, 3))
    if array_names["edge_indices"] is not None:
        specs[array_names["edge_indices"]] = _ArraySpec(index_dtype, (None, 2))
    return specs


def _load_prototype_meshes(
    expected: _OpenedArtifact,
    cancellation: CancellationCheck = None,
) -> tuple[CadPrototypeMesh, ...]:
    try:
        path_before = expected.path.lstat()
        if not stat.S_ISREG(path_before.st_mode):
            raise _artifact_error("CAD preview artifact must remain a regular file")
        _require_reader_limit("artifact_bytes", int(path_before.st_size))
        with expected.path.open("rb") as handle:
            handle_before = os.fstat(handle.fileno())
            path_opened = expected.path.lstat()
            if (
                _path_handle_anchor(handle_before) != _path_handle_anchor(path_before)
                or _stat_signature(path_opened) != _stat_signature(path_before)
            ):
                raise _artifact_error("CAD preview artifact changed before lazy loading")
            current = _read_artifact_metadata_view(
                expected.path,
                handle,
                int(handle_before.st_size),
                cancellation,
            )
            if (
                current.manifest_bytes != expected.manifest_bytes
                or current.inventory != expected.inventory
                or not _strict_json_equal(current.entries, expected.entries)
            ):
                raise _artifact_error("CAD preview artifact changed before lazy loading")
            handle.seek(0)
            archive = zipfile.ZipFile(handle, "r")
            try:
                by_name = {info.filename: info for info in archive.infolist()}
                headers: dict[str, tuple[np.dtype[Any], tuple[int, ...], int, int]] = {}
                array_bytes = 0
                array_elements = 0
                for mesh in current.mesh_payloads:
                    for name in mesh["arrays"].values():
                        if name is None:
                            continue
                        _check_cancelled(cancellation)
                        header = _preflight_npy_member(archive, by_name[name])
                        _dtype, _shape, count, size = header
                        array_elements = _checked_reader_add("aggregate_array_elements", array_elements, count)
                        array_bytes = _checked_reader_add("aggregate_array_bytes", array_bytes, size)
                        headers[name] = header
                specifications = {
                    mesh["prototype_id"]: _prototype_specs(mesh, headers)
                    for mesh in current.mesh_payloads
                }
                meshes: list[CadPrototypeMesh] = []
                prototype_by_id = {item.id: item for item in current.manifest.prototypes}
                for mesh in current.mesh_payloads:
                    arrays = mesh["arrays"]
                    specs = specifications[mesh["prototype_id"]]
                    decoded: dict[str, np.ndarray[Any, Any]] = {}
                    for name, spec in specs.items():
                        data, digest = _read_member(
                            archive,
                            by_name[name],
                            cancellation=cancellation,
                        )
                        if current.entries[name] != digest:
                            raise _artifact_error("prototype member hash does not match the manifest")
                        decoded[name] = _read_npy(data, spec, name)
                        del data
                        _check_cancelled(cancellation)
                    tessellation = CadTessellation(
                        decoded[arrays["origin"]],
                        decoded[arrays["positions"]],
                        decoded[arrays["triangles"]],
                        None if arrays["normals"] is None else decoded[arrays["normals"]],
                        tuple(_entity_from_payload(item) for item in _as_tuple(mesh["face_owners"], "face owners")),
                        decoded[arrays["face_offsets"]],
                        None if arrays["edge_indices"] is None else decoded[arrays["edge_indices"]],
                        tuple(_entity_from_payload(item) for item in _as_tuple(mesh["edge_owners"], "edge owners")),
                        decoded[arrays["edge_offsets"]],
                        mesh["precision"],
                    )
                    bounds = None if mesh["local_bounds_m"] is None else tuple(_as_tuple(mesh["local_bounds_m"], "mesh bounds"))
                    prototype_id = mesh["prototype_id"]
                    if prototype_id not in prototype_by_id or bounds != prototype_by_id[prototype_id].local_bounds_m:
                        raise _artifact_error("mesh bounds disagree with its prototype")
                    result_mesh = CadPrototypeMesh(
                        prototype_id,
                        tessellation,
                        bounds,
                        tuple(_diagnostic_from_payload(item) for item in _as_tuple(mesh["diagnostics"], "mesh diagnostics")),
                    )
                    _require_strict_projection(mesh, _mesh_payload(result_mesh), "mesh")
                    meshes.append(result_mesh)
                    decoded.clear()
            finally:
                archive.close()
            handle_after = os.fstat(handle.fileno())
            path_after = expected.path.lstat()
            if (
                _stat_signature(handle_before) != _stat_signature(handle_after)
                or _stat_signature(path_opened) != _stat_signature(path_after)
            ):
                raise _artifact_error("CAD preview artifact changed during lazy loading")
            bound = _bind_tessellation(current.manifest, current.options, tuple(meshes))
            _validate_mesh_owners(current.manifest, bound.prototype_meshes)
            return bound.prototype_meshes
    except CadArtifactError:
        raise
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as error:
        raise _artifact_error("CAD preview prototype arrays are invalid") from error


def _validate_mesh_owners(manifest: CadManifest, meshes: Sequence[CadPrototypeMesh]) -> None:
    shapes_by_prototype = {
        prototype.id: {shape.cad_ref for shape in manifest.shapes if shape.prototype_id == prototype.id}
        for prototype in manifest.prototypes
    }
    for mesh in meshes:
        known = shapes_by_prototype.get(mesh.prototype_id, set())
        owners = (*mesh.tessellation.face_owners, *mesh.tessellation.edge_owners)
        if any(owner not in known for owner in owners):
            raise _artifact_error("mesh owner is not a manifest shape in the same prototype")


def _same_mesh(left: CadPrototypeMesh, right: CadPrototypeMesh) -> bool:
    scalar_equal = (
        left.prototype_id == right.prototype_id
        and left.local_bounds_m == right.local_bounds_m
        and left.diagnostics == right.diagnostics
        and left.tessellation.face_owners == right.tessellation.face_owners
        and left.tessellation.edge_owners == right.tessellation.edge_owners
        and left.tessellation.precision == right.tessellation.precision
    )
    if not scalar_equal:
        return False
    names = ("origin", "positions", "triangles", "face_offsets", "edge_offsets")
    if any(not np.array_equal(getattr(left.tessellation, name), getattr(right.tessellation, name)) for name in names):
        return False
    for name in ("normals", "edge_indices"):
        left_value = getattr(left.tessellation, name)
        right_value = getattr(right.tessellation, name)
        if (left_value is None) != (right_value is None):
            return False
        if left_value is not None and not np.array_equal(left_value, right_value):
            return False
    return True


def _validate_writer_input(document: CadDocument, result: CadTessellationResult) -> CadTessellationResult:
    manifest = document.manifest
    expected_identity = _source_identity_for_manifest(manifest)
    if result.source_identity != expected_identity:
        raise _artifact_error("tessellation source identity disagrees with the document")
    try:
        bound = _bind_tessellation(manifest, result.options, result.prototype_meshes)
    except Exception as error:
        if isinstance(error, CadArtifactError):
            raise
        raise _artifact_error("tessellation cannot be bound to the document") from error
    prototype_by_id = {item.id: item for item in manifest.prototypes}
    for mesh in bound.prototype_meshes:
        if mesh.local_bounds_m != prototype_by_id[mesh.prototype_id].local_bounds_m:
            raise _artifact_error("mesh bounds disagree with its prototype")
    _validate_mesh_owners(manifest, bound.prototype_meshes)
    return bound


def _create_temporary(destination: pathlib.Path) -> tuple[pathlib.Path, tuple[int, int]]:
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"destination directory does not exist: {destination.parent}")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.stem}.anyfileio-preview-",
        suffix=destination.suffix,
        dir=destination.parent,
    )
    temporary = pathlib.Path(name)
    try:
        identity = _file_identity(os.fstat(descriptor))
    except BaseException as error:
        try:
            os.close(descriptor)
        except OSError as close_error:
            error.add_note(f"temporary descriptor close also failed: {close_error}")
        raise _artifact_error("preview temporary identity could not be recorded") from error
    try:
        os.close(descriptor)
    except OSError as error:
        failure = _artifact_error("preview temporary handle could not be closed")
        _cleanup_after_failure(temporary, identity, failure)
        raise failure from error
    return temporary, identity


def _validate_owned_temporary(path: pathlib.Path, identity: tuple[int, int]) -> None:
    try:
        state = path.lstat()
    except OSError as error:
        raise _artifact_error("preview temporary is unavailable") from error
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode) or _file_identity(state) != identity:
        raise _artifact_error("preview temporary identity changed")


def _hash_owned_temporary(
    path: pathlib.Path,
    identity: tuple[int, int],
    expected_handle_signature: tuple[int, ...],
    expected_path_signature: tuple[int, ...],
    cancellation: CancellationCheck = None,
) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    path_before = path.lstat()
    if (
        stat.S_ISLNK(path_before.st_mode)
        or not stat.S_ISREG(path_before.st_mode)
        or _file_identity(path_before) != identity
        or _stat_signature(path_before) != expected_path_signature
    ):
        raise _artifact_error("preview temporary changed before final hash")
    with path.open("rb") as stream:
        handle_before = os.fstat(stream.fileno())
        path_opened = path.lstat()
        if (
            _file_identity(handle_before) != identity
            or _file_identity(path_opened) != identity
            or stat.S_ISLNK(path_opened.st_mode)
            or not stat.S_ISREG(path_opened.st_mode)
            or _stat_signature(handle_before) != expected_handle_signature
            or _stat_signature(path_opened) != expected_path_signature
            or _path_handle_anchor(handle_before) != _path_handle_anchor(path_opened)
        ):
            raise _artifact_error("preview temporary changed while opening for final hash")
        digest, observed = _digest_stream(
            stream,
            _MAX_ARTIFACT_BYTES,
            "artifact_bytes",
            cancellation,
        )
        handle_after = os.fstat(stream.fileno())
        path_after = path.lstat()
        if (
            observed != int(handle_after.st_size)
            or _stat_signature(handle_before) != _stat_signature(handle_after)
            or _stat_signature(path_opened) != _stat_signature(path_after)
            or _file_identity(path_after) != identity
            or stat.S_ISLNK(path_after.st_mode)
            or not stat.S_ISREG(path_after.st_mode)
            or _path_handle_anchor(handle_after) != _path_handle_anchor(path_after)
        ):
            raise _artifact_error("preview temporary changed during final hash")
        return digest, _stat_signature(handle_after), _stat_signature(path_after)


def open_preview_artifact(
    artifact: PathLike,
    *,
    retained_source: PathLike | None = None,
) -> CadDocument:
    """Open one OCP-free preview artifact with lazy prototype arrays."""

    try:
        artifact_path = pathlib.Path(os.path.abspath(os.fspath(artifact)))
    except (TypeError, ValueError, OSError) as error:
        raise TypeError("artifact must be a path-like value") from error
    opened = _open_artifact_metadata(artifact_path)
    return _document_from_opened(opened, retained_source=retained_source)


def _document_from_opened(
    opened: _OpenedArtifact,
    *,
    retained_source: PathLike | None = None,
    cancellation: CancellationCheck = None,
) -> CadDocument:
    snapshot: pathlib.Path | None = None
    snapshot_identity: tuple[int, int] | None = None
    if retained_source is not None:
        try:
            snapshot, _digest, snapshot_identity = _copy_source_snapshot(
                retained_source,
                expected_sha256=opened.manifest.source_sha256,
            )
        except Exception as error:
            if isinstance(error, CadArtifactError):
                raise
            raise _artifact_error("retained source does not match the preview manifest") from error
    holder: list[CadDocument] = []

    def load() -> tuple[CadPrototypeMesh, ...]:
        try:
            return _load_prototype_meshes(opened, cancellation)
        except Exception as error:
            artifact_error = error if isinstance(error, CadArtifactError) else _artifact_error("CAD preview prototype arrays are invalid")
            document = holder[0]
            if not document.source_available:
                raise _artifact_error(
                    "CAD preview is invalid and no retained source is available",
                    code="cad.preview.invalid_without_source",
                ) from artifact_error
            raise artifact_error

    try:
        document = CadDocument._from_preview_artifact(
            manifest=opened.manifest,
            tessellation_options=opened.options,
            prototype_mesh_loader=load,
            source_snapshot=snapshot,
        )
        holder.append(document)
        return document
    except Exception as error:
        if snapshot is not None:
            _cleanup_after_failure(snapshot, snapshot_identity, error)
        raise


def write_preview_artifact(
    document: CadDocument,
    destination: PathLike,
    *,
    tessellation: CadTessellationResult | None = None,
    cancellation: CancellationCheck = None,
) -> str:
    """Atomically publish a deterministic schema-1 preview artifact."""

    if not isinstance(document, CadDocument):
        raise TypeError("document must be CadDocument")
    if tessellation is not None and not isinstance(tessellation, CadTessellationResult):
        raise TypeError("tessellation must be CadTessellationResult or None")
    if cancellation is not None and not callable(cancellation):
        raise TypeError("cancellation must be callable or None")
    _check_cancelled(cancellation)
    selected = document.tessellation if tessellation is None else tessellation
    if selected is None:
        raise _artifact_error(
            "preview persistence requires tessellation",
            code="cad.preview.tessellation_required",
        )
    result = _validate_writer_input(document, selected)
    members = _occurrence_members(document.manifest, cancellation)
    mesh_payloads: list[Mapping[str, Any]] = []
    for mesh in result.prototype_meshes:
        _check_cancelled(cancellation)
        mesh_members, payload = _mesh_members(mesh, cancellation)
        members.update(mesh_members)
        mesh_payloads.append(payload)
    payload = _manifest_payload(document.manifest, result.options, result.source_identity, mesh_payloads, members)
    manifest_bytes = _canonical_json(payload)
    _preflight_members(members, manifest_bytes)
    try:
        destination_path = pathlib.Path(os.fspath(destination))
    except (TypeError, ValueError, OSError) as error:
        raise TypeError("destination must be a path-like value") from error
    temporary: pathlib.Path | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        temporary, temporary_identity = _create_temporary(destination_path)
        _check_cancelled(cancellation)
        write_handle_signature, write_path_signature = _write_zip(
            temporary,
            temporary_identity,
            manifest_bytes,
            members,
            cancellation,
        )
        reopened_metadata = _open_artifact_metadata(
            temporary,
            expected_identity=temporary_identity,
            cancellation=cancellation,
        )
        reopened = _document_from_opened(
            reopened_metadata,
            cancellation=cancellation,
        )
        reopened_result = reopened.tessellation
        if (
            reopened_result is None
            or _document_payload(reopened.manifest) != _document_payload(document.manifest)
            or reopened_result.source_identity != result.source_identity
            or reopened_result.options != result.options
            or len(reopened_result.prototype_meshes) != len(result.prototype_meshes)
            or not all(_same_mesh(left, right) for left, right in zip(reopened_result.prototype_meshes, result.prototype_meshes))
        ):
            raise _artifact_error("preview self-validation disagrees with the source records")
        digest, hash_handle_signature, hash_path_signature = _hash_owned_temporary(
            temporary,
            temporary_identity,
            write_handle_signature,
            write_path_signature,
            cancellation,
        )
        _check_cancelled(cancellation)
        final_state = temporary.lstat()
        if (
            stat.S_ISLNK(final_state.st_mode)
            or not stat.S_ISREG(final_state.st_mode)
            or _file_identity(final_state) != temporary_identity
            or _stat_signature(final_state) != hash_path_signature
        ):
            raise _artifact_error("preview temporary changed before publication")
        os.replace(temporary, destination_path)
        temporary = None
        return digest
    except Exception as error:
        if temporary is not None:
            _cleanup_after_failure(temporary, temporary_identity, error)
        raise
