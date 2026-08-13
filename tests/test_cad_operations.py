"""Focused orchestration tests for the protocol-1 CAD core."""

from __future__ import annotations

import hashlib
import inspect
import json
import pathlib
import threading

import numpy as np
import pytest

import anyfileio
import anyfileio.cad_operations as operations
from anyfileio.cad import (
    CadAssetWriteReport,
    CadCapabilities,
    CadDiagnostic,
    CadDocument,
    CadEntityRef,
    CadManifest,
    CadOccurrenceRecord,
    CadOperationCancelled,
    CadOperationError,
    CadPrototypeMesh,
    CadPrototypeRecord,
    CadReadOptions,
    CadShapeRecord,
    CadTessellation,
    CadTessellationOptions,
    CadValidationError,
    CadWriteOptions,
)

COUNTS = {name: int(name == "face") for name in ("solid", "shell", "face", "wire", "edge", "vertex")}


def _document_id(source_sha256: str, options: CadReadOptions, unit: str = "m") -> str:
    payload = {
        "backend_compatibility_version": 1,
        "backend_id": "occt",
        "effective_source_length_unit": unit,
        "heal": options.heal,
        "identity_kind": "import",
        "identity_version": 1,
        "source_format": "step",
        "source_sha256": source_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return "cad-import-v1:" + hashlib.sha256(encoded).hexdigest()


def _manifest(
    source_sha256: str,
    *,
    source_name: str = "assembly.step",
    options: CadReadOptions = CadReadOptions(),
    backend_version: str = "0.1.0",
    unit: str = "m",
    external_references: tuple[str, ...] = (),
) -> CadManifest:
    document_id = _document_id(source_sha256, options, unit)
    prototype_ref = CadEntityRef(document_id, "prototype", 1)
    occurrence_ref = CadEntityRef(document_id, "occurrence", 1)
    face_ref = CadEntityRef(document_id, "face", 1)
    prototype = CadPrototypeRecord(1, prototype_ref, "part", "solid", (0, 0, 0, 1, 1, 0), COUNTS)
    occurrence = CadOccurrenceRecord(
        1,
        occurrence_ref,
        1,
        None,
        np.eye(4),
        np.eye(4),
        (0, 0, 0, 1, 1, 0),
        "root",
        True,
    )
    shape = CadShapeRecord(
        face_ref,
        1,
        1,
        prototype_ref,
        "face",
        "face",
        (0, 0, 0, 1, 1, 0),
        (0, 0, 0, 1, 1, 0),
        None,
        (),
    )
    return CadManifest(
        document_id,
        source_sha256,
        source_name,
        "step",
        unit,
        None if unit == "unknown" else 1.0,
        "m",
        (1,),
        (prototype,),
        (occurrence,),
        (shape,),
        (0, 0, 0, 1, 1, 0),
        COUNTS,
        external_references,
        (),
        options,
        "occt",
        backend_version,
        1,
        "cadquery-ocp-novtk",
        "7.9.3.1.1",
        "7.9.3",
    )


def _mesh(manifest: CadManifest) -> CadPrototypeMesh:
    face_ref = next(shape.cad_ref for shape in manifest.shapes if shape.cad_ref.kind == "face")
    tessellation = CadTessellation(
        np.zeros(3),
        np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float),
        np.array([[0, 1, 2]]),
        np.array([[0, 0, 1], [0, 0, 1], [0, 0, 1]], dtype=float),
        (face_ref,),
        np.array([0, 1]),
        None,
        (),
        np.array([0]),
        "float32",
    )
    return CadPrototypeMesh(1, tessellation, (0, 0, 0, 1, 1, 0), ())


