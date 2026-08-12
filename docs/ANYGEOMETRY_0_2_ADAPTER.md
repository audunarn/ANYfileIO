# ANYgeometry 0.2 structural CAD adapter contract

Status: frozen read-only adapter contract for ANYgeometry 0.2.1, document
schema 4, and ANYfileio-occt 0.1.

The public ANYgeometry authority is commit
`37234b7bc6b6c3f2e02cf1c53acb875245d9c3aa`, with qualified code parent
`8828019e0f940b0d6f240b98f8be17d6f306155b`. The source plan SHA-256 is
`473523BD3BD28FC88487A961C29BF7B640592F415B981236C558FA963AF1E414`;
the registered baseline addendum SHA-256 is
`9249191E78C746A81A2B7D80B8ADA543AD45FCAE9CA41F5CAB04E169D68796A1`.

This adapter consumes ANYgeometry; it never edits it. A missing public
capability becomes a geometry-owner proposal, not a private-store workaround.

## 1. Dependency and import boundary

Structural export is implemented only in the lazy module
`anyfileio_occt.geometry_export`. It is installed by the heavy distribution's
`geometry` extra:

```text
ANYfileio-occt[geometry]>=0.1,<0.2
    -> ANYgeometry>=0.2.1,<0.3
```

Neither `anyfileio`, `anyfileio_occt`, provider discovery, CAD import,
tessellation, cached preview reopen, preserve, nor imported-CAD translation
imports ANYgeometry. Structural export imports it only when that operation is
requested. If the extra is absent, the operation fails with a structured
missing-extra diagnostic and all non-geometry operations remain usable.

The lightweight core owns `CadEntityRef` and `CadAssetWriteReport`. The lazy
module `anyfileio_occt.geometry_export` owns and exports
`GeometryReadLease`, `GeometryExportOptions`, `GeometryExportView`,
`GeometryExportDiagnostic`, `GeometryExportBlocked`, `CadWriteReport`, and
`export_geometry`.
The adapter uses the real `anygeometry.EntityHandle`; the core does not define
a surrogate, string form, copied kind catalogue, or optional lazy identity
facsimile.

## 2. Accepted public API

The adapter may consume only root-exported public classes/functions and public
`GeometryModel` properties/methods. The accepted public state is:

```text
model_id, revision, tolerance, units, local_origin,
coordinate_transform, crs_metadata

vertices, edges, faces
parts, sheets, face_uses, coedges
members, member_edge_uses, attachments, junctions
construction_vertices
groups, tags

handle(), resolve_handle(), last_change_set
add_change_hook(), remove_change_hook()
strict_audit(), audit_changed_region()
to_dict(), from_dict(), read_geometry(), write_geometry()
```

Entity and structural stores are live read-only mappings over immutable
records. Coordinate arrays, surface/curve arrays, and `local_origin` /
`coordinate_transform` are published read-only. `FrozenMetadata` is deeply
immutable. Private attributes, mutation helpers, allocator state, internal
indexes, and private serializers are prohibited.

`EntityHandle(model_id, kind, id)` is the cross-package identity. Its model id
is a non-nil canonical UUID, kind is from ANYgeometry's public catalogue, and
id is positive. A handle from another model must never resolve by local id.
The adapter uses `GeometryModel.handle()` or the public constructor and retains
the model UUID in every report mapping.

## 3. Shallow deterministic export view

`GeometryExportView` is an adapter-local frozen, slots-based record. It contains
the following values captured for one revision:

```text
model_id: UUID
revision: int
units: str
local_origin: read-only float64 array, shape (3,)
coordinate_transform: None or read-only float64 array, shape (4, 4)
crs_metadata: immutable metadata
tolerance: TolerancePolicy

vertices: tuple[Vertex, ...]
edges: tuple[Edge, ...]
faces: tuple[Face, ...]
parts: tuple[Part, ...]
sheets: tuple[Sheet, ...]
face_uses: tuple[FaceUse, ...]
coedges: tuple[Coedge, ...]
members: tuple[Member, ...]
member_edge_uses: tuple[MemberEdgeUse, ...]
attachments: tuple[Attachment, ...]
junctions: tuple[Junction, ...]
construction_vertices: tuple[tuple[int, int | None], ...]
groups: tuple[tuple[str, tuple[EntityRef, ...]], ...]
tags: tuple[tuple[EntityRef, tuple[str, ...]], ...]
```

