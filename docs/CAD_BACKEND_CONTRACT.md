# CAD backend contract

Status: frozen protocol contract for the ANYfileIO 0.2 / ANYfileio-occt 0.1
implementation line.

This document is governed by the source plan SHA-256
`473523BD3BD28FC88487A961C29BF7B640592F415B981236C558FA963AF1E414`
and the registered baseline addendum SHA-256
`9249191E78C746A81A2B7D80B8ADA543AD45FCAE9CA41F5CAB04E169D68796A1`.
The accepted ANYfileIO parent is
`0d2c7f8ef1b17f42f667d6183125e51cb650a70d`. This document defines a
contract; it is not test, wheel, resolver, or performance evidence.

## 1. Names and versions

The following identifiers are distinct and must not be substituted for one
another.

| Concept | Frozen value |
| --- | --- |
| Lightweight repository | `ANYfileIO` |
| Lightweight distribution | `ANYfileio` |
| Lightweight import package | `anyfileio` |
| Heavy repository/distribution | `ANYfileio-occt` |
| Heavy import package | `anyfileio_occt` |
| Backend id | `occt` |
| CAD backend protocol version | `1` |
| OCCT backend compatibility version | `1` |
| Preview artifact schema | `anyfileio.cad-preview` version `1` |
| Entry-point group | `anyfileio.backends` |
| Entry-point name | `occt` |
| Entry-point target | `anyfileio_occt.backend:get_backend` |

The protocol version covers public call shapes, records, failure semantics,
units, and array conventions. The backend compatibility version covers
backend-produced identities and derived caches. The preview format has its own
schema version and exact version-1 envelope in section 5. ANYfem project format
7 is a consumer contract and is not an ANYfileIO artifact schema.

An incompatible protocol change increments the protocol version. A change
that invalidates `CadEntityRef` or a backend-derived cache increments backend
compatibility. A persisted-schema change increments that schema and supplies a
migration or explicit rejection. Version equality is required; the core never
guesses compatibility.

## 2. Discovery and loading

`anyfileio` uses `importlib.metadata` to enumerate entry-point metadata. It
does not import a provider while importing the core, describing a format,
listing known formats, or performing a built-in FEM operation.

Discovery has these process-lifetime states, represented by the exact lower-case
strings shown:

- `missing`: no entry point with the exact group and name;
- `discovered`: exactly one matching entry point, not yet loaded;
- `duplicate`: more than one matching entry point; fail closed;
- `ready`: `get_backend()` was loaded for an operation and the returned
  provider passed all checks;
- `broken`: loading, construction, or validation failed;
- `incompatible`: id, protocol version, or compatibility version differed.

Entry-point enumeration is cached after the first metadata query. A matching
provider is loaded only when a requested CAD operation needs it. The loaded
provider or its terminal failure is cached so repeated calls do not repeatedly
scan metadata or execute broken import code. Tests may reset this cache through
a private test-only hook; cache reset is not public API.

`BackendStatus` is a frozen, slots-based record with exact fields:

```text
backend_id: str
state: "missing" | "discovered" | "duplicate" | "ready" | "broken" |
       "incompatible"
entry_point: str | None
distribution: str | None
expected_protocol_version: int
observed_protocol_version: int | None
expected_backend_compatibility_version: int
observed_backend_compatibility_version: int | None
capabilities: CadCapabilities | None
diagnostic: CadDiagnostic | None
```

`backend_status()` reports this record without forcing a provider import.
`known_formats()` includes the built-in formats and the
core-known optional CAD descriptors. `available_formats()` includes built-ins
and only CAD formats whose backend reached `ready` earlier in the process; a
merely `discovered` provider is not claimed operational. Requesting a CAD
operation is the action that may move it to `ready`, `broken`, or
`incompatible`.

The exact core exception hierarchy is:

```text
CadError(RuntimeError)
├── CadBackendError
│   ├── BackendUnavailableError
│   ├── BackendDuplicateError
│   ├── BackendLoadError
│   └── BackendCompatibilityError
├── CadOperationError
│   ├── CadValidationError
│   └── CadOperationCancelled
└── CadArtifactError
```

Every instance exposes a stable `code: str` and `diagnostic: CadDiagnostic`.
A missing backend raises `BackendUnavailableError` with code
`cad.backend.missing` and install hint `pip install "ANYfileio-occt"`.
Duplicate, broken, and incompatible providers use respectively
`cad.backend.duplicate`, `cad.backend.load_failed`, and
`cad.backend.incompatible`. Diagnostics record backend id, operation, state,
expected/observed versions, distribution or entry point when known, and causal
exception text. Such a failure must not disable a built-in FEM/SIF/CalculiX
reader or make `import anyfileio` fail.

## 3. Formats and capabilities

`FormatDescriptor` is an immutable, slots-based core record with these fields:

```text
name: str
suffixes: tuple[str, ...]
kind: str
capabilities: frozenset[str]
backend_id: str | None
provider_distribution: str | None
install_hint: str | None
```

Suffixes are lower-case, include the leading dot, are unique, and are indexed
once for constant-time lookup. Existing built-in descriptors retain their
current dispatch. The core additionally knows:

| Format | Suffixes | Kind | Required capabilities |
| --- | --- | --- | --- |
| STEP | `.step`, `.stp` | `cad_brep` | read, write, inspect, assembly, tessellate |
| IGES | `.iges`, `.igs` | `cad_brep` | read, write, inspect, tessellate |
| BREP | `.brep` | `cad_brep_native` | read/write optional and capability-gated |

All three descriptors are core-known without the heavy distribution, so the
first `.brep` request has an unambiguous route to backend id `occt`. STEP and
IGES operations are required provider capabilities. BREP availability remains
false until a loaded provider declares the requested BREP capability; no code
assumes it is implemented.

