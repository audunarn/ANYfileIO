# Geometry interchange coverage

Current development candidate, not a release or blanket parity claim. Exact
source identities and dirty hashes are in the task evidence baseline and final
candidate manifest. Source tests, installed tests and CI are separate claims.
Unsupported combinations fail explicitly and atomically.

Independent review exposed a large-offset false success (about0.00427m vertex
drift at offset1e12). The current frozen candidate adds a source-to-file physical
error guard and rejects that case. Full source and fresh-installed suites pass;
independent mathematical review is complete and findings are addressed. Actual-file identity alone does
not establish numerical fidelity; earlier passing snapshots did not waive this
finding.

Current format status takes precedence over historical checkpoint descriptions:

| Format | Editable import | Editable export |
|---|---|---|
| Native JSON/gzip | Owner serializer; identity and full payload retained | Owner serializer; atomic persistence |
| BREP | Declared supported subset; explicit units required; ordinary import reports structural shell/wire grouping loss | Native-record association, Member station route and physical precision guard verified within the declared OCCT subset |
| STEP | Declared supported subset and XCAF organization | Refuses `geometry.association_unproved`; provenance restoration planned |
| IGES | Declared supported subset and format limitations | Refuses `geometry.association_unproved`; provenance restoration planned |

CAD asset operations remain separate from editable conversion. The family table
below records routes and checks across named historical snapshots; it must not be
read as current STEP/IGES editable-export availability. Current runtime-gated
candidate passes493 source and512 installed tests with no failures or warnings;
the CI command selects the compatible legacy property format. Claude subscription
mathematical review is complete; its findings and inspection limits are
adjudicated. Earlier six refusal-oracle failures and all other failed evidence
remain preserved. Exact commands, hashes and review dispositions:
`C:/ANYworkspaceWork/anyfileio-geometry-20261027/identity/review-final/verification-index.json`.

The numerical guard uses complete native defining data, exact SI ratios and the
source owner's computational tolerance reserve. Unproved parameter transports
refuse. Two previous surface roundtrips currently refuse atomically because their
pcurve enclosure leaves the active NURBS surface basis: differing-resolution
Coons boundaries in BREP, and the square surface with model-mm/output-in units.
These routes need a separate surface extrapolation proof. Other Coons grids and
bare line/point mm-to-inch physical conversion retain positive coverage. Ordinary
BREP wire-to-structural-Member reconstruction remains open; native JSON retains
the complete Member history.

The corrective checkpoint passed298 source/341 installed tests and Claude
subscription reassessment. A later product-affiliation snapshot passed320 source
and363 installed tests. Review then exposed mixed-wire and incomplete free-wire
accounting; the final narrow guards passed127 affected source tests and127
installed-wheel tests, with a scoped Claude reassessment finding no defect.
Broad counts remain bound to their preceding snapshots. Exact commands, hashes,
review limits and preserved failures are indexed in
`C:/ANYworkspaceWork/anyfileio-geometry-20261027/reference-verification-index.json`.

Loose STEP reference points/edges retain actual product affiliation without fake
Members. Mixed face/free wires and incomplete native wire walks explicitly refuse.
Shared face-corner STEP splitting remains a historical transport limit. Member
station admission is being implemented. BREP actual-file export references now
have a verified native-record route; the other format routes and remaining matrix
are open. This is development evidence, not full interchange or release acceptance.

