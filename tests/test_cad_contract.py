"""Focused protocol-1 tests for backend-neutral CAD records."""

from __future__ import annotations

import inspect
import pickle
import threading
from types import MappingProxyType

import numpy as np
import pytest

import anyfileio.cad as cad
from anyfileio.cad import (
    BackendCompatibilityError,
    BackendDuplicateError,
    BackendLoadError,
    BackendUnavailableError,
    CadArtifactError,
    CadBackendError,
    CadBackendProtocol,
    CadCapabilities,
    CadDiagnostic,
    CadDocument,
    CadEntityRef,
    CadError,
    CadManifest,
    CadOccurrenceRecord,
    CadOperationCancelled,
    CadOperationError,
    CadPrototypeMesh,
    CadPrototypeRecord,
    CadReadOptions,
    CadTessellation,
    CadTessellationOptions,
    CadValidationError,
    CadWriteOptions,
    FormatDescriptor,
)


DOCUMENT_ID = "cad-import-v1:" + "1" * 64
COUNTS = {name: 0 for name in ("solid", "shell", "face", "wire", "edge", "vertex")}


def _manifest(*, with_prototype: bool = True, backend_version: str = "0.1.0") -> CadManifest:
    if with_prototype:
        prototype = CadPrototypeRecord(
            1,
            CadEntityRef(DOCUMENT_ID, "prototype", 1),
            "box",
            "solid",
            (0.0, 0.0, 0.0, 1.0, 1.0, 0.0),
            COUNTS,
        )
        occurrence = CadOccurrenceRecord(
            1,
            CadEntityRef(DOCUMENT_ID, "occurrence", 1),
            1,
            None,
            np.eye(4),
            np.eye(4),
            (0.0, 0.0, 0.0, 1.0, 1.0, 0.0),
            "root",
            True,
        )
        prototypes = (prototype,)
        occurrences = (occurrence,)
        roots = (1,)
    else:
        prototypes = ()
        occurrences = ()
        roots = ()
    return CadManifest(
        DOCUMENT_ID,
        "a" * 64,
        "box.step",
        "step",
        "m",
        1.0,
        "m",
        roots,
        prototypes,
        occurrences,
        (),
        (0.0, 0.0, 0.0, 1.0, 1.0, 0.0) if with_prototype else None,
        COUNTS,
        (),
        (),
        CadReadOptions(),
        "occt",
        backend_version,
        1,
        "cadquery-ocp-novtk",
        "7.9.3.1.1",
        "7.9.3",
    )


def _mesh(*, writable_inputs: bool = False) -> CadPrototypeMesh:
    positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float64)
    if not writable_inputs:
        positions.flags.writeable = False
    tessellation = CadTessellation(
        np.array([0.0, 0.0, 0.0]),
        positions,
        np.array([[0, 1, 2]], dtype=np.int64),
        np.array([[0, 0, 1], [0, 0, 1], [0, 0, 1]], dtype=np.float64),
        (CadEntityRef(DOCUMENT_ID, "face", 1),),
        np.array([0, 1]),
        None,
        (),
        np.array([0]),
        "float32",
    )
    return CadPrototypeMesh(1, tessellation, (0, 0, 0, 1, 1, 0), ())


def test_options_normalize_units_defaults_and_reject_invalid_values() -> None:
    assert CadReadOptions() == CadReadOptions("preview", True, None, False)
    assert CadReadOptions(source_length_unit_override=" MM ").source_length_unit_override == "mm"
    assert CadWriteOptions("translate", target_format="STEP", target_length_unit="in").target_format == "step"
    tessellation = CadTessellationOptions()
    assert tessellation.linear_deflection == 0.001
    assert tessellation.angular_deflection == 0.35
    with pytest.raises(ValueError):
        CadReadOptions(mode="guess")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CadReadOptions(source_length_unit_override="yard")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CadTessellationOptions(linear_deflection=float("nan"))
    with pytest.raises(ValueError):
        CadTessellationOptions(precision_policy="half")  # type: ignore[arg-type]