class FakeBackend:
    backend_id = "occt"
    protocol_version = 1
    backend_compatibility_version = 1
    backend_version = "0.1.0"

    def __init__(self) -> None:
        self.capabilities = CadCapabilities(
            frozenset({"step", "iges"}),
            frozenset({"step", "iges"}),
            frozenset({"manifest_only", "preview", "live"}),
            True,
            True,
            True,
            True,
            True,
        )
        self.read_calls = []
        self.tessellate_calls = []
        self.translate_calls = []
        self.closed = []
        self.bad_name = False
        self.bad_reopen = False
        self.attach_unexpected: pathlib.Path | None = None
        self.raise_read: Exception | None = None
        self.raise_translate: Exception | None = None
        self.report_corruption: str | None = None
        self.wrong_owner = False
        self.returned_documents: list[CadDocument] = []

    def read(
        self,
        source_snapshot: pathlib.Path,
        *,
        source_sha256: str,
        source_name: str,
        options: CadReadOptions,
        tessellation_options: CadTessellationOptions | None,
        cancellation,
    ) -> CadDocument:
        self.read_calls.append((source_snapshot, source_sha256, source_name, options, tessellation_options, cancellation))
        if self.raise_read is not None:
            raise self.raise_read
        manifest = _manifest(
            source_sha256,
            source_name="wrong.step" if self.bad_name else source_name,
            options=options,
            external_references=("changed",) if self.bad_reopen and options.mode == "live" else (),
        )
        snapshot = self.attach_unexpected if self.attach_unexpected is not None else (source_snapshot if options.retain_source else None)
        state = object() if options.mode == "live" else None
        meshes = (_mesh(manifest),) if tessellation_options is not None else ()
        document = CadDocument._from_backend(
            manifest=manifest,
            tessellation_options=tessellation_options,
            prototype_meshes=meshes,
            source_snapshot=snapshot,
            backend_state=state,
            close_backend_state=self.closed.append if state is not None else None,
            owner_thread_id=(threading.get_ident() + 1 if self.wrong_owner else threading.get_ident()) if state is not None else None,
        )
        self.returned_documents.append(document)
        return document

    def tessellate(self, document: CadDocument, *, options: CadTessellationOptions, cancellation):
        self.tessellate_calls.append((document, options, cancellation))
        return (_mesh(document.manifest),)

    def translate(self, document: CadDocument, destination_temporary: pathlib.Path, *, options: CadWriteOptions, cancellation):
        self.translate_calls.append((document, destination_temporary, options, cancellation))
        if self.raise_translate is not None:
            raise self.raise_translate
        payload = b"translated-step"
        destination_temporary.write_bytes(payload)
        output_sha256 = hashlib.sha256(payload).hexdigest()
        manifest = document.manifest
        target_format = options.target_format or manifest.source_format
        target_unit = options.target_length_unit or manifest.source_length_unit
        exported = operations._manifest_entities(manifest)
        diagnostics = ()
        if self.report_corruption == "target_unit":
            target_unit = "mm"
        elif self.report_corruption == "output_hash":
            output_sha256 = "0" * 64
        elif self.report_corruption == "entity":
            exported = (CadEntityRef(manifest.document_id, "face", 999),)
        elif self.report_corruption == "diagnostic":
            diagnostics = (
                CadDiagnostic(
                    "cad.writer.unknown",
                    "warning",
                    "unknown entity",
                    (CadEntityRef(manifest.document_id, "edge", 999),),
                ),
            )
        return CadAssetWriteReport(
            manifest.document_id,
            "translate",
            manifest.source_format,
            target_format,
            manifest.source_length_unit,
            target_unit,
            "occt",
            self.backend_version,
            1,
            manifest.binding_version,
            manifest.occt_version,
            output_sha256,
            False,
            manifest.topology_counts,
            manifest.topology_counts,
            options.heal,
            options.heal,
            exported,
            (),
            (),
            (),
            diagnostics,
            "provider_translation",
        )


def _install_backend(monkeypatch, backend: FakeBackend) -> None:
    monkeypatch.setattr(operations, "_load_backend", lambda: backend)


def _closed_document(source: pathlib.Path, *, options: CadReadOptions | None = None) -> CadDocument:
    selected = options or CadReadOptions("preview", True)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    return CadDocument._from_backend(manifest=_manifest(source_sha256, source_name=source.name, options=selected), source_snapshot=source)


def _live_document(source: pathlib.Path, backend: FakeBackend) -> CadDocument:
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    options = CadReadOptions("live", True)
    return CadDocument._from_backend(
        manifest=_manifest(source_sha256, source_name=source.name, options=options),
        source_snapshot=source,
        backend_state=object(),
        close_backend_state=backend.closed.append,
        owner_thread_id=threading.get_ident(),
    )


def test_public_operation_signatures_and_facade_exports() -> None:
    assert anyfileio.read_cad is operations.read_cad
    assert anyfileio.tessellate_cad is operations.tessellate_cad
    assert anyfileio.write_cad is operations.write_cad
    assert tuple(inspect.signature(operations.read_cad).parameters) == (
        "source", "options", "tessellation_options", "cancellation", "backend_id"
    )