| Owner family / behavior | Native JSON/gzip | Family routes and recorded checks across historical snapshots |
|---|---|---|
| Points, Straight | Payload/identity roundtrip verified | STEP/IGES/BREP panels; shared edges and coincident independent entities checked |
| Arc, Spline, NurbsCurve | Native facade and owner codecs checked | Real free curves and rational controls; BREP shared endpoints checked. Naked STEP curve sets that split vertex identity now refuse atomically; closed circles split into open arcs |
| EllipticArc, LineRange | Native facade schemas 5/8 checked | Real ellipse/retained-line file checks and directed ranges |
| CylinderIntersectionCurve | Owner codec delegation | Explicit bounded free-curve BREP export/reread checked; STEP shared polyline vertices can be split by native transport and now refuse |
| QuadricIntersectionCurve, BezierQuadricCurve | Owner serialization suites checked | Real public-factory bounded BREP exports and rereads checked |
| PlaneNurbsCurve | Owner codec delegation | Opt-in bounded BREP whole-open and clipped-open section trims: four-face Plane/NURBS sharing, independent native references, actual 3D/all-pcurve and literal raw-unit import certificates checked. Clipped import tests explicit depth48; unresolved domains and other formats refuse |
| CurveOnSurface | Native rational records delegated | Exact affine/isoparametric paths; bounded free curves and nonplanar STEP/BREP trims checked |
| Plane, holes and reversed loops | Native panel payload checked | Actual planar faces, holes and shared topology checked |
| Cylinder, Cone | Owner codecs checked | Analytical patch charts/interiors; scalar and batch seam regressions fixed in owner |
| ExtrudedSurface / translated Coons | Schema-6 native facade checked | Polynomial routes plus exact nonperiodic elliptic natural boundaries; effective ranges, oblique/leaning bases, reversal and units checked in all three CAD formats |
| RuledSurface, explicit sampled Coons | Owner codec delegation | Exact natural grid boundaries, interior border splits, differing grid resolutions and public face reversal checked in STEP/IGES/BREP; arbitrary interior trims refuse |
| Topology-backed polynomial Coons | Owner public materialization checked | Four single-edge polynomial sides and recognized translations; unsupported coefficients/chains refuse |
| NurbsSurface | Schema-7 owner/native delegation | Exact rational patches/pcurves; equal-value supports with equal exact incident pcurves preserve shared edges in all three CAD formats; opt-in noniso trim certificates checked |
| Part/Sheet/Member organization | Complete owner payload delegated | STEP flat products/names and declared oriented uses checked; BREP container/use boundaries with name loss; IGES declared parts refuse |
| Repeated/nested CAD occurrences | Not an external native-file concern | XCAF prototypes, separate coincident instances, nested rotations/translations and per-prototype meshes checked |
| History, groups/tags, lineage, relationships, CRS | Full owner payload delegated | Format-specific losses reported; no fabricated connection intent or CRS conversion |

## Evidence

Evidence root: `C:/ANYworkspaceWork/anyfileio-geometry-20261026`. Supervised runs
retain exact commands, source paths, effective temporary directory, timeout,
precise process-wall time, exit/cleanup status and raw output.

- Final core contract checks: `core-candidate2-contracts`, 153 passed, one
  unrelated missing ANYmesher skip.
- Other core regressions: `core-remaining-regression`, 167 passed, 21 missing
  unrelated ANYmesher skips.
- Owner codecs: `geometry-owner-serialization`, 109 passed; current-schema
  selected roundtrips: `native-owner-current-schemas`, two passed.
- Owner support materialization: `owner-interchange-03`, 22 passed.
- Owner chart inversion: `owner-seam-tests-02`, 74 passed; independent review
  and 17 focused scalar seam tests passed.
- Final clean NumPy-only installed base: `final-base-tests`, 30 passed,
  18 explicit owner-absent skips.
- Final provider source suite: `provider-final-core01`, 90 passed through the
  real registered core reader, including all read lifecycles and identity.
- Final installed core/native suite: `final-installed-tests`, 95 passed,
  zero skips. Outside-source `final-installed-smoke` checks native JSON/gzip
  and editable STEP/IGES/BREP, installed origins and no retained-source leak.
- `final-installed-cli` checks six parseable JSON CLI operations, including
  bounded nonplanar trim conversion. All three wheels pass `final-twine-check`;
  `final-pip-check` passes. Exact hashes are in `final-wheel-hashes.json`.
- Earlier pcurve, face, shell and same-count vertex weld/split tampering failures
  remain preserved; the final topology and curve-proof review is clean for this
  admitted domain. Source payload identity, cancellation and budgets are checked.

## Fidelity and remaining limits

Approximation is opt-in, with a physical deviation limit and bounded work. Free
curve certificates include serialized endpoints. Admitted generic NURBS trim
certificates include the source curve, actual file 3D curve AND actual file
pcurve-on-support, using conservative owner boxes and exact physical unit ratios.
Native fit tolerances and samples never establish acceptance. Oriented topology
preservation is an additional gate, separate from the curve error certificate.

STEP may add open carrier shells around previously unowned faces. These transport
wrappers do not establish that source sheet intent was preserved. Declared shell
face groups, oriented outer/hole cycles and geometric incidence must survive.

Remaining limits include arbitrary periodic/seam NURBS, shared multi-support
trims, unrepresentable exact chart coefficients, general multi-edge/angular Coons
materialization, arbitrary interior sampled Coons/Ruled and partial/periodic elliptic trim charts,
and extreme/nonrepresentable parameter cases outside the checked domain. Editable solids,
healing and external STEP dependencies are deliberately refused; native CAD
assets can retain solids without claiming editable conversion.

Failed evidence remains on disk, including environment/DNS failures, incorrect
fixtures, Windows system-temp permissions, CAF units, unsupported binding calls,
and cone scalar/vector branch failures leading to owner fixes. CI definitions
exist but are UNRUN here. Full every-family parity and release qualification are
not accepted by this matrix.