def test_entity_refs_records_and_manifest_fail_closed() -> None:
    reference = CadEntityRef(DOCUMENT_ID, "face", 7)
    assert reference.local_id == 7
    with pytest.raises(ValueError):
        CadEntityRef(DOCUMENT_ID, "provider_face", 1)
    with pytest.raises(ValueError):
        CadEntityRef(DOCUMENT_ID, "face", 0)
    manifest = _manifest()
    assert isinstance(manifest.topology_counts, MappingProxyType)
    with pytest.raises(TypeError):
        manifest.topology_counts["face"] = 1  # type: ignore[index]
    with pytest.raises(ValueError):
        CadPrototypeRecord(2, CadEntityRef(DOCUMENT_ID, "prototype", 1), "", "solid", None, COUNTS)


def test_tessellation_arrays_are_compact_contiguous_and_read_only() -> None:
    mesh = _mesh(writable_inputs=True)
    tessellation = mesh.tessellation
    assert tessellation.positions.dtype == np.dtype(np.float32)
    assert tessellation.triangles.dtype == np.dtype(np.uint32)
    assert tessellation.positions.flags.c_contiguous
    assert not tessellation.positions.flags.writeable
    assert not tessellation.triangles.flags.writeable
    assert tessellation.normals is not None and tessellation.normals.dtype == np.dtype(np.float32)
    with pytest.raises(ValueError):
        tessellation.positions[0, 0] = 2


def test_owner_offsets_and_indices_are_validated() -> None:
    with pytest.raises(ValueError):
        CadTessellation(
            np.zeros(3),
            np.zeros((3, 3)),
            np.array([[0, 1, 3]]),
            None,
            (CadEntityRef(DOCUMENT_ID, "face", 1),),
            np.array([0, 1]),
            None,
            (),
            np.array([0]),
            "float32",
        )
    with pytest.raises(ValueError):
        CadTessellation(
            np.zeros(3),
            np.zeros((3, 3)),
            np.array([[0, 1, 2]]),
            None,
            (CadEntityRef(DOCUMENT_ID, "face", 1),),
            np.array([0, 0]),
            None,
            (),
            np.array([0]),
            "float32",
        )


def test_tessellation_result_binds_exact_source_identity() -> None:
    options = CadTessellationOptions(include_edges=True)
    first = CadDocument._from_backend(manifest=_manifest(), tessellation_options=options, prototype_meshes=(_mesh(),))
    second = CadDocument._from_backend(
        manifest=_manifest(backend_version="0.1.1"),
        tessellation_options=options,
        prototype_meshes=(_mesh(),),
    )
    assert first.tessellation is not None
    assert first.tessellation.source_identity.startswith("cad-tessellation-source-v1:")
    assert second.tessellation is not None
    assert first.tessellation.source_identity != second.tessellation.source_identity


def test_backend_factory_core_binds_tessellation_result() -> None:
    mesh = _mesh()
    document = CadDocument._from_backend(
        manifest=_manifest(),
        tessellation_options=CadTessellationOptions(),
        prototype_meshes=(mesh,),
    )
    assert document.tessellation is not None
    assert document.prototype_meshes == (mesh,)
    with pytest.raises(CadValidationError, match="cover"):
        CadDocument._from_backend(
            manifest=_manifest(),
            tessellation_options=CadTessellationOptions(),
            prototype_meshes=(),
        )


def test_occurrence_transforms_and_instancing_are_preserved() -> None:
    manifest = _manifest()
    occurrence = manifest.occurrences[0]
    assert np.array_equal(occurrence.local_transform, np.eye(4))
    assert not occurrence.local_transform.flags.writeable
    assert manifest.occurrences[0].prototype_id == manifest.prototypes[0].id
    bad = np.eye(4)
    bad[3, 0] = 1
    with pytest.raises(ValueError):
        CadOccurrenceRecord(1, CadEntityRef(DOCUMENT_ID, "occurrence", 1), 1, None, bad, bad, None, "", True)


def test_document_close_release_and_context_are_idempotent(tmp_path) -> None:
    source = tmp_path / "snapshot.step"
    source.write_bytes(b"step")
    closed: list[object] = []
    state = object()
    document = CadDocument._from_backend(
        manifest=_manifest(with_prototype=False),
        source_snapshot=source,
        backend_state=state,
        close_backend_state=closed.append,
        owner_thread_id=threading.get_ident(),
    )
    assert not document.closed and document.source_available
    with document as opened:
        assert opened is document
    document.close()
    assert document.closed and closed == [state]
    assert document.manifest.source_name == "box.step"
    document.release_source()
    document.release_source()
    assert not source.exists() and not document.source_available


