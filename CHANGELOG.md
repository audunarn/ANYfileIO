# Changelog

## 0.3.0 - 2026-09-03

Changed:

- **MPL-2.0 licensing.** Starting with 0.3.0, ANYfileio source code is licensed
  under the Mozilla Public License 2.0. Original project documentation is
  licensed under CC BY 4.0. Earlier releases retain the license terms that
  accompanied them; Git history has not been rewritten.
- **Auditable notices.** The source and binary distributions include the
  repository license, copyright/scope notice, documentation-license notice,
  and the runtime dependency notice for NumPy.
- **Semantic source compatibility.** The development-only semantic adapter now
  admits compatible ANYmesher 0.2 and 0.3 sources. It remains absent from
  published extras and is not a runtime dependency of the NumPy-only package.
- **Secure publication path.** Release artifacts are built and validated in a
  credential-free job, then may be published from a separate protected GitHub
  environment using PyPI Trusted Publishing. No API token is stored in the
  repository or workflow.

Boundaries retained:

- The PyPI package contains CAD-neutral records, discovery, orchestration, and
  preview artifacts, but no native OCCT provider or native CAD capability.
- ANYgeometry, ANYmesher, ANYmaterial, CadQuery, OCP, and ANYfileio-occt are not
  runtime dependencies and are not bundled.

## 0.2.0 - 2026-08-20

Added:

- **NumPy-only 0.2 base.** Records, documents, built-in formats, the inspector,
  CLI, and accepted CAD-neutral records/discovery import without ANYmesher or
  ANYmaterial. Those accepted CAD exports remain available from the package
  facade.
- **Source-development semantic runtime.** SESAM semantic materialization and
  CalculiX deck writing retain their public APIs and lazy owner validation, but
  0.2.0 does not advertise a PyPI `semantics` extra. Development checkouts use
  the accepted ANYgeometry, ANYmesher, and ANYmaterial sources explicitly;
  missing or incompatible owners report typed `SEM001`, `SEM002`, or `SEM003`
  failures with a source-setup hint.
- **Truthful native-CAD boundary.** CAD-neutral records, discovery, orchestration,
  and preview artifacts are included. No native OCCT provider or live CAD
  capability is published or advertised by this release.
- **Embeddable inspection.** `open_inspector` opens the file inspector in a
  host application's existing Tk event loop.
- **Verification-safe CalculiX decks.** The neutral writer now defines the
  `NALL`, `SUPPORT`, and `ALL` sets, requests reaction totals, accepts both
  `buckle` and `buckling`, and exposes a configurable mode count defaulting to
  five.
- `export_sesam_fem` is available from the public SESAM and package facades.

## 0.1.0

First feature release. The format code is extracted from ANYsolver; see
[MIGRATION.md](MIGRATION.md) for provenance.

Added:

- **SESAM FEM** — the raw record reader and writer, the typed document model, the
  element and record schema registry, document validation, and guarded canonical
  or raw round-tripping.
- **SESAM semantics** — `read_sesam_semantics` resolves a document into an
  ANYmesher `Mesh` (keeping the file's own node and element IDs), ANYmaterial
  `MaterialSpec` records, per-element thickness and beam sections, resolved shell
  local axes and beam orientations, nodal supports, element pressures and gravity.
  `beam_section`, `shell_thickness`, `beam_orientation` and `shell_local_axes` are
  public.
- **SESAM SIF** — nodal and element stress results, including both shell payload
  layouts and per-load-case selection.
- **CalculiX** — `parse_frd`, `parse_dat`, `merge_results`, deck reading
  (`summarize_deck`, `read_nodes_and_element_count`, `classify_geometry`) and a
  deck writer driven by a neutral `DeckModel`.
- **Facade** — `read(path)` and `describe(path)` dispatching on suffix, and
  `supported_suffixes()`.
- **Inspector** — a tkinter window with a record tree carrying source line
  numbers, severity-coloured diagnostics, an element-type histogram, report
  export and canonical rewrite; entry point `anyfileio-gui`.
- **CLI** — `anyfileio formats|inspect|validate|roundtrip|convert|summary`, with
  `--json` and `--lenient`. The four command names carried over from
  `anysolver.sesam_fem` keep their behaviour.

Verified against `anysolver` 0.1.3 at
`8b4553cc680ff925df850e627165fc336615eaba`, with both packages importable at
once and exact rather than close: raw records including source line spans; typed
documents field by field across a FEM file, a SIF file and a section-classifying
file; validation diagnostics; **writer output byte for byte** in both canonical
and raw modes; SIF stress across three load-case selections plus the summary;
`beam_section`; FRD, DAT and merged results; and the deck reader.

Changed from the source:

- **`build_fe_model_from_sesam_document` is not here.** It constructs solver
  `FEModel`, `ShellElement` and `BoundaryCondition` objects, so it stays in
  ANYsolver as the adapter. `read_sesam_semantics` returns everything it needs to
  do that, including the shell local axes and beam orientation vectors that were
  previously attached to solver elements as `sesam_local_axes` and
  `sesam_transform_ids`.
- **`_beam_section` is now public** as `anyfileio.sesam.semantics.beam_section`.
  ANYstructure now consumes that public name instead of the former private
  ANYsolver helper.
- **A shared `FileFormatError` base**, with `SesamFemError` and a new
  `CalculixError` deriving from it. `SesamFemError` keeps its name and behaviour.
- **Materials become `MaterialSpec` records, not solver materials.** A material
  the file describes inadmissibly is reported as `FEM124` and skipped rather than
  substituted silently; elements referring to it still import.
- **Triangles and quadrilaterals are kept apart** in the mesh, rather than
  flattened into one "shell" bucket.
- **The deck writer takes a neutral `DeckModel`.** Resolved shell material
  orientations are supplied by the caller, because working one out needs the
  element's own geometric frame and the element owns that frame. ANYsolver keeps
  the flattener that computes them.

Known limitations, stated rather than worked around:

- Semantic SESAM export from an arbitrary model is not offered at all.
- The deck writer covers S3/S4/S6/S8 shells, B31/B32 beams, isotropic and
  orthotropic elasticity, shell and equivalent-rectangular beam sections,
  boundary conditions and point, pressure and gravity loads. Plastic material
  curves, generalized section resultants and coupled 6x6 beam stiffness are not
  written; ANYsolver refuses those cases too.
- `read` dispatches on suffix, not on content. A mislabelled file is reported as
  what its name claims, because guessing past the label hides the mislabelling
  from whoever has to fix it.

## 0.0.1

- Repository scaffolding: packaging metadata under the distribution name
  `ANYfileio`, CI across Python 3.11-3.14 on Windows and Linux, and the layering
  checks that keep the arrow pointing away from ANYsolver.
