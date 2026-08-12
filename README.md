# ANYfileio

Reading and writing structural finite-element interchange files: SESAM formatted
FEM (`.fem`) and SIF (`.sif`), CalculiX input decks (`.inp`) and results (`.frd`,
`.dat`), with a tkinter inspector and a command-line interface.

After compatible ANYmaterial and ANYmesher releases are available on the same
package index, install with `python -m pip install ANYfileio`. Until then, use
the sibling-source development setup below.

The repository is `ANYfileIO`, but `anyio` on PyPI is the well-known async
compatibility library — a transitive dependency of httpx and starlette — and a
top-level import package by that name would shadow it. The distribution is
therefore **`ANYfileio`** and the import package is **`anyfileio`**.

## Quick start

```python
import anyfileio as io

# Layer 2: what the file means, without interpreting it further.
document = io.read_sesam_fem_document("model.FEM")
len(document.nodes), len(document.elements), document.record_counts["GELMNT1"]

# Layer 3: as much of that as maps onto a mesh and a set of records.
semantics = io.read_sesam_semantics("model.FEM")
semantics.mesh.quads                    # an ANYmesher mesh, file node IDs kept
semantics.materials[1].build()          # an ANYmaterial material
semantics.supports[0].dofs              # ('ux', 'uy', 'uz')

# Results from a run elsewhere.
results = io.parse_frd("case.frd")
results.displacements[42]

# Or just read whatever this is.
io.read("case.dat")
```

## Layers

Each format is read in three layers, and each is useful on its own:

| Layer | Answers | Needs |
| --- | --- | --- |
| Records | what does the file say? | numpy |
| Document | what does it mean? | numpy |
| Semantics | what mesh and materials is that? | ANYmesher, ANYmaterial |

Most real questions about a file from another tool stop at the first or second
layer — is it well formed, what element types are in it, what does it reference
that is missing. Those answers must not require a mesh library, a material
library or a solver, and with this layering they do not.

It also means a file can be round-tripped without being understood. Records this
package cannot interpret are **preserved and rewritten**, so canonicalizing a
file does not silently delete the parts it did not recognize — the failure mode
that makes people distrust converters.

## Command line

```bash
anyfileio inspect model.FEM
```

`formats`, `inspect`, `validate`, `roundtrip`, `convert` and `summary`. `--json`
for machine-readable output, `--lenient` to collect diagnostics instead of
failing on the first error. `validate` exits non-zero on an invalid file, so it
works as a check in a build.

`anyfileio-gui` opens the inspector: the record tree with source line numbers,
diagnostics coloured by severity, an element-type histogram, and buttons to save
a report or write a canonical copy.

An existing Tk application can open that same inspector without starting a
second event loop:

```python
from anyfileio.gui import open_inspector

window, inspector = open_inspector(root, selected_path)
```

## Diagnostics are data

Strict reading raises on the first error. Lenient reading collects
`FemDiagnostic` values — a code, a severity, the record name, the source line
range and context — alongside whatever parsed successfully.

Source line numbers are carried from the record layer upwards for one reason: a
diagnostic that cannot point at the text that caused it is not actionable on a
file of a hundred thousand records.

Severity matters as much as the code. An element referencing a missing node is an
error; an element referencing an undefined material is a warning, because the
document is still readable and the caller may not care.

## What is refused, and why

**Semantic SESAM export from an arbitrary model is not implemented**, and not
because it would be hard. A `.fem` file is an interchange format: whoever
receives one treats it as authoritative. Writing one from a model this package
never parsed would produce a file whose fidelity nobody has established, and it
would look exactly like a file whose fidelity had been. Round-tripping a parsed
document is supported and guarded; synthesis is not offered at all rather than
offered with a caveat in a docstring.

The CalculiX deck writer refuses in the same spirit rather than approximating
silently: an orthotropic beam (the equivalent rectangular section cannot carry an
independent torsional rigidity), an orthotropic shell with no resolved material
orientation (a deck without one would quietly align the material with the global
axes), and an element set mixing thicknesses (one `*SHELL SECTION` covers a set,
so writing it would mean picking one and losing the other). What it *does*
approximate — a beam section written as an equivalent square — is listed in
`DeckReport.assumptions`, and every report carries
`execution_mode="not_executed"`, because a deck that has not been run says
nothing about agreement.

## Position in the family

ANYfileio sits above [ANYmesher](https://github.com/audunarn/ANYmesh) and
[ANYmaterial](https://github.com/audunarn/ANYmaterial) and below ANYsolver. It
hands back a neutral mesh and neutral records; turning those into solver elements
is the solver's job. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
layering and [MIGRATION.md](MIGRATION.md) for what was extracted from where.

## Units

SI throughout. A SESAM file declares its own units in its header; those are
honoured on read and converted once, at that boundary, so nothing downstream has
to know the file was not already SI.

## Development

```powershell
python -m pip install --no-deps -e C:\Github\ANYmaterial
python -m pip install --no-deps -e C:\Github\ANYmesh
python -m pip install -e "C:\Github\ANYfileIO[dev]"
python -m pytest
```

For both TestPyPI and PyPI, publish `ANYmaterial` and `ANYmesher` before
`ANYfileio`. The publish workflow enforces that both compatible 0.1.x
dependencies already resolve on the selected index.

To open the inspector straight from a checkout — including an IDE's Run button —
run [`run_gui.py`](run_gui.py) at the repository root, optionally with a file to
open. It also picks up side-by-side `ANYmesh` and `ANYmaterial` checkouts, so the
family works together with nothing installed.

```bash
python run_gui.py model.FEM
```