def test_snapshot_requires_regular_file_and_preserves_caller_bytes(tmp_path) -> None:
    with pytest.raises(CadValidationError):
        operations._snapshot_source(tmp_path)
    source = tmp_path / "part.step"
    source.write_bytes(b"caller-owned")
    snapshot = operations._snapshot_source(source)
    try:
        assert snapshot != source and snapshot.read_bytes() == b"caller-owned"
        assert source.read_bytes() == b"caller-owned"
    finally:
        snapshot.unlink()


def test_snapshot_hashes_exact_bytes_and_normalizes_source_name(monkeypatch, tmp_path) -> None:
    source = tmp_path / "cafe\N{COMBINING ACUTE ACCENT}.step"
    source.write_bytes(b"exact")
    backend = FakeBackend()
    _install_backend(monkeypatch, backend)
    document = operations.read_cad(source, options=CadReadOptions("manifest_only", False))
    assert backend.read_calls[0][1] == hashlib.sha256(b"exact").hexdigest()
    assert backend.read_calls[0][2] == "caf\N{LATIN SMALL LETTER E WITH ACUTE}.step"
    assert not document.source_available


def test_snapshot_detects_expected_hash_or_source_drift(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"one")
    with pytest.raises(CadOperationError) as caught:
        operations._snapshot_source(source, expected_sha256="0" * 64)
    assert caught.value.code == "cad.source.changed"
    original = operations._stat_signature
    calls = 0

    def drifting(value):
        nonlocal calls
        calls += 1
        signature = original(value)
        return signature if calls < 4 else (*signature[:-1], signature[-1] + 1)

    monkeypatch.setattr(operations, "_stat_signature", drifting)
    with pytest.raises(CadOperationError) as caught:
        operations._snapshot_source(source)
    assert caught.value.code == "cad.source.changed"