ID-bearing record tuples are ascending by public integer id. Construction
ownership is ascending by vertex id. Groups are ascending by group name and
retain each `EntityRef` sorted by `(ref.kind, ref.id)`; tags are ascending by
that same public key and retain sorted unique tag strings. `EntityRef` itself is
not assumed orderable. Thus group names and tag keys are never discarded. The
tuples copy references to immutable records, not
entity internals or geometry arrays. Only the small coordinate-setting arrays
are copied once and marked read-only. The normal export path does not call
`clone`, `extract_model_closure`, a topology snapshot, `to_dict`, or any other
full-model serialization.

Capture requires an owner-provided read lease. The adapter-owned structural
protocol is:

```python
class GeometryReadLease(Protocol):
    @property
    def geometry(self) -> anygeometry.GeometryModel: ...

    @property
    def model_id(self) -> UUID: ...

    @property
    def revision(self) -> int: ...
```

The `read_lease` argument is a one-shot
`ContextManager[GeometryReadLease]`; its entered token is immutable for the
interval and may not be reused. This is an adapter protocol implemented by the
ANYfem/application document owner, not a symbol or capability claimed in
ANYgeometry 0.2.1. The context manager is created for the exact `geometry`
argument; after entry, `token.geometry is geometry` is required. Equality of
UUID/revision on a different loaded `GeometryModel` is insufficient. The
context manager's `__enter__` must establish that this exact object is committed
and transaction idle. For the full entered interval it
excludes topology, structural,
feature-history, document-setting, restore/undo, and deserialization mutation.
The adapter checks the live model id/revision against the lease at entry and
again before exit. A future immutable atomic committed snapshot bound to the
same id/revision may substitute after a reviewed contract revision.

Within that one lease interval the adapter performs, in order:

1. validate `lease.geometry is geometry`, lease identity/revision, and read
   `r0`;
2. capture settings, deterministic public records, groups, and tags;
3. run or validate the requested full audit when `certified`;
4. create the optional canonical geometry-document checksum evidence;
5. read `r1` and revalidate exact object identity plus lease identity/revision;
6. accept only when every id/revision value equals the captured values.

The lease is then released before any OCP conversion or destination I/O.
Revision equality without the lease is only stale-change detection: live
mapping-backed stores may expose provisional transaction state before revision
advance. A double revision read, retry, caller assertion, same-thread access,
or absence of an observed writer is not synchronization. If no conforming
lease exists, fail with typed code `geometry.read_lease_required`; mismatch or
a broken lease fails with `geometry.model_changed`. The adapter never falls
back to an unguarded capture.

ANYgeometry 0.2.1 supplies no public lock or snapshot implementing this
protocol. Structural export implementation remains owner-blocked until the
ANYfem/application owner returns an accepted lease implementation and focused
proof that it excludes all named mutation/restore paths. This requirement is
the Boss-approved fail-closed supersession of the source plan's unsafe
double-revision fallback; it does not authorize an ANYgeometry edit.

The export converts the accepted view even if the live model later advances.
At completion it may read the live revision only to report
`live_revision_at_completion` and a warning; it does not switch revisions or
mix records.

## 4. Revision, change observation, and caches

`GeometryExportOptions` is a frozen, slots-based adapter record:

```text
target_format: "step" | "iges" | "brep"
coordinate_space: "model_local" | "external" = "model_local"
output_length_unit: LengthUnit | None = None
validation_mode: "committed" | "certified" = "committed"
audit_policy: AuditPolicy | None = None
    include_unowned_edges: bool = False
    include_unowned_vertices: bool = False
unsupported_policy: "fail_atomic" | "skip_unsupported" = "fail_atomic"
include_geometry_document_checksum: bool = False
```