`CadCapabilities` is an immutable core record containing normalized
`read_formats`, `write_formats`, and `import_modes` frozensets plus boolean
flags for `inspect`, `assembly`, `tessellate`, `preserve`, and `translate`.
Format names are the canonical lower-case names `step`, `iges`, and optional
`brep`. Import modes are `manifest_only`, `preview`, and `live`. A provider
must reject an undeclared format/mode/operation before reading or writing data.

## 4. Backend-neutral public boundary

The lightweight package owns these public types:

```text
FormatDescriptor
BackendStatus and structured backend errors
LengthUnit and CancellationCheck aliases
CadBackendProtocol
CadCapabilities
CadReadOptions
CadWriteOptions
CadTessellationOptions
CadManifest
CadPrototypeRecord
CadOccurrenceRecord
CadShapeRecord
CadEntityRef
CadTessellation
CadPrototypeMesh
CadTessellationResult
CadDiagnostic
CadAssetWriteReport
CadDocument
```

They are dependency-free apart from NumPy. Public annotations and stored
values contain no OCP, CadQuery, `anyfileio_occt`, or ANYgeometry type. In
particular, core code never imports, copies, recreates, string-shadows, or
validates ANYgeometry `EntityHandle` identity or its entity-kind catalogue.
The adapter-only structural report is specified in
[`ANYGEOMETRY_0_2_ADAPTER.md`](ANYGEOMETRY_0_2_ADAPTER.md).

### 4.1 Normalized options

All option records are frozen and slots-based. Normalization occurs before a
cache key is made or a provider is called.

```text
CadReadOptions
    mode: "manifest_only" | "preview" | "live" = "preview"
    retain_source: bool = True
    source_length_unit_override: LengthUnit | None = None
    heal: bool = False

CadWriteOptions
    mode: "preserve" | "translate"
    target_format: "step" | "iges" | "brep" | None
    target_length_unit: LengthUnit | None = None
    heal: bool = False

CadTessellationOptions
    linear_deflection: positive finite float = 0.001
    angular_deflection: positive finite float = 0.35
    relative_deflection: bool = False
    parallel: bool = False
    include_edges: bool = False
    generate_normals: bool = True
    precision_policy: "auto" | "float32" | "float64" = "auto"
```

Healing is always explicit and defaults off. `preserve` rejects a changed
source, a target format different from the source, or any option that would
rewrite content. `translate` never claims byte equivalence. Unknown option
keys and non-finite values fail before provider invocation.

For an imported document, `CadWriteOptions.target_format=None` resolves to
`document.manifest.source_format`. The final destination must have exactly one
core-known CAD suffix whose canonical format equals the resolved target;
missing/unknown suffixes or an explicit target/suffix disagreement fail before
temporary-file creation with `CadValidationError` code
`cad.write.format_suffix_mismatch`. A translation temporary sibling retains
that same final suffix after its unique temporary stem so the provider never
has to infer a format from a generic `.tmp` suffix. Preserve additionally
requires the resolved target to equal the source format,
`target_length_unit` to be `None` or the normalized source unit, and both the
originating read and write `heal` values to be false. A document whose
`normalized_read_options.heal` is true cannot be byte-preserved and fails with
code `cad.preserve.healed_source`; translation is the only allowed write path.

When `relative_deflection` is false, `linear_deflection` is in SI metres. When
it is true, it is a dimensionless fraction of the prototype-local bounding-box
diagonal and the effective deflection is computed independently for each
prototype. `angular_deflection` is in radians. A zero-size prototype fails
relative tessellation explicitly. Parallel execution may change scheduling but
must be canonicalized to the identical prototype, owner, and triangle ordering
as serial execution.

`LengthUnit` is the exact canonical token set below. Input normalization strips
surrounding whitespace and applies Unicode case-folding before alias lookup.

| Canonical token | Metres per unit | Accepted aliases in protocol 1 |
| --- | ---: | --- |
| `um` | `1e-6` | `um`, `µm`, `μm`, `micrometre(s)`, `micrometer(s)` |
| `mm` | `1e-3` | `mm`, `millimetre(s)`, `millimeter(s)` |
| `cm` | `1e-2` | `cm`, `centimetre(s)`, `centimeter(s)` |
| `m` | `1` | `m`, `metre(s)`, `meter(s)` |
| `km` | `1000` | `km`, `kilometre(s)`, `kilometer(s)` |
| `in` | `0.0254` | `in`, `inch`, `inches` |
| `ft` | `0.3048` | `ft`, `foot`, `feet` |

An imported source may report canonical `unknown` with
`source_to_metre_scale=None`. `manifest_only` and byte-preserving output may
retain unknown units, but preview, tessellation, translation, or structural
export must receive an unambiguous supported unit. A read override replaces
only missing/unknown source metadata and is recorded; disagreement with an
explicit source declaration fails unless a future separately reviewed policy
permits override. `internal_length_unit` is always `m`.

For translation, `target_length_unit=None` means the known normalized source
unit. It is invalid when the source unit is unknown. Preserve ignores no unit
metadata: it requires the same source format and bytes and reports the retained
source unit, including `unknown`. No provider or consumer invents a scale from
bounds, filename, locale, ANYfem display units, or an OCCT default.

### 4.2 Identity and records

`CadEntityRef(document_id, kind, local_id)` is immutable CAD-document identity
for both imported and adapter-generated CAD. `kind` is one of the exact
protocol-1 values below; the core validates it but does not import another
package's entity-kind catalogue. `local_id` is a positive deterministic
integer. Equality and hashing use all three fields, so a local id can never
resolve against another document.

`document_id` has one of two forms:

```text
cad-import-v1:<64 lower-case SHA-256 hex>
cad-geometry-v1:<64 lower-case SHA-256 hex>
```

For imported CAD, the hash input is canonical UTF-8 JSON (`sort_keys=True`,
compact separators, `ensure_ascii=False`, `allow_nan=False`) of:

```text
identity_kind = "import"
identity_version = 1
source_sha256
source_format
effective_source_length_unit
heal
backend_id
backend_compatibility_version
```

