"""Focused synthetic tests for schema-1 CAD preview persistence."""

from __future__ import annotations

import hashlib
import builtins
import inspect
import json
import pathlib
import subprocess
import struct
import sys
import threading
import zipfile

import numpy as np
import pytest

import anyfileio
import anyfileio.cad_artifact as artifacts
from anyfileio.cad import (
    CadArtifactError,
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
    _bind_tessellation,
)

COUNTS = {name: int(name == "face") for name in ("solid", "shell", "face", "wire", "edge", "vertex")}


def _document_id(source_sha256: str) -> str:
    payload = {
        "backend_compatibility_version": 1,
        "backend_id": "occt",
        "effective_source_length_unit": "m",
        "heal": False,
        "identity_kind": "import",
        "identity_version": 1,
        "source_format": "step",
        "source_sha256": source_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "cad-import-v1:" + hashlib.sha256(encoded).hexdigest()


def _document(source_bytes: bytes = b"source") -> tuple[CadDocument, bytes]:
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    document_id = _document_id(source_hash)
    prototype_ref = CadEntityRef(document_id, "prototype", 1)
    occurrence_ref = CadEntityRef(document_id, "occurrence", 1)
    face_ref = CadEntityRef(document_id, "face", 1)
    bounds = (0.0, 0.0, 0.0, 1.0, 1.0, 0.0)
    manifest = CadManifest(
        document_id,
        source_hash,
        "part.step",
        "step",
        "m",
        1.0,
        "m",
        (1,),
        (CadPrototypeRecord(1, prototype_ref, "part", "solid", bounds, COUNTS),),
        (CadOccurrenceRecord(1, occurrence_ref, 1, None, np.eye(4), np.eye(4), bounds, "root", True),),
        (CadShapeRecord(face_ref, 1, 1, prototype_ref, "face", "face", bounds, bounds, None, ()),),
        bounds,
        COUNTS,
        (),
        (),
        CadReadOptions(),
        "occt",
        "0.1.0",
        1,
        "cadquery-ocp-novtk",
        "7.9.3.1.1",
        "7.9.3",
    )
    tessellation = CadTessellation(
        np.zeros(3, dtype=np.float64),
        np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32),
        np.array([[0, 1, 2]], dtype=np.uint32),
        np.array([[0, 0, 1], [0, 0, 1], [0, 0, 1]], dtype=np.float32),
        (face_ref,),
        np.array([0, 1], dtype=np.int64),
        None,
        (),
        np.array([0], dtype=np.int64),
        "float32",
    )
    options = CadTessellationOptions()
    result = _bind_tessellation(manifest, options, (CadPrototypeMesh(1, tessellation, bounds),))
    return CadDocument._from_backend(manifest=manifest, tessellation_options=options, prototype_meshes=result.prototype_meshes), source_bytes


def _write(tmp_path: pathlib.Path) -> tuple[CadDocument, pathlib.Path, str]:
    document, _source = _document()
    target = tmp_path / "preview.zip"
    digest = artifacts.write_preview_artifact(document, target)
    return document, target, digest


def _rewrite(target: pathlib.Path, mutate) -> None:
    with zipfile.ZipFile(target, "r") as archive:
        members = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    mutate(members)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, data in members:
            archive.writestr(artifacts._zip_info(name), data)


def _replace_member_and_hash(members, member_name: str, member_bytes: bytes) -> None:
    for index, (name, _data) in enumerate(members):
        if name == member_name:
            members[index] = (name, member_bytes)
            break
    else:
        raise AssertionError(f"missing test member {member_name}")
    manifest = json.loads(members[0][1])
    manifest["entries"][member_name] = hashlib.sha256(member_bytes).hexdigest()
    members[0] = ("manifest.json", artifacts._canonical_json(manifest))


