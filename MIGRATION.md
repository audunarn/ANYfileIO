# Migration to ANYfileio

ANYfileio is a curated extraction, not a filtered-history import, following the
precedent set by `ANYsolver/MIGRATION.md`.

## Provenance

From `audunarn/ANYsolver` at `8b4553cc680ff925df850e627165fc336615eaba`
(branch `extract_mat_mesh_io`):

| Source | Destination |
| --- | --- |
| `src/anysolver/sesam_fem/diagnostics.py` | `anyfileio/diagnostics.py` |
| `src/anysolver/sesam_fem/records.py` | `anyfileio/sesam/records.py` |
| `src/anysolver/sesam_fem/schema.py` | `anyfileio/sesam/schema.py` |
| `src/anysolver/sesam_fem/document.py` | `anyfileio/sesam/document.py` |
| `src/anysolver/sesam_fem/validation.py` | `anyfileio/sesam/validation.py` |
| `src/anysolver/sesam_fem/exporter.py` | `anyfileio/sesam/exporter.py` |
| `src/anysolver/sesam_fem/sif_importer.py` | `anyfileio/sesam/sif.py` |
| `src/anysolver/sesam_fem/importer.py` (neutral half) | `anyfileio/sesam/semantics.py` |
| `src/anysolver/sesam_fem/__main__.py` | `anyfileio/__main__.py` |
| `src/anysolver/external_references.py` (parsers) | `anyfileio/calculix/frd.py`, `dat.py` |
| `src/anysolver/external_references.py` (deck formatting) | `anyfileio/calculix/deck.py` |
| `src/anysolver/reference_cases.py` (deck reading) | `anyfileio/calculix/inp.py` |

The SESAM modules are already free of solver imports, apart from
`importer.py`. Their move is a relocation rather than a rewrite.

## Verified equivalence

Checked with both packages importable at once, exact rather than close:

- **Raw records** for three files, including numeric and text fields, the raw
  source lines and the line span of every record.
- **Typed documents** field by field -- nodes, elements, materials, sections,
  boundaries, element references, unit vectors, coordinate transforms, header,
  load records, dependencies, record counts, unknown records and diagnostics --
  compared by field value, since the two packages define distinct dataclasses.
- **Validation** diagnostics, code and message.
- **Writer output byte for byte**, in both canonical and raw mode, on a FEM file
  and a SIF file, together with the record and byte counts in the report.
- **SIF stress** across three load-case selections, plus the summary.
- **`beam_section`** on every section in the fixture, and on ``None``.
- **FRD, DAT and merged results**, field by field and through ``summary()``,
  including the no-recognized-table warning path.
- **Deck reading**: node array, element count and the geometry summary.

The permanent parity gate lives in ANYsolver, because ANYfileio cannot import it.

## Deliberate behavioural differences

All were reviewed during the coordinated strip:

- **Materials become ANYmaterial `MaterialSpec` records.** The solver's
  `_add_materials` called `model.add_material` directly and substituted defaults
  silently; the specification is validated, so a material the file describes
  inadmissibly is reported as `FEM124` and skipped. Elements referring to it still
  import, and the adapter decides what to substitute.
- **`SesamFemImportResult` is replaced by `SesamSemantics`**, which has no
  `model` field and adds the mesh, the material records, per-element thickness and
  sections, the resolved shell local axes and beam orientations, the supports, the
  pressures and the gravity vector. Everything the solver adapter needs is on it,
  including the metadata previously attached to solver elements as
  `sesam_local_axes` and `sesam_transform_ids`.
- **A shared `FileFormatError` base**, with `SesamFemError` and a new
  `CalculixError` under it. `SesamFemError` keeps its name, code and diagnostics.
- **Triangles and quadrilaterals are separate** in the mesh.
- **The deck writer takes a neutral `DeckModel`** and requires resolved shell
  material orientations rather than computing them, because that computation needs
  the element's own centre frame.

## Included

- **SESAM formatted FEM** — the raw record reader and writer, the typed document
  model, the element/record schema registry, document validation, and guarded
  canonical or raw round-tripping.
- **SESAM SIF** — nodal and element stress results, including the two shell
  payload layouts the reference cases use (lower/upper blocks for first-order
  shells, result-point rows for second-order T6/Q8).
- **CalculiX** — `.frd` and `.dat` result parsing with dataset merging, `.inp`
  deck reading (node and element summaries, convergence tables), and deck
  writing driven by a neutral mesh plus material, section, boundary and load
  records.
- A suffix-dispatching `read`/`write` façade, a tkinter inspector, and a CLI
  keeping the existing `inspect`/`validate`/`roundtrip`/`import-summary`
  subcommands.

## Excluded

- `build_fe_model_from_sesam_document`. It constructs solver `FEModel`,
  `ShellElement`, `BeamElement` and `BoundaryCondition` objects and stays in
  ANYsolver as the adapter, so that
  `from anysolver.sesam_fem.importer import import_sesam_fem` — used by
  `ANYstructure/anystruct/api.py` and `fe_plate_fields.py` — keeps working
  unchanged.
- From `external_references.py`: reference-case generation, CalculiX process
  execution, executable resolution, solver provenance hashing, observable
  extraction and comparison evaluation. Those are verification-harness
  concerns, not file I/O, and the distinction matters: this package must not be
  able to claim a deck was executed.
- From `reference_cases.py`: case discovery and the upstream manifest.
- Semantic SESAM export from an arbitrary model. Round-tripping a document this
  package parsed is supported and guarded; synthesising an interchange file from
  a model it never read would look authoritative without being so.

## Note on a private import

At extraction time, `ANYstructure/anystruct/fe_plate_fields.py` imported
`_beam_section` from `anysolver.sesam_fem.importer`. The consumer now uses the
public `anyfileio.sesam.semantics.beam_section` API.

## Import changes

Applied in ANYsolver 0.2. ANYfileio is now authoritative for interchange syntax
and parsing; ANYsolver retains solver-specific adapters and compatibility
facades.

| Previous import | Replacement |
| --- | --- |
| `anysolver.sesam_fem.records` | `anyfileio.sesam.records` |
| `anysolver.sesam_fem.document` | `anyfileio.sesam.document` |
| `anysolver.sesam_fem.schema` | `anyfileio.sesam.schema` |
| `anysolver.sesam_fem.sif_importer` | `anyfileio.sesam.sif` |
| `anysolver.sesam_fem.importer._beam_section` | `anyfileio.sesam.semantics.beam_section` |
| `anysolver.parse_calculix_frd` | `anyfileio.calculix.frd.parse` |
| `anysolver.parse_calculix_dat` | `anyfileio.calculix.dat.parse` |
| `anysolver.write_calculix_input_deck` | `anyfileio.calculix.deck.write` (+ solver-side flattener) |

`anysolver.sesam_fem` survives as a thin adapter package, and ANYsolver
re-exports the old top-level names through its `0.2.x` line with a
`DeprecationWarning`.

## Tests

`ANYsolver/tests/test_sesam_fem.py` is migrated with its fixture strings
verbatim. Three tests that asserted on a built `FEModel` are rewritten against
`read_sesam_semantics`, since what they were really checking -- the element
topology, the thickness, the boundary flags, the shell transform metadata and the
beam orientation -- is all resolved here; only the `ShellElement` construction
moved on.

The CalculiX parser tests are new: `ANYsolver/tests/test_external_references.py`
mostly exercises case generation, executable resolution and comparison
evaluation, none of which came across.
