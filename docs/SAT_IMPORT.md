# Standalone SAT geometry import

`anyfileio.sat_import.read_sat(path, *, freecad_python=None,
inventor_loader=None, timeout=60)` returns `SatImportResult`: a new native
ANYgeometry model, source SHA-256, metre scale, diagnostic messages and source
vertex/edge/face maps. Attach the returned geometry to a new application project.
Structural materials, thicknesses, sections and solver semantics are unassigned.

The bounded dependency-free route reads indexed SAT version 2000 planar faces
with straight boundaries and loose straight edges. Curved sources use an optional
local FreeCAD Python plus InventorLoader adapter, discovered from the ordinary
Windows installation or supplied explicitly. The adapter reads a private source
snapshot, runs without a visible window, has a finite timeout capped at 60 seconds,
and never edits the original. No remote service or tessellation is used.

The optional route retains analytic ellipse intervals and plane/cylinder supports;
clamped nonperiodic rational B-splines retain their controls, weights and knots.
Supported ACIS laws are explicitly piecewise source curves with positive affine
parameter maps and optional affine placement. Unsupported curves, surfaces,
body transforms, holes, malformed topology, unsupported laws, and native
representation failures refuse the whole import. This is surface/curve geometry,
not a solid modelling or structural concept reconstruction claim.

All source vertices and edges are retained by identity, without coordinate welding.
A source edge may become several native edges at exact piece boundaries or ellipse
chart splits. Plane/cylinder face supports retain geometry; cylindrical charts are
centered on their trims to avoid a parameter seam crossing a narrow patch.

Coordinates convert to metres once from the explicit SAT units header. The fresh
model uses source-declared absolute resolution for curve endpoint and support
residual admission. Points and curves are not moved. Source precision, hash,
face identity/sense, measured endpoint and sampled support residuals are persisted
as face metadata. Support residual reporting samples 33 stations per native edge;
it is a sampled observation, not a global mathematical error bound. Native
geometry topology validation remains required. Machine-zero law intervals and
straight leaf images bounded by 16 coordinate ULPs are reported with counts and
maximum spatial extent; an entirely collapsed source edge refuses.

Focused synthetic tests are in `tests/test_sat_import.py`. Proprietary input and
local evidence remain outside the repository. The supplied 103 face/300 edge source
was imported to 329 vertices/448 native edges/103 faces and saved/reopened with empty
native topology diagnostics. All 181 source vertices and 300 source edge identities
were retained. This does not establish meshing or analysis acceptance.


Review corrections preserve the source straight-curve root/direction and trimmed
parameter interval. Source lines whose endpoints differ from admitted topology
vertices use degree-one NURBS; they are not replaced by vertex chords. Source
face sense changes the actual native plane/cylinder normal, including reversed
cylinder parameter sweeps. Affine law matrices are decoded independently from
thirteen raw source values to avoid the installed dependency's duplicated
matrix entry. Nonunit scale outside sixteen ULPs of one, singular, malformed or
nonfinite transforms refuse. Machine-zero straight leaf admission is performed
only after all source law transforms, with measured transformed extent reported.

`read_sat(..., cancelled=callback)` accepts an optional cancellation callback.
Cancellation raises `SatImportCancelled`, a `SatImportError` subtype with code
`sat.cancelled`, and never returns a partial model. Callback checks surround
source reading and conversion and run between native construction steps. The
local adapter drains stdout/stderr in bounded communicate slices; cancellation
or timeout kills and reaps its owned child before raising. Error diagnostics
retain the final captured stdout/stderr tails. Numerical-zero removal additionally
requires extent no greater than source absolute precision, even at large world
coordinate magnitudes.