`mode`, `retain_source`, tessellation settings, cache location, source filename,
and execution scheduling are deliberately excluded: manifest, preview, and
live reads of the same interpreted topology retain the same references. A
source-unit override and healing are included because they change the
interpreted coordinates or topology. The geometry-generated derivation is
frozen in the adapter contract.

Local identity generation uses XDE label path when available, prototype
identity, occurrence path, shape kind, and deterministic traversal index.
Python's randomized hash and complete tessellation arrays are not identity
inputs. Traversal builds bounded path components rather than quadratic
concatenated strings.

The complete protocol-1 `kind` vocabulary, in canonical order, is:

```text
prototype, occurrence, part, sheet, member,
solid, shell, face, wire, edge, vertex
```

No provider-defined synonym or additional kind is valid under protocol 1.
For imported documents, within each kind `local_id` is the one-based rank of
the canonical identity key below; gaps and hash-derived integers are
forbidden. Generated structural ids follow the separately frozen source-id
mapping below.

For imported CAD the keys are:

- `part`: the XDE product label entry parsed as a tuple of non-negative integer
  components;
- `occurrence`: the tuple of one-based child ordinals from a root assembly to
  the occurrence, with roots ordered by XDE label entry;
- `prototype`: the referenced shape label entry, or for an unlabelled unique
  shape the first canonical occurrence path that references it;
- `solid`, `shell`, `face`, `wire`, `edge`, `vertex`: tuple
  `(prototype_local_id, top_exp_index)`, where `top_exp_index` is the one-based
  index returned by one `TopExp::MapShapes(prototype_shape, requested_kind)`
  call for that exact kind under the frozen OCCT backend compatibility version.

Keys are compared lexicographically as integer tuples. The provider builds one
table for every kind during its single canonical XDE/prototype traversal; all
import, tessellation-owner, report, and artifact code consumes those shared
tables and never reallocates references independently. `CadOccurrenceRecord`
therefore carries its own `cad_ref: CadEntityRef` of kind `occurrence`.
Every imported `CadPrototypeRecord.id` equals its kind-`prototype`
`cad_ref.local_id`; every imported `CadOccurrenceRecord.id` equals its
kind-`occurrence` `cad_ref.local_id`. `parent_id`, `prototype_id`, root ids,
shape foreign keys, artifact directory ids, and occurrence-array rows all use
those same record ids. No second provider-local id allocation is permitted.

For generated structural CAD, primary reference keys and ids are the source
public ids: `Part -> part`, `Sheet -> sheet`, `Member -> member`, geometry
`Face -> face`, `Edge -> edge`, and `Vertex -> vertex`. Thus their `local_id`
equals the source `EntityHandle.id`. A generated `shell` ref has the owning
Sheet id and a generated `wire` ref has the owning Member id. Each surviving
Part creates one prototype and one root occurrence; both helper local ids equal
that Part id, and their record `id` equals the helper ref's `local_id`. If loose
unowned geometry survives, its single `UnownedGeometry` prototype and root
occurrence use `1 + max(surviving Part ids)`, or `1` when no Part survives.
Root occurrence order is ascending by helper local id. `solid` is never
generated by the structural adapter. Attachments, Junctions, FaceUses,
Coedges, and MemberEdgeUses are relationship/ownership evidence, not separate
CAD refs. They never enter `geometry_to_cad` or `cad_to_geometry` under their
own handles. FaceUse/Coedge evidence is carried as in-file metadata on the
owning Sheet CAD object and MemberEdgeUse evidence on the owning Member CAD
object. Attachment/Junction evidence is document-level metadata because it can
have multiple participants and no unique primary carrier. When a writer cannot
retain that metadata, owner-bound diagnostics/loss records identify the real
relationship handles and all participant handles; no arbitrary participant is
selected as its mapping.

A source entity that creates several lower-level OCCT helpers retains one
primary ref of the source-mapped kind. Helpers are not substituted into
`geometry_to_cad`. Changing the vocabulary, XDE key extraction, TopExp ranking,
or structural mapping is a backend-compatibility change, not a patch-level
implementation detail.

The following immutable records are the minimum stable fields. Metadata is a
deeply immutable mapping of JSON-scalar values and small tuples; numerical
geometry never appears in metadata.

```text
CadPrototypeRecord
    id: positive int
    cad_ref: CadEntityRef
    name: str
    shape_type: str
    local_bounds_m: Bounds | None
    topology_counts: immutable Mapping[str, int]

CadOccurrenceRecord
    id: positive int
    cad_ref: CadEntityRef of kind "occurrence"
    prototype_id: positive int
    parent_id: positive int | None
    local_transform: read-only float64 array, shape (4, 4)
    accumulated_transform: read-only float64 array, shape (4, 4)
    world_bounds_m: Bounds | None
    name: str
    visible: bool

CadShapeRecord
    cad_ref: CadEntityRef
    prototype_id: positive int
    occurrence_id: positive int | None
    parent_ref: CadEntityRef | None
    name: str
    shape_type: str
    prototype_local_bounds_m: Bounds | None
    world_bounds_m: Bounds | None
    color_rgba: tuple[float, float, float, float] | None
    layers: tuple[str, ...]

CadManifest
    document_id: str
    source_sha256: 64 lower-case hex
    source_name: str
    source_format: "step" | "iges" | "brep"
    source_length_unit: LengthUnit | "unknown"
    source_to_metre_scale: positive float | None
    internal_length_unit: Literal["m"]
    root_occurrence_ids: tuple[int, ...]
    prototypes: tuple[CadPrototypeRecord, ...]
    occurrences: tuple[CadOccurrenceRecord, ...]
    shapes: tuple[CadShapeRecord, ...]
    world_bounds_m: Bounds | None
    topology_counts: immutable Mapping[str, int]
    external_references: tuple[str, ...]
    diagnostics: tuple[CadDiagnostic, ...]
    normalized_read_options: CadReadOptions
    backend_id: Literal["occt"]
    backend_version: str
    backend_compatibility_version: Literal[1]
    binding_distribution: Literal["cadquery-ocp-novtk"]
    binding_version: str
    occt_version: str

CadDiagnostic
    code: str
    severity: "info" | "warning" | "error" | "fatal"
    message: str
    entities: tuple[CadEntityRef, ...]
    details: immutable Mapping[str, JSON scalar or tuple]
```