def _fresh_target(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    document, _ = _document()
    target = tmp_path / name
    artifacts.write_preview_artifact(document, target)
    return target


def test_public_artifact_signatures_and_facade_exports() -> None:
    assert anyfileio.open_preview_artifact is artifacts.open_preview_artifact
    assert anyfileio.write_preview_artifact is artifacts.write_preview_artifact
    assert tuple(inspect.signature(artifacts.open_preview_artifact).parameters) == ("artifact", "retained_source")
    assert tuple(inspect.signature(artifacts.write_preview_artifact).parameters) == (
        "document", "destination", "tessellation", "cancellation"
    )
    assert artifacts.__all__ == ["open_preview_artifact", "write_preview_artifact"]


def test_write_is_byte_deterministic_and_returns_committed_sha256(tmp_path) -> None:
    document, _ = _document()
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first_hash = artifacts.write_preview_artifact(document, first)
    second_hash = artifacts.write_preview_artifact(document, second)
    assert first.read_bytes() == second.read_bytes()
    assert first_hash == second_hash == hashlib.sha256(first.read_bytes()).hexdigest()


def test_zip_envelope_is_canonical_stored_and_non_zip64(tmp_path) -> None:
    _document_value, target, _digest = _write(tmp_path)
    raw = target.read_bytes()
    assert b"PK\x06\x06" not in raw and b"PK\x06\x07" not in raw
    with zipfile.ZipFile(target) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "manifest.json"
        assert [item.filename for item in infos[1:]] == sorted(item.filename for item in infos[1:])
        assert archive.comment == b""
        for info in infos:
            artifacts._validate_zip_info(info)
    artifacts._validate_raw_zip(artifacts.io.BytesIO(raw), len(raw), infos)


def test_manifest_json_and_entry_hashes_are_canonical(tmp_path) -> None:
    _document_value, target, _digest = _write(tmp_path)
    with zipfile.ZipFile(target) as archive:
        manifest = archive.read("manifest.json")
        payload = json.loads(manifest)
        assert artifacts._canonical_json(payload) == manifest
        assert payload["schema"] == "anyfileio.cad-preview"
        for name, digest in payload["entries"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest


def test_npy_members_are_version_two_native_c_contiguous_and_pickle_free(tmp_path) -> None:
    _document_value, target, _digest = _write(tmp_path)
    with zipfile.ZipFile(target) as archive:
        for name in archive.namelist()[1:]:
            data = archive.read(name)
            dtype, _shape, _count, _size = artifacts._preflight_npy_bytes(data)
            array = np.load(artifacts.io.BytesIO(data), allow_pickle=False)
            assert dtype.byteorder in ("=", "|") and array.flags.c_contiguous and not dtype.hasobject


def test_occurrence_ids_choose_uint32_or_uint64_and_reject_overflow() -> None:
    document, _ = _document()
    members = artifacts._occurrence_members(document.manifest)
    assert np.load(artifacts.io.BytesIO(members["occurrences/prototype_ids.npy"]), allow_pickle=False).dtype == np.uint32

    occurrence = document.manifest.occurrences[0]
    occurrence64 = CadOccurrenceRecord(
        (1 << 32) + 1,
        CadEntityRef(document.manifest.document_id, "occurrence", (1 << 32) + 1),
        occurrence.prototype_id,
        None,
        occurrence.local_transform,
        occurrence.accumulated_transform,
        occurrence.world_bounds_m,
        occurrence.name,
        occurrence.visible,
    )
    manifest64 = CadManifest(
        document.manifest.document_id,
        document.manifest.source_sha256,
        document.manifest.source_name,
        document.manifest.source_format,
        document.manifest.source_length_unit,
        document.manifest.source_to_metre_scale,
        document.manifest.internal_length_unit,
        (occurrence64.id,),
        document.manifest.prototypes,
        (occurrence64,),
        (),
        document.manifest.world_bounds_m,
        document.manifest.topology_counts,
        document.manifest.external_references,
        document.manifest.diagnostics,
        document.manifest.normalized_read_options,
        document.manifest.backend_id,
        document.manifest.backend_version,
        document.manifest.backend_compatibility_version,
        document.manifest.binding_distribution,
        document.manifest.binding_version,
        document.manifest.occt_version,
    )
    members64 = artifacts._occurrence_members(manifest64)
    assert np.load(artifacts.io.BytesIO(members64["occurrences/parent_ids.npy"]), allow_pickle=False).dtype == np.uint64
    class OverflowOccurrence:
        id = artifacts._MAX_UINT64 + 1
        prototype_id = 1
        parent_id = None
        local_transform = np.eye(4)
        accumulated_transform = np.eye(4)
        visible = True
    class OverflowManifest:
        occurrences = (OverflowOccurrence(),)
    with pytest.raises(CadArtifactError):
        artifacts._occurrence_members(OverflowManifest())


def test_open_reconstructs_occurrences_instancing_and_read_only_arrays(tmp_path) -> None:
    document, target, _digest = _write(tmp_path)
    reopened = artifacts.open_preview_artifact(target)
    assert reopened.manifest.occurrences[0].prototype_id == 1
    assert artifacts._canonical_json(artifacts._document_payload(reopened.manifest)) == artifacts._canonical_json(
        artifacts._document_payload(document.manifest)
    )
    for actual, expected in zip(reopened.manifest.occurrences, document.manifest.occurrences):
        assert np.array_equal(actual.local_transform, expected.local_transform)
        assert np.array_equal(actual.accumulated_transform, expected.accumulated_transform)
    result = reopened.tessellation
    assert result is not None and result.prototype_meshes[0].tessellation.positions.flags.writeable is False
    expected_result = document.tessellation
    assert expected_result is not None
    for field_name in ("origin", "positions", "triangles", "normals", "face_offsets", "edge_offsets"):
        assert np.array_equal(
            getattr(result.prototype_meshes[0].tessellation, field_name),
            getattr(expected_result.prototype_meshes[0].tessellation, field_name),
        )

    original_result = document.tessellation
    assert original_result is not None
    child_local = np.eye(4)
    child_local[0, 3] = 2.0
    child = CadOccurrenceRecord(
        2,
        CadEntityRef(document.manifest.document_id, "occurrence", 2),
        1,
        1,
        child_local,
        child_local,
        (2.0, 0.0, 0.0, 3.0, 1.0, 0.0),
        "instance",
        False,
    )
    manifest = CadManifest(
        document.manifest.document_id,
        document.manifest.source_sha256,
        document.manifest.source_name,
        document.manifest.source_format,
        document.manifest.source_length_unit,
        document.manifest.source_to_metre_scale,
        document.manifest.internal_length_unit,
        (1,),
        document.manifest.prototypes,
        (*document.manifest.occurrences, child),
        document.manifest.shapes,
        (0.0, 0.0, 0.0, 3.0, 1.0, 0.0),
        document.manifest.topology_counts,
        document.manifest.external_references,
        document.manifest.diagnostics,
        document.manifest.normalized_read_options,
        document.manifest.backend_id,
        document.manifest.backend_version,
        document.manifest.backend_compatibility_version,
        document.manifest.binding_distribution,
        document.manifest.binding_version,
        document.manifest.occt_version,
    )
    instanced = CadDocument._from_backend(
        manifest=manifest,
        tessellation_options=original_result.options,
        prototype_meshes=original_result.prototype_meshes,
    )
    instanced_target = tmp_path / "instanced.zip"
    artifacts.write_preview_artifact(instanced, instanced_target)
    reopened_instanced = artifacts.open_preview_artifact(instanced_target)
    assert len(reopened_instanced.manifest.occurrences) == 2
    loaded_child = reopened_instanced.manifest.occurrences[1]
    assert loaded_child.parent_id == 1 and loaded_child.prototype_id == 1 and not loaded_child.visible
    assert np.array_equal(loaded_child.local_transform, child_local)
    assert np.array_equal(loaded_child.accumulated_transform, child_local)


def test_open_is_metadata_eager_and_prototype_lazy_once(monkeypatch, tmp_path) -> None:
    _document_value, target, _digest = _write(tmp_path)
    calls = 0
    original = artifacts._load_prototype_meshes

    def counted(opened, cancellation=None):
        nonlocal calls
        calls += 1
        return original(opened, cancellation)

    monkeypatch.setattr(artifacts, "_load_prototype_meshes", counted)
    reopened = artifacts.open_preview_artifact(target)
    assert calls == 0
    assert reopened.tessellation is reopened.tessellation
    assert calls == 1


def test_lazy_failure_is_cached_without_partial_publication(monkeypatch, tmp_path) -> None:
    _document_value, target, _digest = _write(tmp_path)
    reopened = artifacts.open_preview_artifact(target)
    target.write_bytes(b"broken")
    with pytest.raises(CadArtifactError) as first:
        _ = reopened.tessellation
    with pytest.raises(CadArtifactError) as second:
        _ = reopened.tessellation
    assert first.value is second.value and reopened._tessellation is None


def test_writer_accepts_explicit_or_document_tessellation(tmp_path) -> None:
    document, _ = _document()
    result = document.tessellation
    assert result is not None
    artifacts.write_preview_artifact(document, tmp_path / "implicit.zip")
    detached = CadDocument._from_backend(manifest=document.manifest)
    artifacts.write_preview_artifact(detached, tmp_path / "explicit.zip", tessellation=result)


def test_writer_requires_tessellation_with_frozen_code(tmp_path) -> None:
    document, _ = _document()
    detached = CadDocument._from_backend(manifest=document.manifest)
    with pytest.raises(CadArtifactError) as caught:
        artifacts.write_preview_artifact(detached, tmp_path / "missing.zip")
    assert caught.value.code == "cad.preview.tessellation_required"


def test_writer_rejects_source_options_backend_and_mesh_mismatches(tmp_path) -> None:
    document, _ = _document()
    result = document.tessellation
    assert result is not None
    other, _ = _document(b"other")
    with pytest.raises(CadArtifactError):
        artifacts.write_preview_artifact(other, tmp_path / "bad.zip", tessellation=result)
    mesh = result.prototype_meshes[0]
    wrong_owner = CadEntityRef(document.manifest.document_id, "face", 999)
    bad_tessellation = CadTessellation(
        mesh.tessellation.origin,
        mesh.tessellation.positions,
        mesh.tessellation.triangles,
        mesh.tessellation.normals,
        (wrong_owner,),
        mesh.tessellation.face_offsets,
        mesh.tessellation.edge_indices,
        mesh.tessellation.edge_owners,
        mesh.tessellation.edge_offsets,
        mesh.tessellation.precision,
    )
    bad_mesh = CadPrototypeMesh(mesh.prototype_id, bad_tessellation, mesh.local_bounds_m)
    bad_result = type(result)(result.source_identity, result.options, (bad_mesh,))
    with pytest.raises(CadArtifactError):
        artifacts.write_preview_artifact(document, tmp_path / "owner.zip", tessellation=bad_result)
    wrong_bounds = CadPrototypeMesh(mesh.prototype_id, mesh.tessellation, (0, 0, 0, 2, 2, 0))
    with pytest.raises(CadArtifactError):
        artifacts.write_preview_artifact(
            document,
            tmp_path / "bounds.zip",
            tessellation=type(result)(result.source_identity, result.options, (wrong_bounds,)),
        )


def test_reader_rejects_unknown_missing_duplicate_or_noncanonical_json(tmp_path) -> None:
    def unknown(payload):
        payload["unknown"] = 1

    def bool_array_row(payload):
        payload["document"]["occurrences"][0]["array_row"] = True

    def bool_document_integer(payload):
        payload["document"]["backend_compatibility_version"] = True

    def float_cache_integer(payload):
        payload["cache_key"]["artifact_version"] = 1.0

    def mesh_scalar_alias(payload):
        payload["meshes"][0]["local_bounds_m"][0] = 0

    for index, mutation in enumerate(
        (unknown, bool_array_row, bool_document_integer, float_cache_integer, mesh_scalar_alias)
    ):
        target = _fresh_target(tmp_path, f"json-{index}.zip")
        def mutate(members, selected=mutation):
            payload = json.loads(members[0][1])
            selected(payload)
            members[0] = ("manifest.json", artifacts._canonical_json(payload))
        _rewrite(target, mutate)
        with pytest.raises(CadArtifactError):
            artifacts.open_preview_artifact(target)

    noncanonical = _fresh_target(tmp_path, "noncanonical-json.zip")
    def add_whitespace(members):
        members[0] = ("manifest.json", members[0][1] + b" ")
    _rewrite(noncanonical, add_whitespace)
    with pytest.raises(CadArtifactError):
        artifacts.open_preview_artifact(noncanonical)


def test_reader_rejects_extra_missing_duplicate_or_unsafe_zip_members(tmp_path) -> None:
    mutations = (
        lambda members: members.append(("extra.npy", artifacts._npy_bytes(np.array([1])))),
        lambda members: members.pop(1),
        lambda members: members.append(members[1]),
        lambda members: members.append(("../unsafe.npy", artifacts._npy_bytes(np.array([1])))),
    )
    for index, mutation in enumerate(mutations):
        target = _fresh_target(tmp_path, f"inventory-{index}.zip")
        _rewrite(target, mutation)
        with pytest.raises(CadArtifactError):
            artifacts.open_preview_artifact(target)


def test_reader_rejects_zip_metadata_descriptor_zip64_and_trailing_data(monkeypatch, tmp_path) -> None:
    metadata = _fresh_target(tmp_path, "metadata.zip")
    with zipfile.ZipFile(metadata) as archive:
        first_offset = archive.infolist()[0].header_offset
    raw = bytearray(metadata.read_bytes())
    struct.pack_into("<H", raw, first_offset + 10, 1)
    metadata.write_bytes(raw)

    descriptor = _fresh_target(tmp_path, "descriptor.zip")
    with zipfile.ZipFile(descriptor) as archive:
        first_offset = archive.infolist()[0].header_offset
    raw = bytearray(descriptor.read_bytes())
    central_offset = struct.unpack_from("<I", raw, len(raw) - 22 + 16)[0]
    struct.pack_into("<H", raw, first_offset + 6, 8)
    struct.pack_into("<H", raw, central_offset + 8, 8)
    descriptor.write_bytes(raw)

    zip64 = _fresh_target(tmp_path, "zip64.zip")
    raw = bytearray(zip64.read_bytes())
    struct.pack_into("<HH", raw, len(raw) - 22 + 8, 0xFFFF, 0xFFFF)
    zip64.write_bytes(raw)

    trailing = _fresh_target(tmp_path, "trailing.zip")
    trailing.write_bytes(trailing.read_bytes() + b"trailing")

    for target in (metadata, descriptor, zip64, trailing):
        with pytest.raises(CadArtifactError):
            artifacts.open_preview_artifact(target)

    central_count = _fresh_target(tmp_path, "central-count.zip")
    raw = bytearray(central_count.read_bytes())
    struct.pack_into("<HH", raw, len(raw) - 22 + 8, 1, 1)
    central_count.write_bytes(raw)
    invoked = False
    def forbidden_zipfile(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("ZipFile must not run before central-directory preflight")
    monkeypatch.setattr(artifacts.zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(CadArtifactError):
        artifacts.open_preview_artifact(central_count)
    assert not invoked


def test_reader_rejects_entry_hash_and_npy_corruption(tmp_path) -> None:
    hash_target = _fresh_target(tmp_path, "hash.zip")
    def corrupt_hash(members):
        for index, (name, data) in enumerate(members):
            if name == "occurrences/visibility.npy":
                members[index] = (name, data[:-1] + bytes([data[-1] ^ 1]))
    _rewrite(hash_target, corrupt_hash)

    header_target = _fresh_target(tmp_path, "header.zip")
    def corrupt_header(members):
        for index, (name, data) in enumerate(members):
            if name == "occurrences/visibility.npy":
                changed = bytearray(data)
                changed[6] = 1
                _replace_member_and_hash(members, name, bytes(changed))
                return
    _rewrite(header_target, corrupt_header)

    dtype_target = _fresh_target(tmp_path, "dtype.zip")
    _rewrite(
        dtype_target,
        lambda members: _replace_member_and_hash(
            members,
            "occurrences/visibility.npy",
            artifacts._npy_bytes(np.array([1], dtype=np.int8)),
        ),
    )

    shape_target = _fresh_target(tmp_path, "shape.zip")
    _rewrite(
        shape_target,
        lambda members: _replace_member_and_hash(
            members,
            "occurrences/visibility.npy",
            artifacts._npy_bytes(np.array([True, False], dtype=np.bool_)),
        ),
    )

    order_target = _fresh_target(tmp_path, "order.zip")
    fortran_stream = artifacts.io.BytesIO()
    np.lib.format.write_array(
        fortran_stream,
        np.asfortranarray(np.eye(4, dtype=np.float64).reshape((1, 4, 4))),
        version=(2, 0),
        allow_pickle=False,
    )
    _rewrite(
        order_target,
        lambda members: _replace_member_and_hash(
            members,
            "occurrences/local_transforms.npy",
            fortran_stream.getvalue(),
        ),
    )

    index_target = _fresh_target(tmp_path, "index.zip")
    _rewrite(
        index_target,
        lambda members: _replace_member_and_hash(
            members,
            "prototypes/1/triangles.npy",
            artifacts._npy_bytes(np.array([[0, 1, 9]], dtype=np.uint32)),
        ),
    )

    offset_target = _fresh_target(tmp_path, "offset.zip")
    _rewrite(
        offset_target,
        lambda members: _replace_member_and_hash(
            members,
            "prototypes/1/face_offsets.npy",
            artifacts._npy_bytes(np.array([0, 0], dtype=np.int64)),
        ),
    )

    for target in (hash_target, header_target, dtype_target, shape_target, order_target):
        with pytest.raises(CadArtifactError):
            artifacts.open_preview_artifact(target)
    for target in (index_target, offset_target):
        reopened = artifacts.open_preview_artifact(target)
        with pytest.raises(CadArtifactError):
            _ = reopened.tessellation


@pytest.mark.parametrize(
    ("resource", "limit"),
    (
        ("artifact_bytes", artifacts._MAX_ARTIFACT_BYTES),
        ("manifest_bytes", artifacts._MAX_MANIFEST_BYTES),
        ("npy_header_bytes", artifacts._MAX_NPY_HEADER_BYTES),
        ("member_bytes", artifacts._MAX_MEMBER_BYTES),
        ("aggregate_stored_member_bytes", artifacts._MAX_STORED_MEMBER_BYTES),
        ("array_data_bytes", artifacts._MAX_ARRAY_BYTES),
        ("aggregate_array_bytes", artifacts._MAX_ARRAY_AGGREGATE_BYTES),
        ("member_count", artifacts._MAX_MEMBER_COUNT),
        ("array_element_count", artifacts._MAX_ARRAY_ELEMENTS),
        ("aggregate_array_elements", artifacts._MAX_ARRAY_AGGREGATE_ELEMENTS),
        ("json_depth", artifacts._MAX_JSON_DEPTH),
    ),
)
def test_reader_policy_limits_accept_exact_limit_and_reject_limit_plus_one(resource, limit) -> None:
    boundary_callers = {
        "artifact_bytes": lambda observed: artifacts._require_reader_limit("artifact_bytes", observed),
        "manifest_bytes": lambda observed: artifacts._require_reader_limit("manifest_bytes", observed),
        "npy_header_bytes": lambda observed: artifacts._require_reader_limit("npy_header_bytes", observed),
        "member_bytes": lambda observed: artifacts._require_reader_limit("member_bytes", observed),
        "aggregate_stored_member_bytes": lambda observed: artifacts._checked_reader_add("aggregate_stored_member_bytes", 0, observed),
        "array_data_bytes": lambda observed: artifacts._checked_reader_product("array_data_bytes", (observed, 1)),
        "aggregate_array_bytes": lambda observed: artifacts._checked_reader_add("aggregate_array_bytes", 0, observed),
        "member_count": lambda observed: artifacts._require_reader_limit("member_count", observed),
        "array_element_count": lambda observed: artifacts._checked_reader_product("array_element_count", (observed,)),
        "aggregate_array_elements": lambda observed: artifacts._checked_reader_add("aggregate_array_elements", 0, observed),
        "json_depth": lambda observed: artifacts._require_reader_limit("json_depth", observed),
    }
    boundary_callers[resource](limit)
    with pytest.raises(CadArtifactError) as caught:
        boundary_callers[resource](limit + 1)
    assert caught.value.code == "cad.preview.resource_limit"
    assert dict(caught.value.diagnostic.details) == {"resource": resource, "limit": limit, "observed": limit + 1}


def test_reader_policy_preflight_is_overflow_safe_and_hashes_streaming() -> None:
    assert artifacts._checked_product((2, 3), 6, "test") == 6
    with pytest.raises(CadArtifactError):
        artifacts._checked_product((2, 4), 7, "test")
    class BoundedReader:
        def __init__(self, data):
            self.data = data
            self.offset = 0
            self.calls = []
        def read(self, size):
            assert 0 <= size <= artifacts._BUFFER_SIZE
            self.calls.append(size)
            block = self.data[self.offset:self.offset + size]
            self.offset += len(block)
            return block
    reader = BoundedReader(b"abc")
    data, digest = artifacts._hash_stream(reader, 3, "test")
    assert data == b"abc" and digest == hashlib.sha256(b"abc").hexdigest()
    assert reader.calls and all(size != -1 for size in reader.calls)


def test_retained_source_is_snapshotted_hash_bound_and_releasable(monkeypatch, tmp_path) -> None:
    _document_value, target, _digest = _write(tmp_path)
    source = tmp_path / "part.step"
    source.write_bytes(b"source")
    reopened = artifacts.open_preview_artifact(target, retained_source=source)
    assert reopened.source_available and reopened._source_snapshot != source
    reopened.release_source()
    assert not reopened.source_available and source.read_bytes() == b"source"
    mismatch = tmp_path / "mismatch.step"
    mismatch.write_bytes(b"different")
    with pytest.raises(CadArtifactError):
        artifacts.open_preview_artifact(target, retained_source=mismatch)
    assert mismatch.read_bytes() == b"different"
    owned = tmp_path / "owned-snapshot.step"
    owned.write_bytes(b"source")
    identity = artifacts._file_identity(owned.stat())
    monkeypatch.setattr(
        artifacts,
        "_copy_source_snapshot",
        lambda *args, **kwargs: (owned, hashlib.sha256(b"source").hexdigest(), identity),
    )
    monkeypatch.setattr(
        CadDocument,
        "_from_preview_artifact",
        classmethod(lambda cls, **kwargs: (_ for _ in ()).throw(RuntimeError("factory failed"))),
    )
    with pytest.raises(RuntimeError, match="factory failed"):
        artifacts.open_preview_artifact(target, retained_source=source)
    assert not owned.exists() and source.read_bytes() == b"source"


def test_lazy_artifact_replacement_fails_closed_with_or_without_source(tmp_path) -> None:
    _document_value, target, _digest = _write(tmp_path)
    source = tmp_path / "part.step"
    source.write_bytes(b"source")
    with_source = artifacts.open_preview_artifact(target, retained_source=source)
    without_source = artifacts.open_preview_artifact(target)
    target.write_bytes(b"invalid")
    with pytest.raises(CadArtifactError) as first:
        _ = with_source.tessellation
    with pytest.raises(CadArtifactError) as second:
        _ = without_source.tessellation
    assert first.value.code != "cad.preview.invalid_without_source"
    assert second.value.code == "cad.preview.invalid_without_source"
    assert with_source.source_available


def test_release_before_first_access_selects_without_source_failure(tmp_path) -> None:
    _document_value, target, _digest = _write(tmp_path)
    source = tmp_path / "part.step"
    source.write_bytes(b"source")
    reopened = artifacts.open_preview_artifact(target, retained_source=source)
    target.write_bytes(b"invalid")
    reopened.release_source()
    with pytest.raises(CadArtifactError) as caught:
        _ = reopened.tessellation
    assert caught.value.code == "cad.preview.invalid_without_source"


def test_release_and_lazy_load_have_deterministic_linearization(monkeypatch, tmp_path) -> None:
    _document_value, target, _digest = _write(tmp_path)
    source = tmp_path / "part.step"
    source.write_bytes(b"source")
    reopened = artifacts.open_preview_artifact(target, retained_source=source)
    target.write_bytes(b"invalid")
    entered = threading.Event()
    proceed = threading.Event()
    original = artifacts._load_prototype_meshes
    calls = 0
    def blocked(opened, cancellation=None):
        nonlocal calls
        calls += 1
        entered.set()
        assert proceed.wait(2)
        return original(opened, cancellation)
    monkeypatch.setattr(artifacts, "_load_prototype_meshes", blocked)
    failures = []
    load_thread = threading.Thread(target=lambda: _capture_failure(lambda: reopened.tessellation, failures))
    release_thread = threading.Thread(target=reopened.release_source)
    load_thread.start()
    assert entered.wait(2)
    release_thread.start()
    proceed.set()
    load_thread.join(2)
    release_thread.join(2)
    assert len(failures) == 1 and failures[0].code != "cad.preview.invalid_without_source"
    assert not reopened.source_available
    with pytest.raises(CadArtifactError) as cached:
        _ = reopened.tessellation
    assert cached.value is failures[0] and calls == 1


def _capture_failure(action, failures) -> None:
    try:
        action()
    except CadArtifactError as error:
        failures.append(error)


def test_writer_preserves_destination_and_cleans_only_owned_temporary(tmp_path) -> None:
    document, _ = _document()
    target = tmp_path / "preview.zip"
    target.write_bytes(b"existing")
    wrong, _ = _document(b"wrong")
    with pytest.raises(CadArtifactError):
        artifacts.write_preview_artifact(wrong, target, tessellation=document.tessellation)
    assert target.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".*.anyfileio-preview-*"))


def test_cancellation_preserves_destination_with_frozen_codes(tmp_path) -> None:
    document, _ = _document()
    target = tmp_path / "preview.zip"
    target.write_bytes(b"existing")
    with pytest.raises(Exception) as caught:
        artifacts.write_preview_artifact(document, target, cancellation=lambda: True)
    assert getattr(caught.value, "code", None) == "cad.operation.cancelled"
    assert target.read_bytes() == b"existing"


def test_write_open_and_lazy_load_import_no_optional_cad_packages(monkeypatch, tmp_path) -> None:
    source_root = pathlib.Path(anyfileio.__file__).resolve().parents[1]
    script = (
        "import sys; import anyfileio.cad_artifact; "
        "blocked=('OCP','cadquery','anyfileio_occt','anygeometry'); "
        "assert not any(n.startswith(blocked) for n in sys.modules)"
    )
    environment = dict(artifacts.os.environ)
    environment["PYTHONPATH"] = str(source_root)
    subprocess.run([sys.executable, "-c", script], check=True, env=environment, capture_output=True, text=True)
    before = set(sys.modules)
    original_import = builtins.__import__
    def blocked_import(name, *args, **kwargs):
        if name.startswith(("OCP", "cadquery", "anyfileio_occt", "anygeometry")):
            raise AssertionError(f"optional CAD package import attempted: {name}")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked_import)
    _document_value, target, _digest = _write(tmp_path)
    reopened = artifacts.open_preview_artifact(target)
    assert reopened.tessellation is not None
    imported = set(sys.modules) - before
    assert not any(name.startswith(("OCP", "cadquery", "anyfileio_occt", "anygeometry")) for name in imported)


def test_writer_self_validation_forces_lazy_round_trip(monkeypatch, tmp_path) -> None:
    document, _ = _document()
    calls = 0
    original = artifacts._load_prototype_meshes
    def counted(opened, cancellation=None):
        nonlocal calls
        calls += 1
        return original(opened, cancellation)
    monkeypatch.setattr(artifacts, "_load_prototype_meshes", counted)
    artifacts.write_preview_artifact(document, tmp_path / "preview.zip")
    assert calls == 1