The canonical unit vocabulary and `CancellationCheck` are those in
[`CAD_BACKEND_CONTRACT.md`](CAD_BACKEND_CONTRACT.md). `output_length_unit=None`
means the normalized `geometry.units`; an unknown token fails rather than being
guessed. `audit_policy=None` means `AuditPolicy.strict()` when certified and is
ignored when committed. The default unsupported policy creates no output if
any requested exact entity is unsupported. `skip_unsupported` emits the
supported dependency-closed subset only after recording every omitted real
handle and its reason. An unsupported face, support surface, trim, loop edge,
or coedge omits the complete owning FaceUse and Sheet; the owning Part remains
only if another complete Sheet or Member survives. An unsupported Member axis
edge/use omits the complete Member. A relationship whose source or target is
omitted is itself omitted and reported. Shared dependencies are retained only
when every surviving owner can reference them consistently. No incomplete
wire, face, Sheet, Member, Part child list, Attachment, or Junction is emitted.

The exact adapter entry point is:

```python
def export_geometry(
    geometry: anygeometry.GeometryModel,
    destination: str | os.PathLike[str],
    *,
    options: GeometryExportOptions,
    read_lease: ContextManager[GeometryReadLease],
    cancellation: CancellationCheck = None,
) -> CadWriteReport: ...
```

It validates/captures under the lease from section 3, releases the lease, then
performs conversion. It writes a uniquely named temporary sibling and replaces
the destination atomically only after successful close, report validation, and
SHA-256 verification. Failure/cancellation removes only its temporary and
preserves an existing destination. It never accepts `read_lease=None`.
The destination suffix must be one of the core-known suffixes for the explicit
`options.target_format`; absence, an unknown suffix, or disagreement fails
before conversion with `cad.write.format_suffix_mismatch`. The uniquely named
temporary sibling retains the same final CAD suffix after its temporary stem.

Generated `CadEntityRef.document_id` has form
`cad-geometry-v1:<64 lower-case SHA-256 hex>`. The hash is over canonical UTF-8
JSON (`sort_keys=True`, compact separators, `ensure_ascii=False`,
`allow_nan=False`) containing exactly:

```text
identity_kind = "geometry"
identity_version = 1
source_model_id
source_revision
target_format
coordinate_space
effective_output_length_unit
include_unowned_edges
include_unowned_vertices
unsupported_policy
backend_id
backend_compatibility_version
```

`source_model_id` is encoded as the canonical lower-case hyphenated 36-character
UUID string returned by `str(model_id)`, never as a Python `UUID` object, bytes,
hex-without-hyphens, or implementation-specific JSON extension. The prepared
cache key below uses that same scalar representation.

Validation mode/policy, checksum evidence, destination, execution scheduling,
and live completion revision do not change generated topology identity and are
excluded. Local CAD ids are allocated by deterministic Part/Sheet/FaceUse/
Member and unowned-geometry rules frozen in `CAD_BACKEND_CONTRACT.md`, never
Python hash order.

The default prepared-export cache is scoped to the exact live Python
`GeometryModel` object protected by the lease. Within that object scope its key
is:

```text
source_model_id (canonical lower-case hyphenated UUID string)
revision
normalized structural-export options
coordinate_space
target format and output unit
validation mode and audit-policy identity
backend id and backend compatibility version
```

Audit-policy identity is SHA-256 of canonical UTF-8 JSON of the complete
`AuditPolicy.to_dict()` payload (sorted keys, compact separators,
`ensure_ascii=False`, `allow_nan=False`), never merely `policy.name`.

Object scope is enforced by retaining and checking the object itself (for
example, an owner-held per-document cache or a weak-key entry); `id(geometry)`
alone is insufficient because ids can be reused. The object token is
process-local, is never serialized, and never permits reuse for another loaded
model with the same UUID/revision. Optional checksum evidence does not widen
reuse under protocol 1. Certified-audit caches follow the same exact-object
scope in addition to revision and canonical policy identity.

Revision-keyed entries require no speculative incremental correctness. A
successful outer transaction increments the revision once for semantic change
and publishes an immutable `ChangeSet`. Document-settings and feature-history
updates publish their own flagged change sets. Hooks observe committed changes
only, are read-only, and may not mutate the model.

An optional in-session incremental cache may reuse work across revisions only
when it has an unbroken `ChangeSet` chain. It must apply at least these
conservative invalidations:

- added, removed, or modified vertices invalidate incident edges and faces;
- changed edges invalidate incident face uses, sheets, and member wires;
- changed faces invalidate their face uses and sheets;
- `ownership_changes` dispatch by `(kind, id)`: Part, Sheet, FaceUse, and
  Coedge changes invalidate affected assembly/sheet shapes; `('junction', id)`
  invalidates junction relationship metadata/reports; construction-vertex
  ownership reported as `('vertex', id)` invalidates affected ownership and
  assembly metadata;
- `member_changes` invalidate affected Member wires and owning Parts;
- `attachment_changes` invalidate relationship metadata and reports;
- group, tag, or feature-history changes invalidate corresponding metadata;
- `document_settings_changed` invalidates all coordinate, tolerance, and
  output-unit work;
- a gap, unknown key, failed resolution, or uncertain dependency invalidates
  the complete prepared plan.

Audit reports never migrate to another revision. OCP shapes are at most
session-local cache entries and are never stored in ANYfem project files.

## 5. Validation modes

Every export chooses one explicit validation mode.

### 5.1 `committed`

`committed` is the default. It consumes a stable committed revision and checks
that every referenced public record exists, ownership links and ordered loops
are consistent, required exact mappings are supported, coordinates are finite,
and requested output capabilities exist. It relies on ANYgeometry's atomic
commit invariants but does not claim a global geometric certification and does
not run `strict_audit()` merely to write ordinary CAD.

### 5.2 `certified`

`certified` calls the public full-model `strict_audit()` with an explicit
`AuditPolicy`, and proceeds only if the report is complete, verified, clean,
certifiable, and bound to the captured model UUID and revision. A
changed-region audit is useful invalidation evidence but can never certify the
whole model. Checker failure, an unsupported or unclassified candidate, a
revision mismatch, or a non-certifiable report blocks output.

A certified audit may be cached only within the exact-object scope above and by
model UUID, revision, and the canonical policy identity. `AuditReport.checksum` identifies that ephemeral report. It is
not the schema-4 document checksum and is never persisted as a geometry
certificate.

## 6. Structural ownership and ordering

The adapter follows the public ownership graph exactly:

```text
Part -> Sheet -> ordered FaceUse loops -> Coedge -> geometry Edge
Part -> Member -> ordered MemberEdgeUse -> geometry Edge
Attachment/Junction -> declared qualified relationships
```

### 6.1 Parts and sheets

A `Part` becomes an XDE product/component boundary or a named compound. Its
name and representable immutable metadata are retained. Coincident geometry in
separate Parts is not welded, and shapes are never sewn across a Part boundary.

A `Sheet` becomes an open shell when its public topology policy and supported
faces permit, otherwise a named surface compound. The adapter respects
the `SheetTopologyPolicy` boundary, non-manifold, and connectivity fields plus
`declared_non_manifold_edges`. Orientation comes only from the owned
FaceUse/Coedge records and underlying face/edge traversal; there is no Sheet
orientation-policy field. The adapter does not invent thickness, cap an open
boundary, or fabricate a solid.

### 6.2 Face uses and coedges

`FaceUse.orientation` is combined with each persistent `Coedge.orientation`.
Loop order is authoritative: the first loop is the outer loop and subsequent
loops are holes. The adapter does not reconstruct loops from proximity or
unordered edge incidence. Supported exact trims remain exact. Orientation
reversal changes CAD face/wire orientation, not source topology.

### 6.3 Members

One `Member` becomes one persistent wire or curve chain. Its
`Member.edge_use_ids` order is authoritative; each referenced
`MemberEdgeUse.edge_id`, `parent_range`, and `orientation` defines that ordered
segment. Member name and representable immutable metadata are retained.
`Member.orientation_reference`, when present, is encoded as named metadata or
an in-file named CAD property using its public `(vertex|edge|face, id)` key; if
the target format cannot retain it, the real Member handle and the loss are
reported.
The adapter never infers a structural member from a raw or coincident geometry
edge, and never creates a beam cross-section solid.

### 6.4 Attachments and junctions

