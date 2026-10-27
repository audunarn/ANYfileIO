# Editable geometry interchange

This is the current bidirectional contract. It supersedes version-specific
assumptions in `ANYGEOMETRY_0_2_ADAPTER.md` for this implementation. Historical
qualification remains scoped to its original artifacts; this document is not a
release or qualification claim. See `GEOMETRY_INTERCHANGE_COVERAGE.md` for status.

## Public boundary

```python
from anyfileio.geometry import (
    GeometryReadOptions, GeometryWriteOptions, read_geometry, write_geometry,
)

result = read_geometry("panel.anygeometry.json")
model = result.geometry
report = write_geometry("panel.brep", model)
```

Importing the facade imports neither ANYgeometry nor OCCT. Native operations
require ANYgeometry; CAD operations also require ANYfileio-occt with its geometry
extra and supported OCCT binding. Geometry/topology and native schemas remain
owned by ANYgeometry. The adapters consume public APIs, including public modules.

`read_geometry(path, *, format=None, options=None, cancellation=None)` returns a
`GeometryImportResult(geometry, report)`. `write_geometry(path, geometry, *,
format=None, options=None, read_lease=None, cancellation=None)` returns a
`GeometryWriteReport`. Options and reports are frozen; mapping fields are copied
into immutable mappings. CAD entity provenance uses `CadEntityRef` and owner
handles, never coordinate-derived identity. Native files preserve owner identity;
CAD imports create a new model identity and retain source mappings.
Export mappings associate each real model-bound `EntityHandle` with a tuple of
`CadEntityRef` values (multiple values for split curve representations). Each
mapped tuple is nonempty; repeated references can preserve retained carrier/use
multiplicity and are not deduplicated. Import
mappings associate each CAD reference with a tuple of real owner handles.
Approximation records identify the affected editable model handle, including
newly reconstructed import edges; the import mapping links these handles to
the input CAD references.

### Editable CAD output identity

`GeometryWriteReport.output_document_id` is an additive final field, defaulting
to `None`. Native persistence reports `None`. Successful editable CAD writes
report the actual output file's canonical `cad-import-v1` identity, derived by
`cad_import_document_id` from the verified file SHA-256, selected CAD format,
effective output length unit, `heal=False`, and loaded backend ID/compatibility.
Every output `CadEntityRef` binds that identity and addresses the actual reread
manifest. This is separate from the frozen legacy `cad-geometry-v1` generated
namespace in the CAD backend contract.

Before atomic publication, the facade checks output identity, report format,
source identity/revision/units, effective output units and backend version. It
uses the owner's public handle resolution API to require genuine active handles
bound to the source model, and checks nonempty tuples of typed CAD references
bound to the output document. The provider owns proof of actual manifest entity
existence, topology correspondence, and carrier/use projection; core does not
invent a copied owner catalogue or reread the file a second time for this check.
These checks do not claim UUID/history recovery or distinct native container
identity where the actual file only exposes carrier references. Standalone report
record construction remains lightweight; CAD identity is enforced at the write
boundary. CLI JSON includes `output_document_id`.

Recognized formats: `anygeometry`, `step`, `iges`, `brep`. Native compound suffixes
are `.anygeometry.json` and `.anygeometry.json.gz`; plain JSON/gzip requires an
explicit native format. Existing generic `anyfileio.read` returns the native
editable result for these compound suffixes. CAD filenames must use a suffix
matching the requested CAD format; an explicit format does not override a
conflicting or unrecognized CAD suffix. Existing generic CAD reads continue
returning CAD assets; editable conversion is an explicit facade operation.

The current development provider stages editable export through BREP native
record provenance. STEP and IGES editable writes currently fail atomically with
`geometry.association_unproved` until their actual-file entity association is
established. Recognizing a format does not establish an editable export route.
CAD asset operations and editable imports retain their separately tested scope;
see the coverage record for the candidate's review and verification status.

## Fidelity and failure

Exact is the default. `fidelity="approximate"` requires a positive finite
`max_deviation_m`, with `max_segments` and `max_depth` bounding work. Defaults
are 10,000 segments and depth 24; hitting either limit is failure. Successful
approximation records its source, requested limit, conservative `bound_m` and
method. Fitting samples alone do not establish a bound. Exact representation
changes may change curve types without changing geometric meaning.

The BREP provider separately checks numerical changes between its frozen native
conversion and the actual raw and normalized file geometry. It uses complete
defining data, exact physical unit ratios and a conservative reserve derived from
the source owner's computational tolerances. Excessive or unproved error fails
before publication; this does not change writer precision or permit implicit
approximation. The adopted out-of-domain affine-curve continuation is restricted
to the verified loaded OCCT7.9.3 evaluator bundle. Its fidelity claim concerns
that evaluator's semantics, rather than every independent BREP reader.