`backend_version` is the installed `ANYfileio-occt` distribution version,
`binding_version` is the installed `cadquery-ocp-novtk` distribution version,
and `occt_version` is the runtime Open CASCADE version reported by the binding.
They are never conflated.

`Bounds` is six finite float64 values
`(xmin, ymin, zmin, xmax, ymax, zmax)` with ordered minima/maxima. Prototype
bounds are prototype-local SI metres; occurrence and manifest bounds are world
SI metres. A missing or unavailable bound is `None`, never NaN. Topology-count
keys are exactly `solid`, `shell`, `face`, `wire`, `edge`, and `vertex`, each a
non-negative integer. IDs, layer names, external references, diagnostics, and
record tuples are canonicalized deterministically.

Both transform arrays are finite affine column-vector matrices with last row
exactly `(0, 0, 0, 1)` after normalization and a nonsingular upper-left 3-by-3
block. Translation entries are SI metres. `local_transform` maps prototype or
child coordinates into the parent occurrence; roots use their document-local
placement. `accumulated_transform` is retained and must equal
`parent.accumulated_transform @ local_transform`, or `local_transform` for a
root. Mirrors are allowed; singular transforms are not.

### 4.3 Compact tessellation

`CadTessellation` contains:

```text
origin:          float64, shape (3,)
positions:       float32 or float64, shape (n, 3)
triangles:       uint32 or uint64, shape (m, 3)
normals:         None or float32, shape (n, 3)
face_owners:     tuple[CadEntityRef, ...], length k
face_offsets:    int64, shape (k + 1,)
edge_indices:    None or uint32/uint64, shape (e, 2)
edge_owners:     tuple[CadEntityRef, ...], length q
edge_offsets:    int64, shape (q + 1,)
precision:       "float32" or "float64"
```

Arrays are C-contiguous, finite where floating point, normalized to native
byte order, and read-only before publication. Indices are zero-based and in
range. `uint32` is required when counts fit; `uint64` is the overflow path.
Triangles are grouped in canonical `face_owners` order. `face_offsets` is
monotonic, starts at zero, has exactly `len(face_owners) + 1` elements, and
ends at `len(triangles)`; triangles in
`triangles[face_offsets[i]:face_offsets[i + 1]]` belong to
`face_owners[i]`. A face owner occurs once and owns a non-empty range.

When `edge_indices is None`, `edge_owners` is empty and `edge_offsets` is the
single value `[0]`. Otherwise edges are grouped by canonical `edge_owners`
with the identical offset invariant and `edge_offsets[-1] == len(edge_indices)`.
An edge owner occurs once and owns a non-empty range. Ownership is therefore a
compact range table, not one Python or object-dtype value per primitive.

Triangle winding and normals are normalized to the outward orientation of the
represented CAD face after applying face orientation and triangulation-local
location. Degenerate triangles and untessellated faces are excluded from arrays
and produce owner-bound diagnostics; they are never silently claimed present.

`CadPrototypeMesh(prototype_id, tessellation, local_bounds_m, diagnostics)`
binds one tessellation and a tuple of diagnostics to one prototype. A prototype
is tessellated once. Occurrences reference the prototype and never persist
transformed copies of its vertices.

`CadTessellationResult(source_identity, options, prototype_meshes)` is a
frozen, slots-based core record. `source_identity` has form
`cad-tessellation-source-v1:<64 lower-case SHA-256 hex>`. Its hash input is
canonical UTF-8 JSON of exactly `source_sha256`, `source_name`,
`source_format`, `effective_source_length_unit`, the complete normalized
`CadReadOptions`, `backend_id`, `backend_version`,
`backend_compatibility_version`, `binding_distribution`, `binding_version`,
and `occt_version`, using sorted keys, compact separators,
`ensure_ascii=False`, and `allow_nan=False`. `options` is the exact normalized
`CadTessellationOptions` that produced the tuple. `prototype_meshes` contains
exactly one mesh for every manifest prototype in ascending prototype id;
owners, prototype ids, and diagnostics must belong to that document.

Only the core binds this record, immediately around a provider tuple returned
for the exact source/document operation or while opening a validated artifact.
Providers never construct or relabel `CadTessellationResult`. A detached result
therefore carries all source/reader/producer values needed to prove it matches
a target manifest before caching; `document_id` alone is insufficient.

For imported CAD, all exposed preview geometry is in SI metres. Source length
conversion is applied once before rebasing. With column vectors, a local
occurrence matrix maps child coordinates to its parent, accumulated transforms
are `parent_accumulated @ child_local`, and world preview coordinates are:

```text
world = occurrence_accumulated @ [origin + positions, 1]
```

`origin + positions` reconstructs prototype-local SI coordinates. Origins and
transforms are float64. Positions and normals may be float32 only when the
effective tessellation tolerance remains satisfied across the prototype local
bounds; otherwise positions are float64. `precision="float32"` is rejected when
that proof fails, while `auto` selects float64. World-coordinate expansion is
transient and bounded to rendering or export.

For an occurrence linear block `A`, world normals are
`normalize(inv(A).T @ local_normal)`; multiplying positions and normals by the
same matrix under non-uniform scale is forbidden. If `det(A) < 0`, world
triangle expansion swaps columns 1 and 2 after transforming positions so the
front-face convention remains consistent. An instanced renderer must apply the
equivalent determinant-parity cull/winding state. `det(A) == 0` is invalid.
Prototype-local persisted triangles/normals are not rewritten per occurrence.
World bounds transform all eight prototype-local AABB corners and re-bound in
float64; transforming only min/max endpoints is invalid for rotated or mirrored
occurrences.

### 4.4 Document lifetime