Attachments and junctions retain declared connection intent, qualification
evidence, participating ranges, provenance, and structural context in named
document-level in-file CAD properties when the target format supports them;
they are not attached to an arbitrarily chosen participant. FaceUse/Coedge
evidence is placed on the owning Sheet object and MemberEdgeUse evidence on the
owning Member object. Unsupported
metadata is listed as loss in the write report. Geometry is not moved, healed,
welded, or imprinted merely to make a declared relationship appear topological.

`construction_vertices` is modelling/ownership intent, not an automatic
omission rule. A construction vertex that participates in exported topology is
exported normally; its construction flag and optional owning Part are encoded
as an in-file named CAD property when supported and otherwise reported as a
metadata loss on the real vertex handle. A standalone construction vertex is
treated by the same explicit unowned-geometry policy as another unowned vertex;
no implementation silently drops it merely because it is marked construction.

Protocol 1 creates exactly one CAD destination file and no auxiliary sidecar.
All retained adapter metadata is inside that file. If the selected writer
cannot carry a named property, the report records the loss instead of creating
another path, weakening atomic replacement, or returning an unreported output.

### 6.5 Unowned geometry

Faces without Sheet ownership are exported beneath `UnownedGeometry` as loose
faces when their exact geometry is supported. Unowned edges are omitted by
default and require `include_unowned_edges=True`. Their required endpoint
vertices are then included regardless of the standalone-vertex option. A
vertex required by any surviving face, edge, or Member is always included.
Another standalone unowned vertex is omitted by default and produces warning
diagnostic `geometry.unowned_vertex_omitted` bound to its real handle; this
explicitly unrequested omission does not enter `unsupported_entities` or
trigger `fail_atomic`. With `include_unowned_vertices=True`, it is emitted as a
loose CAD vertex beneath `UnownedGeometry` when the target supports an exact
vertex; otherwise it follows `unsupported_policy` and is recorded in
`unsupported_entities`. Construction status never changes these rules.
Unowned edges/vertices remain unowned geometry and are never labelled or mapped
as Members.

## 7. Exact geometry boundary

The protocol-1 structural adapter supports these exact mappings when their
trim/topology is representable by the selected target writer:

| ANYgeometry public type | CAD representation |
| --- | --- |
| `Vertex` | OCCT vertex |
| `Straight` | exact line edge |
| `Arc` | exact circular arc edge |
| supported `Spline` | exact Bezier/B-spline edge |
| `Plane` | exact planar surface |
| `Cylinder` | exact cylindrical surface |
| `Cone` | exact conical surface |
| `RuledSurface` | exact ruled surface when representable |
| supported trimmed `Face` | trimmed CAD face using authoritative loops |

An unsupported spline, Coons/custom surface, parameterization, degenerate
trim, or writer capability produces `geometry.unsupported_exact_entity` bound
to the real source handles. Default behavior is refusal for that entity or the
whole requested atomic export according to the explicit option. It never
silently facets, thickens, caps, approximates, heals, or changes topology.

## 8. Tolerance and units

`TolerancePolicy` belongs to the model. Its dimensional absolute fields are in
`geometry.units`; relative contributions use local feature extent and never
global coordinate magnitude. The adapter consumes all normalized public
fields:

```text
length, merge_length, coincidence, healing, angular, parameter, area,
surface_residual, curve_fit_residual, aabb_padding,
relative_length, relative_area
```

For a model-unit to output-unit length scale `s`, dimensional length/residual
fields scale by `s`, area by `s**2`, and angular, parameter, and relative fields
do not scale. This is equivalent to the public `TolerancePolicy.scaled(s)`.
The adapter records the source policy, scale, effective tolerance used, and any
writer-imposed stricter linear limit in the exact report fields below. An arbitrary OCCT default never replaces model
policy. `coincidence` does not authorize mutation; healing remains off unless
an explicit future contract enables it, and this adapter does not enable it.

Unit conversion is applied exactly once. If
`metres_per_model_unit / metres_per_output_unit == s`, raw model coordinates
are multiplied by `s` on output. ANYfem display units are not an additional
geometry transform.

