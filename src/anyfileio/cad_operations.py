"""Filesystem orchestration for the backend-neutral CAD protocol."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from typing import Callable, ContextManager, Iterator, TypeAlias

import numpy as np

from .cad import (
    CadAssetWriteReport,
    CadBackendProtocol,
    CadDiagnostic,
    CadDocument,
    CadEntityRef,
    CadError,
    CadManifest,
    CadOperationCancelled,
    CadOperationError,
    CadReadOptions,
    CadTessellationOptions,
    CadTessellationResult,
    CadValidationError,
    CadWriteOptions,
    CancellationCheck,
    _bind_tessellation,
)
from .cad_backend import _load_backend

PathLike: TypeAlias = str | os.PathLike[str]

__all__ = ["read_cad", "tessellate_cad", "write_cad"]

_BUFFER_SIZE = 1024 * 1024
_CAD_SUFFIXES = {
    ".brep": "brep",
    ".iges": "iges",
    ".igs": "iges",
    ".step": "step",
    ".stp": "step",
}
_STAT_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")


def _check_cancelled(cancellation: CancellationCheck) -> None:
    if cancellation is None:
        return
    if not callable(cancellation):
        raise TypeError("cancellation must be callable or None")
    try:
        requested = cancellation()
    except Exception as error:
        raise CadOperationError(
            "cancellation callback failed",
            code="cad.cancellation_check.failed",
        ) from error
    if requested:
        raise CadOperationCancelled(
            "CAD operation was cancelled",
            code="cad.operation.cancelled",
        )


def _stat_signature(value: os.stat_result) -> tuple[int, ...]:
    return tuple(int(getattr(value, name)) for name in _STAT_FIELDS)


def _file_identity(value: os.stat_result) -> tuple[int, int]:
    return (int(value.st_dev), int(value.st_ino))


def _path_handle_anchor(value: os.stat_result) -> tuple[int, int, int, int]:
    # Windows can expose different ctime values through pathname and open-handle
    # views.  Full signatures are compared within each view; this cross-view
    # anchor deliberately uses the four fields that have identical semantics.
    return (int(value.st_dev), int(value.st_ino), int(value.st_size), int(value.st_mtime_ns))


def _unlink_owned(path: pathlib.Path | None, identity: tuple[int, int] | None) -> None:
    if path is None:
        return
    try:
        state = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise CadOperationError("task-owned temporary could not be inspected for removal") from error
    if (
        identity is None
        or stat.S_ISLNK(state.st_mode)
        or not stat.S_ISREG(state.st_mode)
        or _file_identity(state) != identity
    ):
        raise CadOperationError("task-owned temporary identity changed; refusing removal")
    try:
        path.unlink()
    except OSError as error:
        raise CadOperationError("task-owned temporary could not be removed") from error


def _cleanup_after_failure(
    path: pathlib.Path | None,
    identity: tuple[int, int] | None,
    failure: BaseException,
) -> None:
    try:
        _unlink_owned(path, identity)
    except CadOperationError as cleanup_error:
        failure.add_note(f"cleanup also failed: {cleanup_error}")


def _sha256_file(path: pathlib.Path, cancellation: CancellationCheck = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(_BUFFER_SIZE)
            if not block:
                break
            digest.update(block)
            _check_cancelled(cancellation)
    return digest.hexdigest()


def _copy_source_snapshot(
    path: PathLike,
    *,
    expected_sha256: str | None = None,
    cancellation: CancellationCheck = None,
) -> tuple[pathlib.Path, str, tuple[int, int]]:
    source = pathlib.Path(path)
    if expected_sha256 is not None and (
        len(expected_sha256) != 64
        or expected_sha256.lower() != expected_sha256
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise CadValidationError("expected_sha256 must be 64 lower-case hexadecimal characters")
    try:
        path_before = source.stat()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise CadOperationError("CAD source could not be inspected") from error
    if not stat.S_ISREG(path_before.st_mode):
        raise CadValidationError("CAD source must be a regular file")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="anyfileio-cad-source-",
        suffix=source.suffix.lower(),
    )
    snapshot = pathlib.Path(temporary_name)
    snapshot_identity = _file_identity(os.fstat(descriptor))
    digest = hashlib.sha256()
    try:
        _check_cancelled(cancellation)
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            descriptor = -1
            opened_before = os.fstat(reader.fileno())
            path_opened_before = source.stat()
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or _path_handle_anchor(opened_before) != _path_handle_anchor(path_before)
                or _stat_signature(path_before) != _stat_signature(path_opened_before)
            ):
                raise CadOperationError("CAD source changed before snapshot", code="cad.source.changed")
            while True:
                block = reader.read(_BUFFER_SIZE)
                if not block:
                    break
                writer.write(block)
                digest.update(block)
                _check_cancelled(cancellation)
            writer.flush()
            os.fsync(writer.fileno())
            opened_after = os.fstat(reader.fileno())
        path_after = source.stat()
        if (
            _stat_signature(opened_before) != _stat_signature(opened_after)
            or _stat_signature(path_opened_before) != _stat_signature(path_after)
        ):
            raise CadOperationError("CAD source changed during snapshot", code="cad.source.changed")
        actual_sha256 = digest.hexdigest()
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise CadOperationError("CAD source checksum changed", code="cad.source.changed")
        return snapshot, actual_sha256, snapshot_identity
    except Exception as error:
        if descriptor >= 0:
            os.close(descriptor)
        _cleanup_after_failure(snapshot, snapshot_identity, error)
        raise


def _snapshot_source(path: PathLike, *, expected_sha256: str | None = None) -> pathlib.Path:
    """Copy one regular source file to core-owned immutable storage."""

    snapshot, _digest, _identity = _copy_source_snapshot(path, expected_sha256=expected_sha256)
    return snapshot


def _source_name(path: pathlib.Path) -> str:
    name = unicodedata.normalize("NFC", path.name)
    if not name or "/" in name or "\\" in name:
        raise CadValidationError("CAD source name must be a non-empty basename")
    return name


def _format_for_path(path: pathlib.Path, *, write: bool = False) -> str:
    source_format = _CAD_SUFFIXES.get(path.suffix.casefold())
    if source_format is None:
        code = "cad.write.format_suffix_mismatch" if write else None
        raise CadValidationError("path must use a core-known CAD suffix", code=code)
    return source_format


def _import_document_id(manifest: CadManifest) -> str:
    payload = {
        "backend_compatibility_version": manifest.backend_compatibility_version,
        "backend_id": manifest.backend_id,
        "effective_source_length_unit": manifest.source_length_unit,
        "heal": manifest.normalized_read_options.heal,
        "identity_kind": "import",
        "identity_version": 1,
        "source_format": manifest.source_format,
        "source_sha256": manifest.source_sha256,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"cad-import-v1:{hashlib.sha256(encoded).hexdigest()}"


def _require_capability(condition: bool, message: str) -> None:
    if not condition:
        raise CadOperationError(message)


def _close_new_document(document: object) -> None:
    if not isinstance(document, CadDocument) or document.closed:
        return
    document.close()


def _close_after_failure(document: object, failure: BaseException) -> None:
    try:
        _close_new_document(document)
    except Exception as close_error:
        failure.add_note(f"provider-session cleanup also failed: {type(close_error).__name__}: {close_error}")


def _validate_read_result(
    document: object,
    *,
    backend: CadBackendProtocol,
    snapshot: pathlib.Path,
    snapshot_identity: tuple[int, int],
    source_sha256: str,
    source_name: str,
    source_format: str,
    options: CadReadOptions,
    tessellation_options: CadTessellationOptions | None,
) -> CadDocument:
    if not isinstance(document, CadDocument):
        raise CadOperationError("CAD provider returned a non-document value")
    manifest = document.manifest
    if (
        manifest.source_sha256 != source_sha256
        or manifest.source_name != source_name
        or manifest.source_format != source_format
        or manifest.normalized_read_options != options
        or manifest.backend_id != backend.backend_id
        or manifest.backend_version != backend.backend_version
        or manifest.backend_compatibility_version != backend.backend_compatibility_version
        or manifest.document_id != _import_document_id(manifest)
    ):
        raise CadOperationError("CAD provider returned mismatched source identity")
    if options.mode != "manifest_only" and (
        manifest.source_length_unit == "unknown" or manifest.source_to_metre_scale is None
    ):
        raise CadOperationError("preview and live reads require known source units")
    try:
        snapshot_state = snapshot.lstat()
    except OSError as error:
        raise CadOperationError("owned source changed after provider read", code="cad.source.changed") from error
    if (
        stat.S_ISLNK(snapshot_state.st_mode)
        or not stat.S_ISREG(snapshot_state.st_mode)
        or _file_identity(snapshot_state) != snapshot_identity
        or _sha256_file(snapshot) != source_sha256
    ):
        raise CadOperationError("owned source changed after provider read", code="cad.source.changed")

    attached = getattr(document, "_source_snapshot", None)
    if attached is not None and pathlib.Path(attached) != snapshot:
        raise CadOperationError("provider attached a non-core source path")
    if options.retain_source:
        if attached is None or not document.source_available:
            raise CadOperationError("provider did not retain the core source snapshot")
    elif attached is not None and pathlib.Path(attached) != snapshot:
        raise CadOperationError("provider retained an unexpected source path")

    expected_options = tessellation_options
    if options.mode == "manifest_only":
        if not document.closed or document.owner_thread_id is not None or document.tessellation is not None:
            raise CadOperationError("manifest-only provider lifecycle is invalid")
    elif options.mode == "preview":
        if not document.closed or document.owner_thread_id is not None:
            raise CadOperationError("preview provider lifecycle is invalid")
        if document.tessellation is None or document.tessellation.options != expected_options:
            raise CadOperationError("preview provider tessellation is invalid")
    else:
        if document.closed or document.owner_thread_id != threading.get_ident():
            raise CadOperationError("live provider session is not owned by the caller thread")
        if expected_options is None:
            if document.tessellation is not None:
                raise CadOperationError("live read unexpectedly published tessellation")
        elif document.tessellation is None or document.tessellation.options != expected_options:
            raise CadOperationError("live provider tessellation is invalid")
    return document


def read_cad(
    source: PathLike,
    *,
    options: CadReadOptions = CadReadOptions(),
    tessellation_options: CadTessellationOptions | None = None,
    cancellation: CancellationCheck = None,
    backend_id: str = "occt",
) -> CadDocument:
    if not isinstance(options, CadReadOptions):
        raise TypeError("options must be CadReadOptions")
    if tessellation_options is not None and not isinstance(tessellation_options, CadTessellationOptions):
        raise TypeError("tessellation_options must be CadTessellationOptions or None")
    if backend_id != "occt":
        raise CadValidationError("protocol 1 supports backend_id='occt' only")
    if options.mode == "manifest_only" and tessellation_options is not None:
        raise CadValidationError("manifest-only reads cannot request tessellation")
    effective_tessellation = (
        CadTessellationOptions()
        if options.mode == "preview" and tessellation_options is None
        else tessellation_options
    )
    _check_cancelled(cancellation)

    source_path = pathlib.Path(source)
    source_format = _format_for_path(source_path)
    source_name = _source_name(source_path)
    try:
        source_state = source_path.stat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(source_state.st_mode):
        raise CadValidationError("CAD source must be a regular file")

    backend = _load_backend()
    capabilities = backend.capabilities
    _require_capability(source_format in capabilities.read_formats, "backend cannot read this CAD format")
    _require_capability(options.mode in capabilities.import_modes, "backend cannot perform this CAD read mode")
    if effective_tessellation is not None:
        _require_capability(capabilities.tessellate, "backend cannot tessellate CAD")

    snapshot: pathlib.Path | None = None
    snapshot_identity: tuple[int, int] | None = None
    returned: object | None = None
    try:
        snapshot, source_sha256, snapshot_identity = _copy_source_snapshot(
            source_path,
            cancellation=cancellation,
        )
        snapshot_state = snapshot.lstat()
        if (
            stat.S_ISLNK(snapshot_state.st_mode)
            or not stat.S_ISREG(snapshot_state.st_mode)
            or _file_identity(snapshot_state) != snapshot_identity
        ):
            raise CadOperationError("owned source snapshot is not a regular file", code="cad.source.changed")
        _check_cancelled(cancellation)
        try:
            returned = backend.read(
                snapshot,
                source_sha256=source_sha256,
                source_name=source_name,
                options=options,
                tessellation_options=effective_tessellation,
                cancellation=cancellation,
            )
        except CadError:
            raise
        except Exception as error:
            raise CadOperationError("CAD provider read failed") from error
        document = _validate_read_result(
            returned,
            backend=backend,
            snapshot=snapshot,
            snapshot_identity=snapshot_identity,
            source_sha256=source_sha256,
            source_name=source_name,
            source_format=source_format,
            options=options,
            tessellation_options=effective_tessellation,
        )
        _check_cancelled(cancellation)
        if not options.retain_source:
            if getattr(document, "_source_snapshot", None) == snapshot:
                document.release_source()
            else:
                _unlink_owned(snapshot, snapshot_identity)
            snapshot = None
            snapshot_identity = None
            if document.source_available:
                raise CadOperationError("unretained read still exposes source storage")
        else:
            snapshot = None  # ownership transferred to CadDocument
            snapshot_identity = None
        return document
    except Exception as error:
        _close_after_failure(returned, error)
        _cleanup_after_failure(snapshot, snapshot_identity, error)
        raise


def _same_prototype(left: object, right: object) -> bool:
    return (
        getattr(left, "id", None) == getattr(right, "id", None)
        and getattr(left, "cad_ref", None) == getattr(right, "cad_ref", None)
        and getattr(left, "name", None) == getattr(right, "name", None)
        and getattr(left, "shape_type", None) == getattr(right, "shape_type", None)
        and getattr(left, "local_bounds_m", None) == getattr(right, "local_bounds_m", None)
        and dict(getattr(left, "topology_counts", {})) == dict(getattr(right, "topology_counts", {}))
    )


def _same_occurrence(left: object, right: object) -> bool:
    return (
        getattr(left, "id", None) == getattr(right, "id", None)
        and getattr(left, "cad_ref", None) == getattr(right, "cad_ref", None)
        and getattr(left, "prototype_id", None) == getattr(right, "prototype_id", None)
        and getattr(left, "parent_id", None) == getattr(right, "parent_id", None)
        and np.array_equal(getattr(left, "local_transform", None), getattr(right, "local_transform", None))
        and np.array_equal(getattr(left, "accumulated_transform", None), getattr(right, "accumulated_transform", None))
        and getattr(left, "world_bounds_m", None) == getattr(right, "world_bounds_m", None)
        and getattr(left, "name", None) == getattr(right, "name", None)
        and getattr(left, "visible", None) == getattr(right, "visible", None)
    )


def _same_shape(left: object, right: object) -> bool:
    fields = (
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
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _same_reopen_manifest(original: CadManifest, reopened: CadManifest) -> bool:
    scalar_fields = (
        "document_id",
        "source_sha256",
        "source_name",
        "source_format",
        "source_length_unit",
        "source_to_metre_scale",
        "internal_length_unit",
        "root_occurrence_ids",
        "world_bounds_m",
        "external_references",
        "backend_id",
        "backend_version",
        "backend_compatibility_version",
        "binding_distribution",
        "binding_version",
        "occt_version",
    )
    if any(getattr(original, field) != getattr(reopened, field) for field in scalar_fields):
        return False
    if dict(original.topology_counts) != dict(reopened.topology_counts):
        return False
    if original.diagnostics != reopened.diagnostics:
        return False
    if len(original.prototypes) != len(reopened.prototypes) or not all(
        _same_prototype(left, right) for left, right in zip(original.prototypes, reopened.prototypes)
    ):
        return False
    if len(original.occurrences) != len(reopened.occurrences) or not all(
        _same_occurrence(left, right) for left, right in zip(original.occurrences, reopened.occurrences)
    ):
        return False
    return len(original.shapes) == len(reopened.shapes) and all(
        _same_shape(left, right) for left, right in zip(original.shapes, reopened.shapes)
    )


@contextmanager
def _operation_document(
    document: CadDocument,
    backend: CadBackendProtocol,
    cancellation: CancellationCheck,
) -> Iterator[CadDocument]:
    state = document._backend_state_for(document.manifest.backend_id)
    if state is not None:
        yield document
        return

    with document._borrow_source_snapshot() as retained_source:
        if _sha256_file(retained_source, cancellation) != document.manifest.source_sha256:
            raise CadOperationError("retained source changed", code="cad.source.reopen_mismatch")
        source_stat = retained_source.stat()
        reopened: object | None = None
        read_options = CadReadOptions(
            mode="live",
            retain_source=False,
            source_length_unit_override=document.manifest.normalized_read_options.source_length_unit_override,
            heal=document.manifest.normalized_read_options.heal,
        )
        operation_failure: BaseException | None = None
        try:
            try:
                reopened = backend.read(
                    retained_source,
                    source_sha256=document.manifest.source_sha256,
                    source_name=document.manifest.source_name,
                    options=read_options,
                    tessellation_options=None,
                    cancellation=cancellation,
                )
            except CadError:
                raise
            except Exception as error:
                raise CadOperationError("CAD transient reopen failed") from error
            if (
                not isinstance(reopened, CadDocument)
                or reopened.closed
                or reopened.owner_thread_id != threading.get_ident()
                or getattr(reopened, "_source_snapshot", None) is not None
                or reopened.manifest.normalized_read_options != read_options
                or not _same_reopen_manifest(document.manifest, reopened.manifest)
            ):
                raise CadOperationError("CAD transient reopen identity mismatched", code="cad.source.reopen_mismatch")
            yield reopened
        except BaseException as error:
            operation_failure = error
            raise
        finally:
            close_error: BaseException | None = None
            try:
                _close_new_document(reopened)
            except BaseException as error:
                close_error = error

            source_error: BaseException | None = None
            try:
                if (
                    _stat_signature(source_stat) != _stat_signature(retained_source.stat())
                    or _sha256_file(retained_source) != document.manifest.source_sha256
                ):
                    raise CadOperationError("retained source changed during reopen", code="cad.source.reopen_mismatch")
            except BaseException as error:
                source_error = error

            if operation_failure is not None:
                if close_error is not None:
                    operation_failure.add_note(
                        f"transient-session cleanup also failed: {type(close_error).__name__}: {close_error}"
                    )
                if source_error is not None:
                    operation_failure.add_note(
                        f"retained-source verification also failed: {type(source_error).__name__}: {source_error}"
                    )
            elif close_error is not None:
                if source_error is not None:
                    close_error.add_note(
                        f"retained-source verification also failed: {type(source_error).__name__}: {source_error}"
                    )
                raise close_error
            elif source_error is not None:
                raise source_error


def _backend_for_document(document: CadDocument) -> CadBackendProtocol:
    backend = _load_backend()
    manifest = document.manifest
    if (
        backend.backend_id != manifest.backend_id
        or backend.backend_version != manifest.backend_version
        or backend.backend_compatibility_version != manifest.backend_compatibility_version
    ):
        raise CadOperationError("loaded backend does not match the CAD document")
    return backend


def tessellate_cad(
    document: CadDocument,
    *,
    options: CadTessellationOptions = CadTessellationOptions(),
    cancellation: CancellationCheck = None,
) -> CadTessellationResult:
    if not isinstance(document, CadDocument):
        raise TypeError("document must be CadDocument")
    if not isinstance(options, CadTessellationOptions):
        raise TypeError("options must be CadTessellationOptions")
    _check_cancelled(cancellation)
    if document.closed and not document.source_available:
        raise CadOperationError("retained source is unavailable", code="cad.source.unavailable")
    if document.manifest.source_length_unit == "unknown":
        raise CadValidationError("tessellation requires known source units")
    backend = _backend_for_document(document)
    capabilities = backend.capabilities
    _require_capability(capabilities.tessellate, "backend cannot tessellate CAD")
    _require_capability(document.manifest.source_format in capabilities.read_formats, "backend cannot reopen source format")
    _require_capability("live" in capabilities.import_modes, "backend cannot open a live CAD session")
    with _operation_document(document, backend, cancellation) as operation_document:
        _check_cancelled(cancellation)
        try:
            meshes = backend.tessellate(
                operation_document,
                options=options,
                cancellation=cancellation,
            )
        except CadError:
            raise
        except Exception as error:
            raise CadOperationError("CAD provider tessellation failed") from error
        _check_cancelled(cancellation)
        if not isinstance(meshes, tuple):
            raise CadOperationError("CAD provider tessellation result must be a tuple")
        try:
            return _bind_tessellation(document.manifest, options, meshes)
        except CadError:
            raise
        except Exception as error:
            raise CadOperationError("CAD provider tessellation result is invalid") from error


def _manifest_entities(manifest: CadManifest) -> tuple[CadEntityRef, ...]:
    entities = {
        *(prototype.cad_ref for prototype in manifest.prototypes),
        *(occurrence.cad_ref for occurrence in manifest.occurrences),
        *(shape.cad_ref for shape in manifest.shapes),
    }
    return tuple(sorted(entities, key=lambda item: (item.kind, item.local_id, item.document_id)))


def _destination_target(document: CadDocument, destination: PathLike, options: CadWriteOptions) -> tuple[pathlib.Path, str, str]:
    target = pathlib.Path(destination)
    suffix_format = _format_for_path(target, write=True)
    target_format = options.target_format or document.manifest.source_format
    if suffix_format != target_format:
        raise CadValidationError(
            "destination suffix disagrees with target format",
            code="cad.write.format_suffix_mismatch",
        )
    target_unit = options.target_length_unit or document.manifest.source_length_unit
    return target, target_format, target_unit


def _create_output_temporary(destination: pathlib.Path) -> tuple[pathlib.Path, tuple[int, int]]:
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"destination directory does not exist: {parent}")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.stem}.anyfileio-",
        suffix=destination.suffix.lower(),
        dir=parent,
    )
    temporary = pathlib.Path(name)
    identity_error: BaseException | None = None
    try:
        identity = _file_identity(os.fstat(descriptor))
    except BaseException as error:
        identity_error = error
    if identity_error is not None:
        try:
            os.close(descriptor)
        except OSError as close_error:
            identity_error.add_note(f"temporary descriptor close also failed: {close_error}")
        if isinstance(identity_error, Exception):
            raise CadOperationError("core-owned output identity could not be recorded") from identity_error
        raise identity_error
    try:
        os.close(descriptor)
    except OSError as close_error:
        failure = CadOperationError("core-owned output temporary handle could not be closed")
        _cleanup_after_failure(temporary, identity, failure)
        raise failure from close_error
    return temporary, identity


def _validate_owned_output(path: pathlib.Path, identity: tuple[int, int]) -> None:
    try:
        path_state = path.lstat()
    except OSError as error:
        raise CadOperationError("core-owned output temporary is unavailable") from error
    if stat.S_ISLNK(path_state.st_mode) or not stat.S_ISREG(path_state.st_mode) or _file_identity(path_state) != identity:
        raise CadOperationError("provider replaced the core-owned output temporary")


def _flush_owned_output(path: pathlib.Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _preserve_report(document: CadDocument, target_format: str, target_unit: str, output_sha256: str) -> CadAssetWriteReport:
    manifest = document.manifest
    return CadAssetWriteReport(
        source_document_id=manifest.document_id,
        mode="preserve",
        source_format=manifest.source_format,
        target_format=target_format,
        source_length_unit=manifest.source_length_unit,
        target_length_unit=target_unit,
        backend_id=manifest.backend_id,
        backend_version=manifest.backend_version,
        backend_compatibility_version=manifest.backend_compatibility_version,
        binding_version=None,
        occt_version=None,
        output_sha256=output_sha256,
        byte_identical=True,
        source_topology_counts=manifest.topology_counts,
        output_topology_counts=manifest.topology_counts,
        healing_applied=False,
        geometry_changed=False,
        exported_entities=_manifest_entities(manifest),
        unsupported_entities=(),
        approximations=(),
        metadata_losses=(),
        diagnostics=(),
        execution_mode="preserve_copy",
    )


def _known_manifest_entities(manifest: CadManifest) -> frozenset[CadEntityRef]:
    return frozenset(_manifest_entities(manifest))


def _validate_translation_report(
    report: object,
    *,
    document: CadDocument,
    backend: CadBackendProtocol,
    options: CadWriteOptions,
    target_format: str,
    target_unit: str,
    output_sha256: str,
) -> CadAssetWriteReport:
    if not isinstance(report, CadAssetWriteReport):
        raise CadOperationError("CAD provider returned a non-report value")
    manifest = document.manifest
    if (
        report.source_document_id != manifest.document_id
        or report.mode != "translate"
        or report.source_format != manifest.source_format
        or report.target_format != target_format
        or report.source_length_unit != manifest.source_length_unit
        or report.target_length_unit != target_unit
        or report.backend_id != backend.backend_id
        or report.backend_version != backend.backend_version
        or report.backend_compatibility_version != backend.backend_compatibility_version
        or report.binding_version != manifest.binding_version
        or report.occt_version != manifest.occt_version
        or report.output_sha256 != output_sha256
        or report.execution_mode != "provider_translation"
        or dict(report.source_topology_counts) != dict(manifest.topology_counts)
        or (not options.heal and report.healing_applied)
    ):
        raise CadOperationError("CAD provider translation report mismatched the request")
    known = _known_manifest_entities(manifest)
    exported = frozenset(report.exported_entities)
    unsupported = frozenset(report.unsupported_entities)
    if exported & unsupported or exported | unsupported != known:
        raise CadOperationError("CAD provider report entity coverage is inconsistent")
    if any(entity not in known for diagnostic in report.diagnostics for entity in diagnostic.entities):
        raise CadOperationError("CAD provider diagnostic references an unknown entity")
    return report


def write_cad(
    document: CadDocument,
    destination: PathLike,
    *,
    options: CadWriteOptions,
    cancellation: CancellationCheck = None,
) -> CadAssetWriteReport:
    # Binding requirement: these two checks precede even PathLike coercion.
    if not isinstance(document, CadDocument):
        raise TypeError("document must be CadDocument")
    if not isinstance(options, CadWriteOptions):
        raise TypeError("options must be CadWriteOptions")
    _check_cancelled(cancellation)
    destination_path, target_format, target_unit = _destination_target(document, destination, options)

    if options.mode == "preserve":
        if document.manifest.normalized_read_options.heal:
            raise CadValidationError("a healed read cannot be byte-preserved", code="cad.preserve.healed_source")
        if options.heal:
            raise CadValidationError("preserve cannot request healing")
        if target_format != document.manifest.source_format or (
            options.target_length_unit is not None and target_unit != document.manifest.source_length_unit
        ):
            raise CadValidationError("preserve requires source format and units")
        temporary: pathlib.Path | None = None
        temporary_identity: tuple[int, int] | None = None
        try:
            with document._borrow_source_snapshot() as retained_source:
                if _sha256_file(retained_source, cancellation) != document.manifest.source_sha256:
                    raise CadOperationError("retained source changed", code="cad.source.changed")
                temporary, temporary_identity = _create_output_temporary(destination_path)
                with retained_source.open("rb") as reader, temporary.open("r+b") as writer:
                    writer.seek(0)
                    writer.truncate()
                    while True:
                        block = reader.read(_BUFFER_SIZE)
                        if not block:
                            break
                        writer.write(block)
                        _check_cancelled(cancellation)
                    writer.flush()
                    os.fsync(writer.fileno())
                _validate_owned_output(temporary, temporary_identity)
                output_sha256 = _sha256_file(temporary, cancellation)
                _validate_owned_output(temporary, temporary_identity)
                if output_sha256 != document.manifest.source_sha256:
                    raise CadOperationError("preserve output checksum mismatched source")
                report = _preserve_report(document, target_format, target_unit, output_sha256)
                _check_cancelled(cancellation)
                _validate_owned_output(temporary, temporary_identity)
                os.replace(temporary, destination_path)
                temporary = None
                temporary_identity = None
                return report
        except Exception as error:
            _cleanup_after_failure(temporary, temporary_identity, error)
            raise

    if document.manifest.source_length_unit == "unknown":
        raise CadValidationError("translation requires known source units")
    backend = _backend_for_document(document)
    capabilities = backend.capabilities
    _require_capability(capabilities.translate, "backend cannot translate CAD")
    _require_capability(target_format in capabilities.write_formats, "backend cannot write target CAD format")
    _require_capability(document.manifest.source_format in capabilities.read_formats, "backend cannot reopen source CAD format")
    _require_capability("live" in capabilities.import_modes, "backend cannot open a live CAD session")

    temporary: pathlib.Path | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        temporary, temporary_identity = _create_output_temporary(destination_path)
        with _operation_document(document, backend, cancellation) as operation_document:
            _check_cancelled(cancellation)
            try:
                raw_report = backend.translate(
                    operation_document,
                    temporary,
                    options=options,
                    cancellation=cancellation,
                )
            except CadError:
                raise
            except Exception as error:
                raise CadOperationError("CAD provider translation failed") from error
        _validate_owned_output(temporary, temporary_identity)
        _flush_owned_output(temporary)
        _validate_owned_output(temporary, temporary_identity)
        output_sha256 = _sha256_file(temporary, cancellation)
        _validate_owned_output(temporary, temporary_identity)
        report = _validate_translation_report(
            raw_report,
            document=document,
            backend=backend,
            options=options,
            target_format=target_format,
            target_unit=target_unit,
            output_sha256=output_sha256,
        )
        _check_cancelled(cancellation)
        _validate_owned_output(temporary, temporary_identity)
        os.replace(temporary, destination_path)
        temporary = None
        temporary_identity = None
        return report
    except Exception as error:
        _cleanup_after_failure(temporary, temporary_identity, error)
        raise