def test_live_document_refuses_pickle_and_wrong_thread_close() -> None:
    state = object()
    closed: list[object] = []
    document = CadDocument._from_backend(
        manifest=_manifest(with_prototype=False),
        backend_state=state,
        close_backend_state=closed.append,
        owner_thread_id=threading.get_ident(),
    )
    errors: list[CadOperationError] = []

    def close_elsewhere() -> None:
        try:
            document.close()
        except CadOperationError as error:
            errors.append(error)

    worker = threading.Thread(target=close_elsewhere)
    worker.start()
    worker.join()
    assert errors and errors[0].code == "cad.session.wrong_thread"
    assert not document.closed and closed == []
    with pytest.raises(CadOperationError, match="pickle"):
        pickle.dumps(document)
    document.close()


def test_preview_factory_loads_once_and_caches_first_failure() -> None:
    calls = 0

    def loader() -> tuple[CadPrototypeMesh, ...]:
        nonlocal calls
        calls += 1
        return (_mesh(),)

    document = CadDocument._from_preview_artifact(
        manifest=_manifest(),
        tessellation_options=CadTessellationOptions(),
        prototype_mesh_loader=loader,
    )
    first_meshes = document.prototype_meshes
    assert len(first_meshes) == 1 and first_meshes[0].prototype_id == 1
    assert document.prototype_meshes is first_meshes
    assert calls == 1

    failed_calls = 0

    def failed_loader() -> tuple[CadPrototypeMesh, ...]:
        nonlocal failed_calls
        failed_calls += 1
        raise ValueError("corrupt")

    failed = CadDocument._from_preview_artifact(
        manifest=_manifest(),
        tessellation_options=CadTessellationOptions(),
        prototype_mesh_loader=failed_loader,
    )
    for _ in range(2):
        with pytest.raises(CadArtifactError) as caught:
            _ = failed.prototype_meshes
        assert caught.value.code == "cad.preview.load_failed"
    assert failed_calls == 1


def test_exception_hierarchy_exposes_stable_diagnostics() -> None:
    assert issubclass(BackendUnavailableError, CadBackendError)
    assert issubclass(BackendDuplicateError, CadBackendError)
    assert issubclass(BackendLoadError, CadBackendError)
    assert issubclass(BackendCompatibilityError, CadBackendError)
    assert issubclass(CadValidationError, CadOperationError)
    assert issubclass(CadOperationCancelled, CadOperationError)
    error = CadValidationError("invalid", code="cad.validation.example")
    assert isinstance(error, CadError)
    assert error.code == error.diagnostic.code == "cad.validation.example"
    assert error.diagnostic.severity == "error"


def test_backend_protocol_has_exact_provider_call_shapes() -> None:
    read = inspect.signature(CadBackendProtocol.read)
    tessellate = inspect.signature(CadBackendProtocol.tessellate)
    translate = inspect.signature(CadBackendProtocol.translate)
    assert tuple(read.parameters) == (
        "self",
        "source_snapshot",
        "source_sha256",
        "source_name",
        "options",
        "tessellation_options",
        "cancellation",
    )
    assert tuple(tessellate.parameters) == ("self", "document", "options", "cancellation")
    assert tuple(translate.parameters) == ("self", "document", "destination_temporary", "options", "cancellation")


def test_core_public_annotations_contain_no_optional_package_types() -> None:
    forbidden_type_prefixes = ("OCP.", "cadquery.", "anyfileio_occt.", "anygeometry.")
    public_types = [
        value
        for name, value in vars(cad).items()
        if not name.startswith("_") and inspect.isclass(value) and getattr(value, "__module__", None) == cad.__name__
    ]
    annotation_text = "\n".join(str(getattr(value, "__annotations__", {})) for value in public_types)
    assert not any(name in annotation_text for name in forbidden_type_prefixes)
    descriptor = FormatDescriptor("step", (".step",), "cad_brep", frozenset({"read"}), "occt")
    assert not any(name in repr(descriptor) for name in forbidden_type_prefixes)