ANYgeometry 0.2.1 accepts any non-empty unit string and supplies no converter.
The adapter therefore normalizes it only through the exact protocol-1 alias
table in `CAD_BACKEND_CONTRACT.md`. An unknown or ambiguous model unit fails
with `geometry.unit.unsupported`; bounds, tolerances, or UI settings are never
used to infer a scale. `output_length_unit=None` means that canonicalized model
unit. The report retains both the original model string and canonical source/
output tokens.

## 9. Coordinate spaces and the transform gate

`model_local` is the only emitted coordinate space in protocol 1. The option
parser recognizes `external` only to return its stable blocked diagnostic; it
never aliases it to `model_local`.

### 9.1 `model_local`

This is the default and is fully defined for ANYgeometry 0.2.1:

- read raw model geometry;
- interpret it in `geometry.units`;
- do not apply `local_origin`, `coordinate_transform`, or CRS metadata;
- convert from model unit to requested CAD unit exactly once.

### 9.2 `external`

ANYgeometry 0.2.1 validates, revisions, copies, and serializes
`local_origin` and `coordinate_transform` independently, but its public API and
schema 4 do not define a composition formula or application helper. The
separate geometry-edit operation `anygeometry.transform` uses ordinary affine
column-vector math, but it does not define the semantics of these document
metadata fields.

Accordingly, protocol 1 must not guess whether an external point is
`T @ (p + origin)`, `T @ (p - origin)`, `T @ p + origin`, or whether the origin
is already included in `T`. Every `external` or CRS-coordinate request fails
before creating output with typed state `BLOCKED` and code
`geometry.external_transform_undefined`, including a model whose local origin
is zero, transform is absent, or CRS metadata is empty. Identity-like metadata
does not authorize an inferred directional composition.

External export remains unavailable until the ANYgeometry owner publishes a
directional composition formula or helper plus forward/inverse translation,
rotation, nonzero-origin, CRS, and non-default-unit regressions. Adopting that
owner contract requires a reviewed revision of this document; two adapters may
not choose their own order.

For reference, the live model setter accepts a finite, invertible 4-by-4 matrix
whose final row passes its public affine validation; schema-4 serialization
uses the stricter tolerance `rtol=0, atol=1e-14`. The adapter applies that
stricter check during capture and otherwise blocks malformed metadata. These
shape checks still do not supply composition semantics.

## 10. Serialization and checksums

ANYgeometry's public serializer owns geometry document integrity:

- schema name `anygeometry`, version `4`;
- `to_dict()` writes schema 4; `from_dict()` reads schemas 1 through 4;
- current payloads contain model UUID/revision, coordinates/CRS, complete
  tolerance fields, geometry/topology/structural state, allocator high-water
  marks, lineage, groups/tags, extensions, and optional feature history;
- the document checksum is lower-case SHA-256 over canonical UTF-8 JSON of all
  document fields except `checksum`, with sorted keys, compact separators,
  `ensure_ascii=False`, and `allow_nan=False`;
- the checksum record is `{algorithm: "sha256", value: <64 hex>}`.

The normal export hot path does not serialize the model. If
`include_geometry_document_checksum=True`, the adapter calls the complete
public `anygeometry.to_dict()` while still inside the same owner read lease as
capture and verifies the returned model id and revision before accepting its
checksum. `to_dict()` cannot serialize a historical `GeometryExportView`; it
must never be called after releasing the lease and described as that view's
checksum. The adapter never reinterprets or patches schema fields and never computes
a checksum for a partial view.

This evidence is named `canonical_geometry_document_checksum`: it identifies a
fresh canonical schema-4 representation of the current committed model under
the read lease. It does not prove provenance or byte identity of an originally
loaded geometry file. This explicit mode may cost a full serialization and is
reported separately; the field is otherwise `None`.

Ordinary and certified schema-4 payload shapes are identical. Certification is
ephemeral. The report therefore distinguishes
`canonical_geometry_document_checksum`, `audit_checksum`, and CAD
`output_sha256`; none substitutes for another.

## 11. Adapter reports and identity mapping

`GeometryExportDiagnostic` is a frozen adapter record:

```text
code: str
state: "INFO" | "WARNING" | "ERROR" | "BLOCKED"
severity: "info" | "warning" | "error" | "fatal"
message: str
entities: tuple[anygeometry.EntityHandle, ...]
details: immutable Mapping[str, JSON scalar or tuple]
```