Editable CAD export rereads the actual file and checks a complete material/use
topology witness before publication. `max_topology_work` (default 1,000,000;
CLI `--max-topology-work`) bounds extraction, correspondence and graph search
separately from fitting. Coincident independent entities remain distinct;
unresolved correspondence or budget exhaustion fails atomically. Topology
verification is separate from geometric fidelity. Approximations also require
their conservative geometric error certificate.

No silent healing, topology welding, capping, faceting, thickness inference or
entity omission. Editable solid conversion is outside the admitted domain;
solid assets can still use the CAD asset API. Unsupported entity combinations
fail atomically. Reports enumerate metadata losses and cannot turn an unsupported
geometry result into success.

Native writes delegate the complete schema to the owner and retain document
units/settings without conversion. CAD uses model-local coordinates and converts
length units once at its boundary. Unknown CAD units need an explicit override;
an override cannot contradict a declared source unit. External/CRS requests fail
with `geometry.external_transform_undefined`.
BREP requires an explicit source unit. Current IGES decoding refuses unknown
header overrides and inconsistent unit headers until a safe decoder is available.

`validation_mode="certified"` uses the owner's current certified gate. Ordinary
committed mode is not a global geometric certificate. Export requires a stable
revision; supplied read leases must bind the same exact model/revision. Without
a lease, revision checks detect ordinary mutation but do not constitute a
concurrent-read lock. Concurrent callers must provide owner-controlled exclusion.

Writes use a same-directory owned temporary, verify output identity and checksum,
flush, recheck cancellation/revision, and replace the destination atomically.
Failures before publication leave the existing destination intact. A lease
release failure after publication raises `GeometryLeaseReleaseError` with
`output_committed=True` and the completed report, making the committed file
explicit. A release failure while unwinding preserves the original exception.
Callbacks are checked between
bounded stages; a running OCCT call is not claimed to be interruptible.

## CLI and environment

```text
anyfileio geometry-convert panel.anygeometry.json panel.step
anyfileio geometry-convert source.step editable.anygeometry.json --source-unit mm
anyfileio geometry-convert source.anygeometry.json approximation.step --allow-approximation --max-deviation-m 0.0001
```

Existing output requires `--overwrite`. `--certified` requests the owner's audit
gate. `--json` emits model/revision, output checksum, approximations and losses.
`--max-segments` and `--max-depth` expose the public approximation work limits;
a tighter deviation limit can legitimately exhaust the default budget.

The NumPy-only base remains independently importable. Provider package
requirements are reconciled against core >=0.3.2,<0.4 and geometry >=0.4.7,<0.5;
those are compatibility targets, with installed-test evidence recorded separately.
The new owner support-materialization API belongs to the current unreleased
ANYgeometry source candidate. A numeric version alone does not prove that
capability is present; affected operations check for it and return a typed
missing-capability diagnostic. Evidence identifies the exact tested wheel hashes.

## Acceptance evidence

Each admitted family must be exercised through real file I/O and editable import,
with analytical/external references, topology and unit checks. Native payload
equality is stronger than entity-count equality. CAD round trips do not promise
source UUIDs, feature history or structural relationships absent from the format.
Independent review, failures, skips and unsupported cases remain explicit.

The checked generic nonplanar NURBS trim route is opt-in. It explicitly fits a
native 3D curve, then certifies source, emitted 3D curve and emitted pcurve lifted
onto its emitted support using conservative interval enclosures and exact SI
unit ratios. Separate vertex/edge bijections, oriented outer/hole cycles,
material traversal multiplicity and declared shell groups must survive. STEP
may add open transport shells only around previously unowned faces; this does
not preserve or infer original source sheet intent. Ambiguous correspondence,
periodic/located unsupported representations and exhausted work limits refuse.

Bounded BREP import also reconstructs clipped open plane/NURBS sections through
the owner's public parameter-region and cooperative-work APIs. Endpoint UV boxes propose a domain;
unchanged world endpoints, owner validation and the raw file's 3D curve and all
incident pcurves determine acceptance. The tested settings explicitly supply
`max_depth=48` and `max_segments=100000`; the default depth24 can refuse. Ambiguous
or unresolved parameter regions never produce a partial result. Older owners
without the cooperative-work API receive a typed missing-capability error for
both whole and clipped reconstruction. Native persistence remains independent of
that capability. This does not recover historical parameter domains. Certified
PlaneNurbs CAD export currently refuses rather than mixing the owner's separate
qualification context with conversion work; its cooperative validation contract
remains open. Default committed conversion still requires the geometric bound.
