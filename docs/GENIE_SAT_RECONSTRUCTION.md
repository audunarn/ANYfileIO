# GeniE concept/SAT reconstruction — initial source slice

This development slice reads GeniE structure-concept XML containing independently
base64-encoded segments of a zipped SAT. It requires explicit metre model units.
Use `anyfileio.genie_sat.read_genie_concept(path)` to obtain the concept records
and indexed SAT entities. Every referenced named face and edge is resolved;
duplicate IDs/names, missing entities and broken loop cycles are errors.

The optional `anyfileio.genie_geometry.reconstruct_linear_geometry(document,
names)` creates a new ANYgeometry model. It currently admits one planar face
with one straight-edged boundary loop per plate, or one straight SAT edge per
beam. SAT vertex/edge identity is retained in the reconstruction caches; points
are not welded by coordinate. Structural sheet/member names are populated.
The function returns geometry and a concept-to-entity map. It creates no solver
elements and imports neither ANYfem nor ANYsolver.

This is not a generic SAT kernel, a complete GeniE converter, or a round-trip
writer. Curved edges, multi-face plates, multi-edge beams, holes, exact original
surface parameterization/orientation, complete section/offset semantics, sets,
attachments, and project orchestration still require implementation. Unsupported
selected geometry raises `GenieSatError`; no chord approximation is substituted.
The XML reader currently exposes only the initial concept/property subset.

Real input evidence (kept outside the repository):

- Source: StrippedC2.xml, with T1.FEM as comparison data.
- Parsed: 2,900 concepts, 61,441 SAT entities, 6,166 named face/edge references.
- Reconstructed: Pl877 and Bm1261, 6 vertices/5 edges/1 face/1 member.
- Saved and reopened ANYfem geometry project, `mesh_only=False`.
- Maximum nearest-FEM-node distance: 6.76210534861298e-7 m. This is a positional
  observation, not remeshing, structural-equivalence or analysis acceptance.
- Evidence: `C:/ANYworkspaceWork/aquatraz-c2-proof-report.json` and reproducible
  `aquatraz-c2-reconstruction-proof.py`; project `aquatraz-c2-editable-proof.anyfem`.
- Tests: 13 passed, covering segmented archive parsing, topology, shared identity,
  geometry persistence, unsupported curves, missing refs, duplicates, units and
  existing dependency layering. Log: `aquatraz-c2-converter-tests-final.log`.
- A prior layering failure is preserved as `aquatraz-c2-converter-tests-failed-01.log`.
  Optional owner loading was corrected. Python's pyreadline3 emits a teardown
  handle warning after pytest; all collected assertions passed.

Next implementation slice: translate a real ACIS intcurve/NURBS boundary and a
curved surface, retaining parameter ranges and trim topology; prove it through
ANYgeometry persistence before expanding coverage. No release qualification or
full-model reconstruction is claimed.

Connected sample correction: the first independent Pl877/Bm1261 selection was
spatially separated. The proof script/project now reconstructs Pl1311 with
boundary beams Bm507 and Bm508. The beams share actual source SAT edges with the
plate, preserved as geometry edges 3 and 4; this was also checked after reopening.
Current counts are 6 vertices/6 edges/1 face/2 members; topology audit is empty.
The updated maximum nearest-FEM-node distance is 4.3152377616459716e-7 m.
Evidence: aquatraz-c2-connected-proof.log, updated aquatraz-c2-proof-report.json,
and aquatraz-c2-connected-proof.png. The prior report is retained separately.

## Expanded SAT and GeniE concept interoperability

Pure-Python ACIS version-2000 SAT support (`anyfileio.sat_import`) and
`anyfileio.geometry` (`.sat` in `geometry_format`, `read_geometry`,
`write_geometry`) now support without requiring FreeCAD or `ANYfileIO-occt`:

- Curves: `straight-curve` (`forward`/`reversed`), `ellipse-curve` (`Arc` and
  `EllipticArc`), and `intcurve-curve` (`exact_int_cur`, `nurbs_int_cur`,
  `int_int_cur`, `par_int_cur` with `nubs`/`nurb` and rational `nurbs`/`rat`
  B-splines).
- Surfaces: `plane-surface` (`Plane`), `cone-surface` (both `Cylinder` and
  frustum/apex `Cone`), `sphere-surface`, `torus-surface`, and `spline-surface`
  (`NurbsSurface` with `CurveOnSurface` boundary pcurves).
- Multi-loop faces: inner holes (`Face.holes`) across planar and curved
  surfaces via ACIS `$next_loop` chains.
- Export: `write_sat` / `serialize_sat_bytes` emit atomic version-2000 SAT
  files with explicit unit conversion (`m`, `mm`, `cm`) and optional `dnvscp`
  name attributes.

In `anyfileio.genie_sat` and `anyfileio.genie_geometry`:

- `read_genie_concept(path, *, allow_unit_conversion=False)` retains the
  default metre requirement and optionally converts `mm`/`cm` XML documents.
- `reconstruct_genie_geometry(document, names=None)` reconstructs planar and
  curved plates/shells (including holes), multi-segment straight and curved
  beams, and structure metadata (`material`, `thickness_ref`, `thickness_m`,
  `section_ref`, `section_type`). `reconstruct_linear_geometry` remains
  unchanged.
- `write_genie_concept(path, geometry, ...)` exports plates/shells and
  straight/curved/multi-segment beams to `<DNV_structure_concept_protocol>` XML
  with base64-chunked zipped ACIS 2000 SAT.