`GeometryExportBlocked(CadOperationError)` preserves the core error contract:
its inherited `diagnostic` is an `anyfileio.CadDiagnostic` with the same stable
code, no geometry entities, and JSON details containing the canonical model-id
string, revision, and requested coordinate space. It additionally exposes
`geometry_diagnostic: GeometryExportDiagnostic`, whose state is `BLOCKED` and
whose real `EntityHandle` tuple may identify affected geometry. It never
overrides the inherited field with the incompatible adapter record. External/
CRS requests use that exception and never return a success report or replace
the destination.

`CadWriteReport` is a frozen, slots-based adapter record with exact fields:

```text
source_model_id: UUID
source_revision: non-negative int
live_revision_at_completion: non-negative int
cad_document_id: str
target_format: "step" | "iges" | "brep"
coordinate_space: Literal["model_local"]
source_unit_original: str
source_unit: LengthUnit
output_unit: LengthUnit
validation_mode: "committed" | "certified"
source_tolerance_policy: TolerancePolicy
model_to_output_length_scale: positive finite float
effective_output_tolerance_policy: TolerancePolicy
writer_linear_tolerance_limit: positive finite float | None
audit_checksum: 64 lower-case hex | None
canonical_geometry_document_checksum: 64 lower-case hex | None
exported_parts: tuple[EntityHandle, ...]
exported_sheets: tuple[EntityHandle, ...]
exported_members: tuple[EntityHandle, ...]
exported_unowned_geometry: tuple[EntityHandle, ...]
unsupported_entities: tuple[EntityHandle, ...]
approximations: tuple[str, ...]
metadata_losses: tuple[str, ...]
geometry_to_cad: immutable Mapping[EntityHandle, CadEntityRef]
cad_to_geometry: immutable Mapping[CadEntityRef, tuple[EntityHandle, ...]]
backend_id: Literal["occt"]
backend_version: str
backend_compatibility_version: Literal[1]
binding_version: str
occt_version: str
output_sha256: 64 lower-case hex
execution_mode: Literal["geometry_exact"]
diagnostics: tuple[GeometryExportDiagnostic, ...]
```

The mapping types are exact:

```python
CadWriteReport.geometry_to_cad:
    Mapping[anygeometry.EntityHandle, anyfileio.CadEntityRef]
CadWriteReport.cad_to_geometry:
    Mapping[anyfileio.CadEntityRef, tuple[anygeometry.EntityHandle, ...]]
GeometryExportDiagnostic.entities:
    tuple[anygeometry.EntityHandle, ...]
```

Every exported/unsupported tuple is sorted by `EntityHandle.sort_key`; strings
and diagnostics use deterministic canonical ordering. Mappings are immutable,
deterministic, and include only successfully created entities. Reverse mapping
tuples are sorted real handles. Only primary Part, Sheet, Member, Face, Edge,
and Vertex handles enter the mappings. FaceUse, Coedge, MemberEdgeUse,
Attachment, and Junction handles never enter either mapping; representable
relationship evidence uses the deterministic carriers in section 6 and every
loss/diagnostic identifies the real relationship and participant handles.
Refused entities appear in diagnostics and
`unsupported_entities`. Attachment/junction evidence, member orientation
references, and metadata losses are reported even when the target format cannot
encode them. A success output SHA is always the exact lower-case SHA-256 of the
final bytes.

## 12. Prohibited behavior and qualification

The adapter does not mutate ANYgeometry, access private stores, retain the
owner read lease during OCCT work, deep-clone/serialize in the normal path,
sew across Parts, infer Members from edges, thicken Sheets, cap openings,
fabricate solids, facet unsupported exact surfaces, assign analysis properties,
or perform solid-to-shell conversion.

Focused source tests do not qualify actual OCCT conversion, full strict audit,
large-model scaling, built wheels, or round-trip fidelity. Those suites,
builds, resolver environments, native operations, large fixtures, timing/RSS,
and benchmarks require their separate performance lease and observed evidence.
Dependency and qualification status is recorded in
[`../DEPENDENCY_MATRIX.md`](../DEPENDENCY_MATRIX.md).