`CadDocument` exposes exact read-only properties:

```text
manifest: CadManifest
prototype_meshes: tuple[CadPrototypeMesh, ...]
tessellation: CadTessellationResult | None
closed: bool
source_available: bool
owner_thread_id: int | None
```

It may privately own a retained source store and live provider session. It is a
context manager and has idempotent `close()` and `release_source()` methods.
`prototype_meshes` is the empty tuple when `tessellation is None` and otherwise
is exactly `tessellation.prototype_meshes`; it is a convenience view, not a
second independently mutable value.
`close()` releases only the live provider session, readers, XDE document,
shape maps, native handles, and provider callbacks; the manifest, published
arrays, and retained source remain usable. `release_source()` deletes only the
task-owned immutable source spool, never the caller's file, and makes later
preserve or source-reopen operations unavailable. It does not invalidate the
manifest or published arrays.

The core snapshots every input regular file to an owned private spool before
provider parsing and computes SHA-256 while copying. The caller must exclude
source-file mutation for that copy interval; detected size/metadata drift fails
with `cad.source.changed`. The provider reads only the snapshot. The core keeps
it when `retain_source=True` and otherwise deletes it after the requested read
mode finishes. This is the byte authority used by preserve and later reopen.

Only `live` may retain an OCCT session. Mode behavior is exact:

- `manifest_only`: `tessellation_options` must be `None`; return no meshes and
  close the transient provider session before returning;
- `preview`: `None` selects default `CadTessellationOptions`; read and
  tessellate every unique prototype, publish meshes, then close the transient
  provider session before returning;
- `live`: retain the provider session; `None` produces no meshes, while an
  explicit tessellation record produces meshes before returning.

Later tessellation uses an open live session when present. Otherwise it may
reopen the immutable retained source through the exact borrow SPI below. If
neither exists, it raises `CadOperationError` with code
`cad.source.unavailable`. It returns a new `CadTessellationResult` and never
mutates the document's already published result or arrays. Translation follows
the same live-state or retained-source rule.

A live document refuses pickling. Closing a live session occurs on its owner
thread; wrong-thread close fails with `cad.session.wrong_thread` and leaves the
session for owner-thread cleanup. A finalizer may emit a leak warning and
delete a pure-Python source spool using a thread-safe callback, but it must
never call OCP or pretend to close a thread-affine native session.

The provider constructs documents through a protocol-versioned integration
SPI which is not re-exported from `anyfileio.__init__`:

```python
@classmethod
def CadDocument._from_backend(
    cls,
    *,
    manifest: CadManifest,
    tessellation_options: CadTessellationOptions | None = None,
    prototype_meshes: tuple[CadPrototypeMesh, ...] = (),
    source_snapshot: pathlib.Path | None = None,
    backend_state: object | None = None,
    close_backend_state: Callable[[object], None] | None = None,
    owner_thread_id: int | None = None,
) -> CadDocument: ...

@classmethod
def CadDocument._from_preview_artifact(
    cls,
    *,
    manifest: CadManifest,
    tessellation_options: CadTessellationOptions,
    prototype_mesh_loader: Callable[[], tuple[CadPrototypeMesh, ...]],
    source_snapshot: pathlib.Path | None = None,
) -> CadDocument: ...

def CadDocument._backend_state_for(self, backend_id: str) -> object | None: ...

def CadDocument._borrow_source_snapshot(self) -> ContextManager[pathlib.Path]: ...
```

The factory validates all public records/arrays, requires tessellation options
and a complete prototype-mesh tuple either both present or both absent, binds a
`CadTessellationResult` from the exact manifest through the core, requires state, close callback,
and owner thread either all present or all absent, and takes ownership only of
the core-created `source_snapshot`. `_backend_state_for` validates backend id,
closed state, and owner thread before returning the opaque object to that
provider; user code never receives it through a public property. The core
deletes an unretained snapshot after `read` returns and verifies that a retained
snapshot is exactly the path attached to the returned document. A provider
cannot attach or delete an arbitrary caller path.

`_from_preview_artifact` is the core integration SPI for its OCP-free artifact
codec. It validates the manifest and normalized options immediately and stores
a one-shot, thread-safe loader. The first `prototype_meshes` or `tessellation`
property access calls the loader, validates exactly one canonical mesh for
every manifest prototype, binds them with those options into one
`CadTessellationResult`, caches it, and discards the loader. Concurrent callers
share one invocation and one outcome. Failure is cached as `CadArtifactError`
and publishes no partial tuple/result. A document from this factory is closed
and has no backend state or owner thread.

The core helper
`_snapshot_source(path, *, expected_sha256: str | None = None) -> pathlib.Path`
copies a caller path to core-owned immutable storage while checking drift and
hash. Only its returned path may be transferred into either factory. An
artifact codec or provider never adopts the caller's path as owned storage.

`_borrow_source_snapshot()` is core-only/provider-SPI access to the immutable
task-owned spool. It fails after `release_source()`, yields a read-only path for
the entered interval, prevents source release/deletion until the last borrower
exits, and never transfers deletion authority. The provider may open/read but
must not write, rename, retain, or delete it. For a closed/session-free
document, `tessellate` and `translate` enter this context, perform a fresh
transient provider read in `live` mode against the same manifest identity,
validate document id/source hash/backend compatibility, execute the requested
operation, close the transient native session on its owner thread, then exit
the borrow. Tessellation returns a new `CadTessellationResult` and never mutates
the document's previously published result/arrays. They never replace the
caller's `CadDocument` or attach transient state to it. Any mismatch fails
closed as `cad.source.reopen_mismatch`.

### 4.5 Exact core and provider call shapes

Protocol 1 accepts local filesystem paths only. `PathLike` means
`str | os.PathLike[str]`; file-like objects, URLs, and remote references are a
future protocol change. `CancellationCheck` is
`Callable[[], bool] | None`; a true result requests cancellation.

The public core surface is:

```python
def read_cad(
    source: PathLike,
    *,
    options: CadReadOptions = CadReadOptions(),
    tessellation_options: CadTessellationOptions | None = None,
    cancellation: CancellationCheck = None,
    backend_id: str = "occt",
) -> CadDocument: ...

def tessellate_cad(
    document: CadDocument,
    *,
    options: CadTessellationOptions = CadTessellationOptions(),
    cancellation: CancellationCheck = None,
) -> CadTessellationResult: ...

def write_cad(
    document: CadDocument,
    destination: PathLike,
    *,
    options: CadWriteOptions,
    cancellation: CancellationCheck = None,
) -> CadAssetWriteReport: ...
```

Existing `anyfileio.read(path)` dispatches a known CAD suffix to `read_cad`
with default options. There is no implicit write default: callers choose
preserve or translate. Before snapshotting, the core defines `source_name` as
Unicode-NFC normalization of `pathlib.Path(source).name`; it must be non-empty
and contain no path separator. Only that basename, never an absolute path, is
stored in the manifest or cache key.

The entry-point target has exact no-argument shape:

```python
def get_backend() -> CadBackendProtocol: ...
```

The core caches the first returned object and validates these read-only
attributes before use:

```text
backend_id: Literal["occt"]
protocol_version: Literal[1]
backend_compatibility_version: Literal[1]
backend_version: str
capabilities: CadCapabilities
```

The exact provider methods are:

```python
def read(
    self,
    source_snapshot: pathlib.Path,
    *,
    source_sha256: str,
    source_name: str,
    options: CadReadOptions,
    tessellation_options: CadTessellationOptions | None,
    cancellation: CancellationCheck,
) -> CadDocument: ...

def tessellate(
    self,
    document: CadDocument,
    *,
    options: CadTessellationOptions,
    cancellation: CancellationCheck,
) -> tuple[CadPrototypeMesh, ...]: ...

def translate(
    self,
    document: CadDocument,
    destination_temporary: pathlib.Path,
    *,
    options: CadWriteOptions,
    cancellation: CancellationCheck,
) -> CadAssetWriteReport: ...
```

Provider `read` passes any eagerly produced mesh tuple plus its exact normalized
options to `_from_backend`; that core factory binds provenance. Provider
`tessellate` returns only the unlabelled tuple produced for the supplied
document call. `tessellate_cad` validates the tuple and core-binds it with the
supplied document manifest/options into the public `CadTessellationResult`.
This division prevents provider code or callers from asserting another
document's source/producer identity.

The core handles preserve itself and does not load the provider for that path.
Translation is the only provider write method; it rejects
`options.mode != "translate"`. The core creates a uniquely named temporary
file in the destination directory, passes only that path, validates the
returned report and output SHA-256, flushes the file, and atomically replaces
the final path. Failure or cancellation removes the temporary file and leaves
an existing destination untouched. Provider methods never rename/delete the
caller's source or final destination.

Provider callables use core records and errors only. They do not expose OCP
types. Structural ANYgeometry export has the separate exact adapter call in the
adapter contract and is deliberately absent from the NumPy-only protocol.

## 5. Source, cache, and export semantics

Exact retained source bytes are authoritative. Their SHA-256 is computed from
bytes, not filename or timestamp. Derived manifests, preview arrays, bounds,
selection tables, and optional backend-native data are disposable caches.

The document preview cache key contains:

```text
source_sha256
source_name
source_format
effective_source_length_unit
normalized_read_options (all CadReadOptions fields)
backend_id
backend_version
backend_compatibility_version
binding_distribution
binding_version
occt_version
source_identity
normalized_tessellation_options
preview_artifact_schema_name = "anyfileio.cad-preview"
preview_artifact_schema_version = 1
```

The first eleven fields through `occt_version` have the exact canonical digest
`CadTessellationResult.source_identity`; the cache key recomputes and checks
that digest rather than trusting a supplied label.

The full read record includes `mode`, `retain_source`, unit override, and
`heal`. Exact provider-distribution, binding-distribution/version, and runtime
OCCT producer values are also keyed, so the cache never aliases artifacts whose
observable `CadManifest` metadata differs. This cache identity is distinct from topological
`document_id`, which deliberately excludes filename/mode/retention. The key is
`cad-preview-key-v1:<hex>`, where `<hex>` is SHA-256 of canonical UTF-8 JSON of
the exact fields above using sorted keys, compact separators,
`ensure_ascii=False`, and `allow_nan=False`. A per-prototype cache additionally
includes its stable `prototype_identity`. An invalid, unknown, or mismatched
cache is never authoritative. It falls back to the retained source when that
source is available and otherwise raises `CadArtifactError` with code
`cad.preview.invalid_without_source`. Cached manifest/preview reopen must not
import OCP. Backend-native BREP caching is excluded until separately justified
by measured evidence.

### 5.1 Preview artifact schema 1

The derived preview artifact is a single ZIP file with MIME intent
`application/vnd.anyfileio.cad-preview+zip`. Every member uses `ZIP_STORED`;
filenames are UTF-8, relative, slash-separated, unique, and cannot contain an
empty, `.` or `..` component. Timestamps and platform permission fields are
normalized for deterministic output. Archive order is `manifest.json` first,
then every other member in ascending ASCII byte order (all schema-1 member
names are ASCII). Every `ZipInfo` has `date_time=(1980,1,1,0,0,0)`,
`compress_type=ZIP_STORED`, `create_system=3`, `create_version=20`,
`extract_version=20`, `flag_bits=0`, `internal_attr=0`,
`external_attr=(0o100600 << 16)`, empty `extra` and member `comment`; the
archive comment is empty. No data descriptor, encryption, ZIP64 field, or
extra field is emitted. ZIP64 is therefore rejected rather than selected in
schema 1; a member or archive exceeding ordinary ZIP limits requires a future
schema.

Required members are:

```text
manifest.json
occurrences/prototype_ids.npy
occurrences/parent_ids.npy
occurrences/local_transforms.npy
occurrences/accumulated_transforms.npy
occurrences/visibility.npy
prototypes/<decimal-id>/origin.npy
prototypes/<decimal-id>/positions.npy
prototypes/<decimal-id>/triangles.npy
prototypes/<decimal-id>/face_offsets.npy
prototypes/<decimal-id>/edge_offsets.npy
```

`normals.npy` and `edge_indices.npy` are present exactly when the corresponding
array is not `None`. All `.npy` members use NumPy format 2.0, contain one
non-object array with the dtype/shape/contiguity rules from section 4.3, and are
read with `allow_pickle=False`. Occurrence arrays have one row per canonical
manifest occurrence: ids use uint32 only when the maximum encoded prototype,
occurrence, and parent id fits uint32, otherwise uint64; a root parent is
encoded as zero; transforms are float64 `(n,4,4)`; visibility is bool. Count
alone never selects an id dtype because stable ids can be sparse or large.

`manifest.json` is canonical UTF-8 JSON with sorted keys, compact separators,
`ensure_ascii=False`, and `allow_nan=False`. Its top-level object has exactly
these keys and no others:

```text
schema, version, protocol_version, backend, cache_key,
document, meshes, entries
```

`schema`, `version`, and `protocol_version` are respectively
`"anyfileio.cad-preview"`, `1`, and `1`. `backend` has exactly
`id`, `version`, `compatibility_version`, `binding_distribution`,
`binding_version`, and `occt_version`, whose values equal the corresponding
manifest fields. `cache_key` has exactly `id`, `source_sha256`, `source_name`,
`source_format`, `effective_source_length_unit`, `read_options`,
`tessellation_options`, `backend_id`, `backend_version`,
`backend_compatibility_version`, `binding_distribution`, `binding_version`,
`occt_version`, `source_identity`, `artifact_schema`, and `artifact_version`;
`id` and every
component equal the section-5 cache key. `read_options` contains exactly `mode`, `retain_source`,
`source_length_unit_override`, and `heal`. `tessellation_options` contains
exactly `linear_deflection`, `angular_deflection`, `relative_deflection`,
`parallel`, `include_edges`, `generate_normals`, and `precision_policy`.

`document` is the canonical non-array projection of `CadManifest` and has exactly
`document_id`, `source_sha256`, `source_name`, `source_format`,
`source_length_unit`, `source_to_metre_scale`, `internal_length_unit`,
`root_occurrence_ids`, `prototypes`, `occurrences`, `shapes`,
`world_bounds_m`, `topology_counts`, `external_references`, `diagnostics`,
`normalized_read_options`, `backend_id`, `backend_version`,
`backend_compatibility_version`, `binding_distribution`, `binding_version`,
and `occt_version`. Prototype and shape records use exactly the field names
frozen in section 4.2. An occurrence projection deliberately excludes every
array-backed field and has exactly `id`, `cad_ref`, `array_row`,
`world_bounds_m`, and `name`. `array_row` is the zero-based row in all five
occurrence arrays; projections/rows are in ascending occurrence id.
`prototype_id`, `parent_id` (`0` means root), `local_transform`,
`accumulated_transform`, and `visible` come solely from those arrays, so no
duplicated JSON value can disagree. `root_occurrence_ids` must equal exactly
the ids whose parent row is zero. On open, the core validates ids/foreign keys,
the affine/accumulation rules, visibility, and root equality before constructing
`CadOccurrenceRecord`. A `Bounds` is a six-number JSON array or null. Topology counts are
an object containing exactly the six canonical kind keys. A
`CadEntityRef` is exactly
`{"document_id":str,"kind":str,"local_id":int}`. A `CadDiagnostic` has
exactly `code`, `severity`, `message`, `entities`, and `details`. Tuples become
JSON arrays and immutable string mappings become JSON objects.

`meshes` is an array in ascending `prototype_id`. Every item has exactly
`prototype_id`, `local_bounds_m`, `precision`, `diagnostics`, `face_owners`,
`edge_owners`, and `arrays`. `arrays` has exactly `origin`, `positions`,
`triangles`, `normals`, `face_offsets`, `edge_indices`, and `edge_offsets`;
each value is its exact ZIP member name, while `normals` and `edge_indices` are
JSON null exactly when those members are absent. `entries` maps every
non-manifest member name, and no other name, to the lower-case SHA-256 of its
complete stored member bytes. The full normalized read options in `document`
must equal `cache_key.read_options`; no identity-only subset is serialized in
their place.

Every required JSON key is present; optional values use JSON null and are never
omitted. Unknown keys, unknown enum values, duplicate logical ids, and any
inconsistency between repeated backend/cache/document values are rejected in
schema 1 rather than ignored.

Large numerical arrays are never JSON. The ZIP file's own SHA-256 is retained
by its owning cache/project index and is not embedded recursively. A reader
rejects an unknown schema/version, extra or missing member, duplicate member,
unsafe name, mismatched hash, object array, wrong dtype/shape, record/array
count mismatch, owner/offset violation, source/options/backend mismatch, or
non-canonical identity. Schema 1 has no implicit migration; rebuild from source
or fail when source is unavailable.

The writer creates a temporary sibling, writes and closes it, reopens and
validates hashes/invariants without OCP, and atomically replaces the target.
Failure preserves the first diagnostic and removes only its temporary. The
artifact does not contain retained source bytes or OCP/native shapes. Metadata
open first reads `manifest.json`, then eagerly reads and hash-validates only the
five compact occurrence arrays needed to reconstruct the complete
`CadManifest`; it passes that manifest to `_from_preview_artifact`. Prototype
geometry/owner/offset arrays remain lazy and are loaded on first tessellation
access, then published read-only. This schema is separate from ANYfem project-format-7
storage even when ANYfem embeds equivalent manifest/array data.

The schema owner is the NumPy-only core module `anyfileio.cad_artifact`, whose
exact public calls are:

```python
def write_preview_artifact(
    document: CadDocument,
    destination: str | os.PathLike[str],
    *,
    tessellation: CadTessellationResult | None = None,
    cancellation: CancellationCheck = None,
) -> str:  # lower-case artifact SHA-256
    ...

def open_preview_artifact(
    artifact: str | os.PathLike[str],
    *,
    retained_source: str | os.PathLike[str] | None = None,
) -> CadDocument: ...
```

Writing uses the explicit `tessellation` when supplied and otherwise requires
`document.tessellation`; absence fails with `cad.preview.tessellation_required`.
It validates the result's exact normalized options, one mesh per document
prototype, ids, owners, bounds, and diagnostics, recomputes the manifest's
`source_identity`, and requires exact equality before deriving the cache key.
This permits a detached result from `tessellate_cad` to be cached without
mutating the document and prevents callers from supplying an unlabelled tuple
or relabelling another source/reader/producer result. It then uses the atomic rules above.
The writer creates every `.npy` member with
`numpy.lib.format.write_array(..., version=(2, 0), allow_pickle=False)` after
normalization and hashes the resulting complete member bytes. Opening imports
neither OCP nor the provider backend module. It
constructs a closed, session-free `CadDocument` through the core integration
factory, initially with lazily loaded prototype arrays. If `retained_source` is
given, the module snapshots it to task-owned storage, hashes it, and attaches it
only when the hash equals the manifest source hash; mismatch fails. Without it,
`source_available=False`, so viewing works but preserve/re-tessellate/translate
from source does not. Input paths are never adopted or deleted.

The codec ships in the lightweight `ANYfileio` wheel and is the callable path
used by ANYfem/project offline preview. `ANYfileio-occt` may produce documents
and call the core writer but does not own, duplicate, or need to be installed
for the reader. ANYfem project format 7 remains its own persistence schema; it
may embed equivalent records/arrays only through its separately frozen V7
contract.

### 5.2 Imported-asset writes

Preserve mode is available only when both the originating normalized read and
write have `heal=False`. It copies unchanged retained bytes to the same format,
verifies the output SHA-256, does not import OCP, and reports
`byte_identical=True` only when the hashes and bytes agree. Because no healing
or rewrite preceded the report, its source/output topology counts are the same
manifest counts; a healed-read document is rejected rather than making a claim
about topology in its raw retained bytes. Translation invokes the heavy writer, records all
reader/writer diagnostics, unit conversions, topology-count changes,
approximations, and metadata losses, and always reports
`byte_identical=False`.

`CadAssetWriteReport` is the frozen, slots-based core imported-asset result:

```text
source_document_id: str
mode: "preserve" | "translate"
source_format: "step" | "iges" | "brep"
target_format: "step" | "iges" | "brep"
source_length_unit: LengthUnit | "unknown"
target_length_unit: LengthUnit | "unknown"
backend_id: Literal["occt"]
backend_version: str
backend_compatibility_version: Literal[1]
binding_version: str | None
occt_version: str | None
output_sha256: 64 lower-case hex
byte_identical: bool
source_topology_counts: immutable Mapping[str, int]
output_topology_counts: immutable Mapping[str, int]
healing_applied: bool
geometry_changed: bool
exported_entities: tuple[CadEntityRef, ...]
unsupported_entities: tuple[CadEntityRef, ...]
approximations: tuple[str, ...]
metadata_losses: tuple[str, ...]
diagnostics: tuple[CadDiagnostic, ...]
execution_mode: "preserve_copy" | "provider_translation"
```

Strings and entity tuples are canonicalized deterministically; topology counts
use the exact keys in section 4.2. Preserve reports equal source/output counts,
`healing_applied=False`, `geometry_changed=False`, no approximation/loss, and
`execution_mode="preserve_copy"`. Translation reports observed output counts,
healing and change truthfully and uses `provider_translation`. Binding/OCCT
versions are `None` only for provider-free preserve. Its entities are
`CadEntityRef` values and it has no ANYgeometry mapping. The adapter's
`CadWriteReport` is a different type and namespace.

## 6. Concurrency, global state, and cancellation

Published core records and arrays are immutable and safe for concurrent reads.
A live provider session is thread-affine unless its provider explicitly
documents stronger guarantees; it is never shared across processes after a
fork. Tk calls never occur in provider workers.

Process-global OCCT settings are protected by a backend-owned re-entrant lock
and a context manager that captures and restores previous values even on
failure. The critical section is limited to operations that actually depend on
global state. NumPy normalization, hashing, byte copying, artifact I/O, and
other non-OCCT work are not serialized by that lock.

The core/provider calls `CancellationCheck` before starting and between source
copy/read, assembly and prototype traversal, each unique-prototype
tessellation, bounded array-extraction chunks, and writer stages. A true result
raises `CadOperationCancelled` with code `cad.operation.cancelled`. An exception
raised by the callback becomes `CadOperationError` with code
`cad.cancellation_check.failed` and retains the cause. A running native OCCT
call is not falsely claimed interruptible. Cancellation leaves no successful
artifact/report, closes newly owned resources on their proper thread, removes
only task-owned temporaries, and preserves the pre-existing destination.

## 7. Future solid-to-shell boundary

This protocol imports CAD, previews it, preserves or translates imported CAD,
and exports already-structural ANYgeometry through the separate adapter. It
does not define solid-to-shell conversion, midsurface extraction, thickness or
beam recognition, automatic healing, general booleans, or hidden conversion
features. A future conversion starts from retained source plus
`CadEntityRef` and returns separately owned ANYgeometry entities and evidence.
It requires a new reviewed contract and must not be smuggled into protocol 1.

## 8. Qualification boundary

Source review and focused contract tests do not establish built-wheel,
platform, resolver, startup, memory, or throughput claims. Package builds,
clean installs, full or large suites, OCP execution, timing/RSS/copy counts,
profiling, and benchmarks are separately lease-gated. See
[`../DEPENDENCY_MATRIX.md`](../DEPENDENCY_MATRIX.md) for the frozen metadata
matrix and the explicit `UNRUN` cells.