def test_cancellation_true_and_callback_failure_use_frozen_codes(tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    with pytest.raises(CadOperationCancelled) as caught:
        operations.read_cad(source, cancellation=lambda: True)
    assert caught.value.code == "cad.operation.cancelled"

    def broken():
        raise LookupError("stop")

    with pytest.raises(CadOperationError) as caught:
        operations.read_cad(source, cancellation=broken)
    assert caught.value.code == "cad.cancellation_check.failed"
    assert isinstance(caught.value.__cause__, LookupError)


def test_manifest_only_rejects_tessellation_before_provider_or_copy(monkeypatch) -> None:
    monkeypatch.setattr(operations, "_load_backend", lambda: pytest.fail("provider loaded"))
    monkeypatch.setattr(operations, "_copy_source_snapshot", lambda *args, **kwargs: pytest.fail("source copied"))
    with pytest.raises(CadValidationError):
        operations.read_cad("missing.step", options=CadReadOptions("manifest_only"), tessellation_options=CadTessellationOptions())


def test_read_checks_format_mode_and_capability_before_source_access(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    backend = FakeBackend()
    backend.capabilities = CadCapabilities()
    _install_backend(monkeypatch, backend)
    monkeypatch.setattr(operations, "_copy_source_snapshot", lambda *args, **kwargs: pytest.fail("source copied"))
    with pytest.raises(CadOperationError):
        operations.read_cad(source)


def test_read_passes_exact_provider_call_and_retains_owned_snapshot(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    backend = FakeBackend()
    _install_backend(monkeypatch, backend)
    options = CadReadOptions("live", True)
    document = operations.read_cad(source, options=options)
    assert backend.read_calls[0][3:] == (options, None, None)
    assert document.source_available and not document.closed
    document.close()
    document.release_source()
    assert source.exists()


def test_read_preview_defaults_tessellation_and_closes_session(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    backend = FakeBackend()
    _install_backend(monkeypatch, backend)
    document = operations.read_cad(source)
    assert document.closed and document.tessellation is not None
    assert document.tessellation.options == CadTessellationOptions()
    document.release_source()


def test_read_unretained_and_provider_failure_remove_only_owned_spool(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    backend = FakeBackend()
    _install_backend(monkeypatch, backend)
    document = operations.read_cad(source, options=CadReadOptions("manifest_only", False))
    spool = backend.read_calls[0][0]
    assert not spool.exists() and source.read_bytes() == b"part" and not document.source_available
    backend.raise_read = RuntimeError("provider")
    with pytest.raises(CadOperationError):
        operations.read_cad(source, options=CadReadOptions("manifest_only", False))
    assert not backend.read_calls[-1][0].exists() and source.exists()

    backend.raise_read = None
    held_spool = None

    def replace_spool_then_fail(
        source_snapshot,
        *,
        source_sha256,
        source_name,
        options,
        tessellation_options,
        cancellation,
    ):
        nonlocal held_spool
        backend.read_calls.append(
            (source_snapshot, source_sha256, source_name, options, tessellation_options, cancellation)
        )
        held_spool = source_snapshot.with_name(f"{source_snapshot.name}.held")
        source_snapshot.replace(held_spool)
        source_snapshot.write_bytes(b"replacement-owned-by-someone-else")
        raise RuntimeError("provider replaced the spool")

    monkeypatch.setattr(backend, "read", replace_spool_then_fail)
    with pytest.raises(CadOperationError) as caught:
        operations.read_cad(source, options=CadReadOptions("manifest_only", False))
    replacement = backend.read_calls[-1][0]
    try:
        assert isinstance(caught.value.__cause__, RuntimeError)
        assert replacement.read_bytes() == b"replacement-owned-by-someone-else"
        assert held_spool is not None and held_spool.read_bytes() == b"part"
        assert any(
            "cleanup also failed:" in note and "identity changed; refusing removal" in note
            for note in getattr(caught.value, "__notes__", ())
        )
    finally:
        replacement.unlink(missing_ok=True)
        if held_spool is not None:
            held_spool.unlink(missing_ok=True)
    assert source.read_bytes() == b"part"


def test_read_rejects_manifest_or_attached_source_mismatch(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    backend = FakeBackend()
    backend.bad_name = True
    _install_backend(monkeypatch, backend)
    with pytest.raises(CadOperationError):
        operations.read_cad(source, options=CadReadOptions("manifest_only", False))
    assert source.exists()
    backend.bad_name = False
    caller_owned = tmp_path / "caller-owned.step"
    caller_owned.write_bytes(b"must-survive")
    backend.attach_unexpected = caller_owned
    with pytest.raises(CadOperationError):
        operations.read_cad(source, options=CadReadOptions("manifest_only", True))
    assert caller_owned.read_bytes() == b"must-survive"

    backend.attach_unexpected = None
    backend.wrong_owner = True
    with pytest.raises(CadOperationError) as caught:
        operations.read_cad(source, options=CadReadOptions("live", False))
    assert "live provider session is not owned by the caller thread" in str(caught.value)
    assert any(
        "provider-session cleanup also failed:" in note and "cad.session.wrong_thread" in note
        for note in getattr(caught.value, "__notes__", ())
    )
    leaked = backend.returned_documents[-1]
    leaked._owner_thread_id = threading.get_ident()
    leaked.close()
    assert source.read_bytes() == b"part"


def test_live_tessellation_core_binds_provider_meshes(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    backend = FakeBackend()
    _install_backend(monkeypatch, backend)
    document = _live_document(source, backend)
    options = CadTessellationOptions(include_edges=True)
    result = operations.tessellate_cad(document, options=options)
    assert result.options is options and result.prototype_meshes[0].prototype_id == 1
    assert backend.tessellate_calls[0][0] is document
    monkeypatch.setattr(
        backend,
        "tessellate",
        lambda operation_document, *, options, cancellation: [_mesh(operation_document.manifest)],
    )
    with pytest.raises(CadOperationError, match="must be a tuple"):
        operations.tessellate_cad(document, options=options)
    document.close()


def test_closed_tessellation_reopens_retained_source_transiently(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    backend = FakeBackend()
    _install_backend(monkeypatch, backend)
    document = _closed_document(source)
    result = operations.tessellate_cad(document)
    assert result.prototype_meshes and backend.read_calls[-1][3] == CadReadOptions("live", False)
    assert backend.tessellate_calls[-1][0] is not document and backend.closed
    document.release_source()


def test_tessellation_rejects_unavailable_or_mismatched_reopen(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    backend = FakeBackend()
    _install_backend(monkeypatch, backend)
    unavailable = CadDocument._from_backend(manifest=_manifest(hashlib.sha256(b"part").hexdigest()))
    with pytest.raises(CadOperationError) as caught:
        operations.tessellate_cad(unavailable)
    assert caught.value.code == "cad.source.unavailable"
    backend.bad_reopen = True
    mismatched = _closed_document(source)
    with pytest.raises(CadOperationError) as caught:
        operations.tessellate_cad(mismatched)
    assert caught.value.code == "cad.source.reopen_mismatch"
    mismatched.release_source()

    backend.bad_reopen = False
    drifted_source = tmp_path / "drifted.step"
    drifted_source.write_bytes(b"part")
    drifted = _closed_document(drifted_source)

    def drift_then_fail(_document, *, options, cancellation):
        backend.read_calls[-1][0].write_bytes(b"changed")
        raise RuntimeError("tessellation failed")

    monkeypatch.setattr(backend, "tessellate", drift_then_fail)
    with pytest.raises(CadOperationError, match="provider tessellation failed") as caught:
        operations.tessellate_cad(drifted)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert any(
        "retained-source verification also failed:" in note and "cad.source.reopen_mismatch" in note
        for note in getattr(caught.value, "__notes__", ())
    )
    drifted.release_source()


def test_preserve_is_provider_free_atomic_and_byte_identical(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"original-step")
    document = _closed_document(source)
    monkeypatch.setattr(operations, "_load_backend", lambda: pytest.fail("provider loaded"))
    destination = tmp_path / "copy.step"
    report = operations.write_cad(document, destination, options=CadWriteOptions("preserve"))
    expected_entities = {
        document.manifest.prototypes[0].cad_ref,
        document.manifest.occurrences[0].cad_ref,
        document.manifest.shapes[0].cad_ref,
    }
    assert destination.read_bytes() == b"original-step"
    assert report.byte_identical and set(report.exported_entities) == expected_entities
    assert report.diagnostics == () and report.execution_mode == "preserve_copy"
    document.release_source()


def test_preserve_rejects_heal_format_unit_or_suffix_before_temporary(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    document = _closed_document(source)
    monkeypatch.setattr(operations, "_create_output_temporary", lambda *args: pytest.fail("temporary created"))
    for destination, options in (
        (tmp_path / "part.iges", CadWriteOptions("preserve")),
        (tmp_path / "part.step", CadWriteOptions("preserve", target_length_unit="mm")),
        (tmp_path / "part.step", CadWriteOptions("preserve", heal=True)),
        (tmp_path / "part.xyz", CadWriteOptions("preserve")),
    ):
        with pytest.raises(CadOperationError):
            operations.write_cad(document, destination, options=options)
    document.release_source()
    healed_source = tmp_path / "healed.step"
    healed_source.write_bytes(b"healed")
    healed = _closed_document(healed_source, options=CadReadOptions("preview", True, heal=True))
    with pytest.raises(CadValidationError) as caught:
        operations.write_cad(healed, tmp_path / "healed-copy.step", options=CadWriteOptions("preserve"))
    assert caught.value.code == "cad.preserve.healed_source"
    healed.release_source()


def test_translation_uses_live_state_and_atomic_same_suffix_temporary(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    backend = FakeBackend()
    _install_backend(monkeypatch, backend)
    document = _live_document(source, backend)
    destination = tmp_path / "translated.step"
    report = operations.write_cad(document, destination, options=CadWriteOptions("translate"))
    assert destination.read_bytes() == b"translated-step"
    assert backend.translate_calls[0][0] is document
    assert backend.translate_calls[0][1].suffix == ".step"
    assert report.execution_mode == "provider_translation"
    document.close()


def test_translation_reopens_retained_source_without_adopting_it(monkeypatch, tmp_path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    backend = FakeBackend()
    _install_backend(monkeypatch, backend)
    document = _closed_document(source)
    destination = tmp_path / "translated.step"
    operations.write_cad(document, destination, options=CadWriteOptions("translate"))
    assert backend.translate_calls[0][0] is not document
    assert backend.read_calls[-1][3].retain_source is False
    assert source.exists() and document.source_available
    document.release_source()


@pytest.mark.parametrize("corruption", ["target_unit", "output_hash", "entity", "diagnostic"])
def test_translation_rejects_report_entity_or_output_hash_mismatch(monkeypatch, tmp_path, corruption) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    destination = tmp_path / "translated.step"
    destination.write_bytes(b"old")
    backend = FakeBackend()
    backend.report_corruption = corruption
    _install_backend(monkeypatch, backend)
    document = _live_document(source, backend)
    with pytest.raises(CadOperationError):
        operations.write_cad(document, destination, options=CadWriteOptions("translate"))
    assert destination.read_bytes() == b"old"
    document.close()


def test_write_failure_or_cancellation_preserves_existing_destination(monkeypatch, tmp_path) -> None:
    class ExplosivePath:
        def __fspath__(self):
            raise AssertionError("path coerced before type validation")

    with pytest.raises(TypeError):
        operations.write_cad(object(), ExplosivePath(), options=CadWriteOptions("preserve"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        operations.write_cad(object(), ExplosivePath(), options=object())  # type: ignore[arg-type]

    events = []
    real_fstat = operations.os.fstat
    real_close = operations.os.close

    def tracked_fstat(descriptor):
        events.append(("fstat", descriptor))
        return real_fstat(descriptor)

    def tracked_close(descriptor):
        events.append(("close", descriptor))
        return real_close(descriptor)

    with monkeypatch.context() as local:
        local.setattr(operations.os, "fstat", tracked_fstat)
        local.setattr(operations.os, "close", tracked_close)
        probe, probe_identity = operations._create_output_temporary(tmp_path / "probe.step")
    assert events[0][0] == "fstat" and events[1] == ("close", events[0][1])
    operations._unlink_owned(probe, probe_identity)

    created_on_failure = []
    closed_on_failure = []
    real_mkstemp = operations.tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        result = real_mkstemp(*args, **kwargs)
        created_on_failure.append(pathlib.Path(result[1]))
        return result

    def failing_fstat(_descriptor):
        raise OSError("fstat failed")

    def close_after_fstat_failure(descriptor):
        closed_on_failure.append(descriptor)
        return real_close(descriptor)

    with monkeypatch.context() as local:
        local.setattr(operations.tempfile, "mkstemp", tracked_mkstemp)
        local.setattr(operations.os, "fstat", failing_fstat)
        local.setattr(operations.os, "close", close_after_fstat_failure)
        with pytest.raises(CadOperationError, match="identity could not be recorded"):
            operations._create_output_temporary(tmp_path / "probe-failure.step")
    assert closed_on_failure and created_on_failure
    created_on_failure[0].unlink(missing_ok=True)

    source = tmp_path / "part.step"
    source.write_bytes(b"part")
    destination = tmp_path / "translated.step"
    destination.write_bytes(b"old")
    backend = FakeBackend()
    _install_backend(monkeypatch, backend)
    document = _live_document(source, backend)
    with pytest.raises(TypeError):
        operations.write_cad(document, ExplosivePath(), options=object())  # type: ignore[arg-type]
    checks = 0

    def cancel_after_provider() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(CadOperationCancelled):
        operations.write_cad(
            document,
            destination,
            options=CadWriteOptions("translate"),
            cancellation=cancel_after_provider,
        )
    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob(".translated.anyfileio-*.step"))
    backend.raise_translate = RuntimeError("failed")
    with pytest.raises(CadOperationError):
        operations.write_cad(document, destination, options=CadWriteOptions("translate"))
    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob(".translated.anyfileio-*.step"))

    backend.raise_translate = None
    real_validate_report = operations._validate_translation_report
    replacement = None
    held_output = None

    def substitute_after_report(*args, **kwargs):
        nonlocal replacement, held_output
        report = real_validate_report(*args, **kwargs)
        replacement = backend.translate_calls[-1][1]
        held_output = replacement.with_name(f"{replacement.name}.held")
        replacement.replace(held_output)
        replacement.write_bytes(b"replacement-owned-by-someone-else")
        return report

    with monkeypatch.context() as local:
        local.setattr(operations, "_validate_translation_report", substitute_after_report)
        with pytest.raises(CadOperationError, match="provider replaced the core-owned output temporary") as caught:
            operations.write_cad(document, destination, options=CadWriteOptions("translate"))
    try:
        assert destination.read_bytes() == b"old"
        assert replacement is not None and replacement.read_bytes() == b"replacement-owned-by-someone-else"
        assert held_output is not None and held_output.read_bytes() == b"translated-step"
        assert any(
            "cleanup also failed:" in note and "identity changed; refusing removal" in note
            for note in getattr(caught.value, "__notes__", ())
        )
    finally:
        if replacement is not None:
            replacement.unlink(missing_ok=True)
        if held_output is not None:
            held_output.unlink(missing_ok=True)
    document.close()