## 2026-10-27 continuation evidence

Evidence root: `C:/ANYworkspaceWork/anyfileio-geometry-20261027`.
`surface-expanded05`:51 passed; `surface-full01`:141 passed,8.828s process wall.
This adds natural-boundary surface coverage with unchanged1e-12m analytical
acceptance for O(1m) cases. IGES output now uses object-local17-significant-digit
formatting; its previous default truncation failed the original target and is
preserved in failed evidence. Success/failure/cancellation restore unit settings.
Independent surface review is clean for the declared subset. Installed-package
verification and same-support shared-edge continuation are pending at this
checkpoint. Earlier final evidence above belongs to the 2026-10-26 candidate.

Shared-support checkpoint:`shared-transport02`25passed; `shared-full02`167passed,
11.141s process wall, successful owned-process cleanup. Strict incident-pcurve
equality applies only to shared NURBS edges, preserving the prior single-face
route. STEP groups existing edge-connected unowned faces in explicit OPEN
transport carriers; no coordinate welding or structural sheet inference.
Exact export now checks actual-file material counts and shell state. This is
NOT yet the complete incidence-graph proof. Known naked STEP free curves
E3/V3 -> E3/V7 now typed-refuse and preserve existing destinations; prior tests
checking edge counts alone missed this loss. New shared/carrier review and
installed verification are pending; public topology-work option24tests pass.

Shared/support checkpoint accepted within declared subset: independent review
`review-shared-carrier-final.txt` clean. `installed-shared-suite01`:172 passed,
12.469s process wall, test files copied outside source roots; installed origins
verified independently. `installed-shared-smoke01`:2native/3CADroundtrips;
`installed-shared-cli01`:6JSONoperations. Three wheel metadata checks and pip
check pass. NumPy-only updated core:`core-base-options01`,6passed18expected
owner-absent skips. Exact artifact/source hashes:`shared-wheel-hashes-and-source.json`.
The next full graph implementation has started; these results refer to the
frozen shared/support wheels, not subsequent source edits. No blanket parity,
CI execution or release claim follows from this checkpoint.

Latest accepted development checkpoint (2026-10-27): the actual-file oriented
incidence graph replaces the earlier count-only gate. Exact public Ruled
reconstruction and bounded whole-open PlaneNurbs BREP import/export are covered
within their declared subsets. Literal raw BREP coordinates and all incident
pcurves participate in import error certificates, including unit-normalization
rounding. Source suite: 235 passed (`combined-section-raw-source02`); installed
suite: 240 passed (`installed-section01`). Installed native/CAD smoke checks,
seven CLI operations, wheel metadata and dependency checks also passed.
Independent reviews are clean for these slices. Evidence and exact wheel/source
hashes are in `C:/ANYworkspaceWork/anyfileio-geometry-20261027/section-verification-index.json`
and `section-wheel-hashes-and-source.json`. These results supersede earlier
checkpoint counts, without extending their declared geometry coverage.

Clipped-open PlaneNurbs BREP reconstruction is now accepted within its declared
subset. Source provider suite: 252 passed, 118.109s process wall; external installed
suite: 289 passed, 119.297s. Three CLI conversions and an expected depth refusal
preserving an existing destination pass. Independent owner-API, consumer and
manual-CI reviews are clean. Native persistence preserves the imported model
exactly; the CAD cycle preserves shared incidence and oriented boundary cycles.
Evidence: `C:/ANYworkspaceWork/anyfileio-geometry-20261027/clipped-verification-index.json`.
Default depth24 refuses; depth48 is explicitly tested. General periodic/seam
conversion, STEP free-wire topology preservation and remaining matrix entries
stay open. The STEP direct-edge prototype is partial evidence only; no production
gate has changed. No full parity, CI execution, qualification or release claim.

Current identity-repair candidate (2026-10-27) narrows editable CAD export to
BREP pending acceptance of native record provenance. STEP and IGES editable
export explicitly refuse with `geometry.association_unproved`; their earlier
round-trip results above describe historical candidates, not current export
availability. CAD asset read/write and editable import are separate paths.
BREP replacement installed checks:177passed,69.109s process wall, matching
wheel/source/installed bytes. Independent Claude subscription review confirms
the original incidence-only identity/certificate mapping defects are closed by
inspection, but further precision, foreign-read and callback tests/repairs are
in progress. This candidate is not yet accepted. Evidence:
`C:/ANYworkspaceWork/anyfileio-geometry-20261027/identity/brep-association` and
`C:/ANYworkspaceWork/anyfileio-claude-association-20261027-01/review.txt`.
