# ANYfileIO / OCCT CAD dependency and qualification matrix

Status: frozen dependency metadata and explicit qualification ledger for the
ANYfileIO 0.2 / ANYfileio-occt 0.1 implementation line.

Date of metadata observation: 2026-08-12 (Europe/Oslo).

Authoritative inputs:

- source plan SHA-256
  `473523BD3BD28FC88487A961C29BF7B640592F415B981236C558FA963AF1E414`;
- registered baseline addendum SHA-256
  `9249191E78C746A81A2B7D80B8ADA543AD45FCAE9CA41F5CAB04E169D68796A1`;
- registered Forseti M2 allowlist SHA-256
  `87A30E18F4DCF6D7CE194AC4CE05909BC149027128A8F2CE5EFB421310185697`;
- accepted ANYfileIO M2 base
  `0d2c7f8ef1b17f42f667d6183125e51cb650a70d`;
- [`docs/CAD_BACKEND_CONTRACT.md`](docs/CAD_BACKEND_CONTRACT.md) and
  [`docs/ANYGEOMETRY_0_2_ADAPTER.md`](docs/ANYGEOMETRY_0_2_ADAPTER.md),
  committed with this ledger. Their commit and blob hashes are recorded in the
  handoff rather than embedded recursively in their own content.

## 1. Status vocabulary

This file separates metadata from evidence.

| Status | Meaning |
| --- | --- |
| `FROZEN` | Contract value implementations must use. |
| `ACCEPTED` | Owner handoff identified by exact commit/artifact evidence. |
| `BLOCKED` | An owner or prerequisite must supply a value; it is not guessed. |
| `UNRUN` | No qualifying command has been run for this cell. |
| `UNSUPPORTED` | Deliberately outside the supported heavy-backend matrix. |
| `PASS` / `FAIL` | Reserved for an observed, recorded qualification outcome. |

Package-index metadata, a local checkout, and a source import do not constitute
a resolver, wheel, platform, isolation, performance, or release pass. Every
qualification cell starts `UNRUN`.

## 2. Revalidated repository observations (2026-08-12)

These tips establish provenance, not completion of another owner's active
work. Revalidate immediately before every handoff, branch, merge, edit, and
qualification gate.

| Repository | Observed exact state | CAD authority |
| --- | --- | --- |
| ANYfileIO | primary checkout clean at accepted Forseti M2 `0d2c7f8ef1b17f42f667d6183125e51cb650a70d`, direct parent `82a0f5f110361fcd902cd3aac5d4c6beeaa187fa`; registered isolated contract worktree is an uncommitted direct-child candidate | Contract/core work descends from M2 and preserves its README, pyproject, and packaging-test identity hunks. |
| ANYfileio-occt | clean license-only root `571231dc4c7d8b4131daac6b719a6b93125a20b4` | Separate heavy backend after registered exact-file plans. |
| ANYgeometry | public tip `37234b7bc6b6c3f2e02cf1c53acb875245d9c3aa` on `native_hybrid_mesher`; qualified code parent `8828019e0f940b0d6f240b98f8be17d6f306155b`; version 0.2.1/schema 4; unrelated untracked `.github/`, `.idea/vcs.xml`, and `dist_gap_closure/` | Read-only accepted committed dependency; never touch the dirty checkout. |
| ANYmesh / ANYmesher | current committed tip `574fac99db064cc447bdb3e91ff029047a3c2248` on `native_hybrid_mesher`; untracked installed-wheel smoke reports remain | Read-only; newer commits/reports do not themselves constitute the accepted 0.2.x owner handoff/artifact. |
| ANYfem | current committed tip `7a41baca4bd4d1a5cb538ec6148c6ca51c79d1f2` on `native_hybrid_mesher`; dirty `scene.py`, `viewport.py`, `test_scene.py`, plus untracked `test_overlap_and_generator_ui.py` | No CAD edit until committed/accepted V6, native-selector, UI/settings, plate-ownership, and selector-parity tips are handed off. CAD persistence is V7. |
| ANYsolver | `7daa6e8c61954cfc1bc4469457fef0db154d3375` on `native_hybrid_mesher`; dirty CI/publish/README/CHANGELOG/pyproject plus untracked compatibility test; separate S4 worktree context remains | Dependency change is a solver-owner handoff, never a CAD edit. |
| ANYstructure | `4a79b860739c2f0b24f61314d4c13d943886bdd3` on `clean-up-after-external`; untracked `.claude/` | Later owner-approved isolation work only. |
| ANYmaterial | `4626887667f4c251479d26f321b9e73b046a2783` | Read-only. |
| ANYtk3D | `0f49efc53670c601bbabc012d856cc8ca18dcc9b` on `main`; dirty README/pyproject plus untracked `.idea/`, selection module, and test fixture | Read-only unless leased evidence and a separate owner plan prove a blocker. |

No base SHA above implies that active, dirty, or separate-worktree delivery is
complete.

## 3. Frozen package constraints

### 3.1 Lightweight core

| Distribution / extra | Target | Frozen requirements | Status |
| --- | --- | --- | --- |
| `ANYfileio` | `0.2.0` | `numpy>=1.26` | `FROZEN` |
| `ANYfileio[semantics]` | `0.2.x` | `ANYmesher>=0.2,<0.3`; `ANYmaterial>=0.1,<0.2` | Family ranges `FROZEN`; delivered mesher patch floor `BLOCKED` |

The base runtime is NumPy-only. Existing SESAM/CalculiX semantic paths load
ANYmesher and ANYmaterial only when those operations execute. The base must not
depend on ANYgeometry, ANYmesher, ANYmaterial, OCP, CadQuery, or
ANYfileio-occt.

The CAD semantic-operation acceptance range is exactly
`ANYmesher>=0.2,<0.3`. CAD functionality checks that range at the semantic
operation boundary and never infers compatibility from a broader
package-install range.

The accepted M2 `pyproject.toml`, `README.md`, and packaging-test hunks remain
byte-identical in this direct-child docs commit. A future, disjoint resolver
owner may propose changing canonical package-install metadata from
`ANYmesher>=0.1,<0.2` to exactly `ANYmesher>=0.1,<0.3`, but only after real
hash-pinned ANYmesher 0.1.0 and 0.2.1 wheel compatibility plus a clean combined
resolver gate. That range is transitional legacy-install metadata only: it
does not authorize CAD semantic use of 0.1.x, cannot count as CAD capability,
and cannot merge by itself into the CAD 0.2 line.

The final CAD base requires a separately registered canonical ANYfileIO
metadata/runtime owner to move both `ANYmesher>=0.2,<0.3` and
`ANYmaterial>=0.1,<0.2` out of base requirements and into the `semantics`
extra. That same owner eliminates eager base imports. Base-wheel import, core
CAD records, format discovery, and OCP-free artifact reopen must work with
neither semantic package installed. A semantic entry point lazy-loads the two
packages only when invoked, checks the CAD-accepted mesher range, and otherwise
fails with a typed missing-extra/incompatible-version diagnostic without
breaking core operations.

`pyproject.toml`, `tests/test_packaging.py`, the publish workflow, `README.md`,
and `CHANGELOG.md` are reserved to that separately registered resolver owner.
CAD core/provider plans neither edit those files/hunks nor depend on the
resolver proposal landing. After any resolver commit, the Boss must register an
explicit final merge/rebase order and receive protected-M2 plus CAD-contract
diff proof before integration; no order is inferred here. The owner must record
exact base-only and `[semantics]` resolver cells, focused eager-import/missing-
extra regressions, version synchronization, and a separate publication gate.
This document grants no metadata, version, workflow, or publication authority.

### 3.2 Heavy provider and geometry adapter

| Distribution / extra | Target | Frozen requirements | Status |
| --- | --- | --- | --- |
| `ANYfileio-occt` | `0.1.0` | `ANYfileio>=0.2,<0.3`; `numpy>=1.26`; `cadquery-ocp-novtk>=7.9.3.1.1,<7.10` | `FROZEN` |
| `ANYfileio-occt[geometry]` | `0.1.x` | base requirements plus `ANYgeometry>=0.2.1,<0.3` | `FROZEN` |

Python metadata for the heavy distribution is `>=3.11,<3.15`. It has no
dependency on `cadquery`, `cadquery-ocp`, VTK, ANYfem, ANYsolver, or
ANYstructure. The geometry extra is unnecessary for imported-CAD operations.

### 3.3 Consumers

| Consumer | Target dependency family | Status / owner gate |
| --- | --- | --- |
| ANYfem base | `ANYgeometry[planar]>=0.2.1,<0.3`; `ANYmesher>=0.2,<0.3`; `ANYfileio>=0.2,<0.3`; `ANYsolver=<owner-delivered range>` | Geometry/file families `FROZEN`; mesher patch and solver range `BLOCKED` |
| `ANYfem[cad]` | `ANYfileio-occt[geometry]>=0.1,<0.2` | `FROZEN`; implementation waits for accepted V6/native/UI handoff, then project format V7 |
| ANYstructure | `ANYgeometry>=0.2.1,<0.3`; `ANYmesher>=0.2,<0.3`; `ANYfileio>=0.2,<0.3`; `ANYsolver=<owner-delivered range>` | Final patch floors/range `BLOCKED`; no heavy dependency permitted |

ANYstructure never installs ANYfileio-occt, OCP, CadQuery, or OCCT binaries.
ANYfem CAD persistence supports migration from formats 1 through 6 and writes
format 7; embedded geometry is a complete schema-4 payload/checksum produced by
ANYgeometry public codecs.

Default ANYfem, without `[cad]`, must load V7 and display a valid cached CAD
manifest/preview through the NumPy-only core codec. `[cad]` adds only live CAD
import/export/translation and structural-export capabilities. Imported CAD is
always a reference asset: it is excluded from meshing, loads, constraints,
assembly participation, solving, result ownership, and solver serialization
unless a future separately reviewed conversion contract creates real analysis
geometry.

### 3.4 Blocked owner values

The following are deliberately not represented by invented pins:

1. The accepted ANYmesher 0.2.x delivery commit, artifact/index strategy,
   selected native/default status, and exact lower patch floor.
2. The accepted ANYfem V6/native-selector and UI/settings commits, including
   plate-ownership behavior and direct/incremental selector parity evidence.
3. The solver-compatible dependency range and returned commit/wheel from a
   separately registered solver-owner plan coordinating native-hybrid and S4.
4. Final ANYstructure administrative/dependency hunks from its repository
   owner after upstream contracts are real.
5. External/CRS structural export. Protocol 1 emits only `model_local` and
   returns typed `BLOCKED` / `geometry.external_transform_undefined` for every
   external request until an ANYgeometry-owner directional composition helper
   or formula and its regressions are accepted.
6. Structural-export read synchronization. ANYgeometry 0.2.1 has no public read
   lock/snapshot. The ANYfem/application owner must deliver an accepted
   `GeometryReadLease` implementation that starts committed/transaction-idle,
   is bound by object identity to the exact `GeometryModel` argument as well as
   its model id/revision, and excludes topology, structural, feature, settings,
   restore/undo, and deserialization mutation through capture, audit, and
   optional checksum. A lease over a different loaded object with the same UUID
   and revision is invalid. Until then structural export is `BLOCKED` with
   `geometry.read_lease_required`; imported-CAD operations remain independent.

Current dirty files and base commits are observations only. Updating a family
range with an owner-delivered patch floor requires an approved contract review,
not an opportunistic metadata edit.

Items 5 and 6 are Boss-approved fail-closed supersessions of the source plan's
provisional external-transform and no-lock/double-revision language. They do
not grant CAD write scope in ANYgeometry. A future immutable committed snapshot
or geometry-owner API substitutes only through a reviewed contract revision.

## 4. OCP distribution freeze

The raw VTK-free binding is the only selected OCCT Python binding:

```text
PyPI distribution:             cadquery-ocp-novtk
Python import namespace:       OCP
allowed metadata range:        >=7.9.3.1.1,<7.10
qualification pin:             ==7.9.3.1.1
Python requirement observed:   >=3.10,<3.15
source distribution:           none observed
source-build fallback:         forbidden
cadquery-ocp co-install:       forbidden (same OCP namespace)
cadquery co-install:           forbidden (brings/conflicts with OCP namespace)
VTK-enabled variant:           forbidden
```

Metadata source:
`https://pypi.org/project/cadquery-ocp-novtk/`, observed 2026-08-12. The 7.9
minor boundary is intentional. Widening to another minor requires an API/ABI,
backend-compatibility, cache, and protocol review.

The upstream metadata includes CPython 3.10, but the ANY ecosystem heavy target
is only 3.11 through 3.14. Release-file hashes have not yet been captured and
are `UNRUN`; qualification must revalidate the index and record exact wheel
filename, version, size, and SHA-256 before installation.

The provider validates distribution metadata with `importlib.metadata` before
the first `OCP` import. Presence of distributions `cadquery-ocp` or `cadquery`
puts the backend in `incompatible` state with code
`cad.backend.ocp_namespace_conflict`; it does not attempt import-order
selection. The selected `cadquery-ocp-novtk` version must be a dot-separated
numeric release, compared as a zero-padded integer tuple against
`(7,9,3,1,1) <= version < (7,10)`. Pre/dev/local versions are rejected. This
requires no undeclared `packaging` runtime dependency. Missing/duplicate
distribution metadata or a version outside the range fails before OCP import.

## 5. Python and platform wheel matrix

Each listed platform has an upstream wheel in the observed metadata. That fact
is not a qualification pass.

| CPython | Windows amd64 | Linux x86_64 | Linux aarch64 | macOS x86_64 | macOS arm64 |
| --- | --- | --- | --- | --- | --- |
| 3.11 | `win_amd64` / `UNRUN` | `manylinux_2_31_x86_64` / `UNRUN` | `manylinux_2_31_aarch64` / `UNRUN` | `macosx_11_0_x86_64` / `UNRUN` | `macosx_11_0_arm64` / `UNRUN` |
| 3.12 | `win_amd64` / `UNRUN` | `manylinux_2_31_x86_64` / `UNRUN` | `manylinux_2_31_aarch64` / `UNRUN` | `macosx_11_0_x86_64` / `UNRUN` | `macosx_11_0_arm64` / `UNRUN` |
| 3.13 | `win_amd64` / `UNRUN` | `manylinux_2_31_x86_64` / `UNRUN` | `manylinux_2_31_aarch64` / `UNRUN` | `macosx_11_0_x86_64` / `UNRUN` | `macosx_11_0_arm64` / `UNRUN` |
| 3.14 | `win_amd64` / `UNRUN` | `manylinux_2_31_x86_64` / `UNRUN` | `manylinux_2_31_aarch64` / `UNRUN` | `macosx_11_0_x86_64` / `UNRUN` | `macosx_11_0_arm64` / `UNRUN` |

Explicit heavy-backend `UNSUPPORTED` cells:

- Python below 3.11 or above 3.14, including upstream CPython 3.10;
- PyPy or another Python implementation;
- Windows ARM64 or any 32-bit platform;
- musllinux;
- Linux with glibc older than 2.31;
- macOS older than 11;
- any OS/architecture/Python cell without the exact upstream wheel;
- source compilation as a fallback.

The lightweight ANYfileIO wheel remains platform independent. A Windows pass
does not imply a Linux or macOS pass; remote CI evidence is recorded per cell.

## 6. Entry point and protocol metadata

```text
entry-point group:              anyfileio.backends
entry-point name:               occt
entry-point target:             anyfileio_occt.backend:get_backend
CAD backend protocol version:   1
backend id:                     occt
backend compatibility version: 1
preview artifact schema:        anyfileio.cad-preview / 1
preview artifact codec:         anyfileio.cad_artifact (NumPy-only core)
```

Entry-point enumeration reads metadata only. `get_backend()` is imported and
called only for a requested heavy operation. The returned provider must declare
all three exact id/version values plus normalized capabilities before use.
Missing, duplicate, broken, or unequal values fail closed. See
[`docs/CAD_BACKEND_CONTRACT.md`](docs/CAD_BACKEND_CONTRACT.md) for state and
error semantics.

## 7. Wheel content allow/deny lists

### 7.1 ANYfileio

Allowed:

- pure-Python `anyfileio/**` modules and declared package data;
- the OCP-free `anyfileio.cad_artifact` schema-1 reader/writer;
- distribution metadata, type marker, license, and required notices.

Denied:

- `OCP`, OCCT DLL/SO/DYLIB/framework files, CadQuery, VTK, or
  `anyfileio_occt`;
- copied ANYgeometry, ANYmesher, or ANYmaterial package trees;
- tests, benchmark corpus, source CAD, generated previews, caches, and reports.

### 7.2 ANYfileio-occt

Allowed:

- pure-Python `anyfileio_occt/**`, its type marker, entry-point metadata,
  distribution `METADATA`, `WHEEL`, `RECORD`, license, and
  `THIRD_PARTY_NOTICES.md`.

Denied:

- vendored OCP binaries or wheel contents, CadQuery, VTK, and another binding;
- ANYgeometry code, ANYfem/project code, tests, benchmarks, source CAD,
  generated artifacts, caches, or reports.

The OCP wheel remains a separately installed dependency. Wheel `RECORD` and
installed-distribution inspection are required evidence and currently `UNRUN`.
No copy of the repository contract documents is required package data in
version 0.1; the frozen protocol values live in code, distribution metadata,
entry-point metadata, and the release evidence packet. The only package-data
file under `anyfileio_occt` is `py.typed` unless a later registered plan revises
this allowlist.

## 8. Import-isolation contract

From built, cleanly installed wheels, the following must hold:

- `import anyfileio` loads none of `OCP`, `cadquery`, `anyfileio_occt`,
  `anygeometry`, `anymesher`, or `anymaterial`;
- known-format lookup, backend metadata/status lookup, and built-in operations
  preserve that isolation;
- `import anyfileio_occt` and entry-point enumeration load neither `OCP` nor
  ANYgeometry;
- provider construction imports no OCP until a heavy CAD operation begins;
- structural export alone imports ANYgeometry;
- cached manifest/preview reopen through `anyfileio.cad_artifact` and
  preserve-mode byte copying do not import OCP or require ANYfileio-occt;
- `import anyfem` and `import anystruct` load none of OCP, CadQuery, or
  ANYfileio-occt.

Source-tree module snapshots are useful focused checks but do not replace the
built-wheel isolation gate.

## 9. Resolver and artifact matrix

| Environment | Required result | Status |
| --- | --- | --- |
| ANYfileio base only | NumPy plus core; neither ANYmesher nor ANYmaterial installed; import/core CAD records/artifact reopen pass | `BLOCKED` on canonical metadata/runtime owner, then `UNRUN` |
| ANYfileio semantics | accepted `ANYmesher>=0.2,<0.3` + `ANYmaterial>=0.1,<0.2`; lazy semantic operation succeeds | `BLOCKED` on mesher and metadata/runtime handoffs, then `UNRUN` |
| Transitional legacy resolver | real hash-pinned ANYmesher 0.1.0 and 0.2.1 compatibility for proposed install-only `>=0.1,<0.3`; never a CAD-capability pass and never merged alone | `BLOCKED` on separate resolver-owner plan/evidence |
| ANYfileio-occt base | core + NumPy + exact OCP wheel; no geometry | `UNRUN` |
| ANYfileio-occt geometry | heavy base + ANYgeometry 0.2.1; no consumer | `UNRUN` |
| ANYfem default | geometry/mesher/core/solver, no OCP/provider | `BLOCKED` on owner handoffs, then `UNRUN` |
| ANYfem default V7 offline | load/migrate V1-V6, load/save V7 reference assets, and display cached manifest/arrays through `anyfileio.cad_artifact` with no OCP/provider/heavy extra; CAD assets never enter mesh/solve | `BLOCKED` on accepted V6/UI plus V7 owner implementation, then `UNRUN` |
| ANYfem CAD extra | default + heavy geometry extra for live import/export/translation and structural export; project format remains V7 | `BLOCKED` on imported-heavy API, owner lease, and structural-adapter handoffs, then `UNRUN` |
| ANYstructure | lightweight graph only; no heavy transitive dependency | `BLOCKED` on final owner ranges, then `UNRUN` |

For every supported Python/platform cell, qualification records resolver
output, installed versions, wheel filenames/hashes, dependency tree, module
snapshot, package sizes, and first failure. No retry, pin tuning, or source
fallback is implied by a failure.

## 10. Build, test, and performance evidence

All of the following are currently `UNRUN` for the CAD implementation:

- full repository or cross-repository suites;
- native/OCP correctness suites and qualification builds;
- `python -m build`, wheel/sdist creation, `twine check`, and `RECORD` review;
- clean virtual-environment installs and resolver/Python/platform matrices;
- cold/warm import time, peak RSS, wheel/installed/executable size;
- CAD import, XDE traversal, tessellation, copy count, cache reopen,
  instancing, structural export, translation, profiling, stress, and scaling;
- large/generated fixture and throughput runs.

They require the exclusive ecosystem performance lease. The exact protocol is:

1. send `PERF LEASE REQUEST` naming exact commands, worktree/commit, CPU/GPU,
   RAM/disk resources, output path, and ETA;
2. wait for the complete token `PERF LEASE GRANTED`;
3. preserve the first observed result; a grant does not imply retry/tuning;
4. send `PERF LEASE RELEASED` with outcome and process state after the first
   observed outcome, ensuring no run process remains.

An idle machine is not a grant. Only one task may hold the lease. Focused
low-cost tests may run without it only after the Boss classifies the exact
command as non-lease-sensitive.

## 11. Acceptance and publication order

The dependency/qualification edges are:

```text
registered contract freeze
  -> registered/implemented CAD-neutral ANYfileIO types/discovery
  -> accepted core public-API commit
  -> registered/implemented imported-CAD ANYfileio-occt slices
  -> accepted imported-CAD heavy API (no structural lease dependency)

future disjoint resolver evidence/owner commit (if accepted)
  -> explicit registered merge/rebase order
  -> protected M2 + CAD-contract diff proof
  -> integration without weakening CAD >=0.2 semantic checks

accepted ANYmesher 0.2.x owner handoff
  -> exact semantics-extra floor and lazy semantic integration
  -> semantic resolver qualification

accepted core public API + accepted semantic integration
  -> complete ANYfileIO 0.2 qualification

accepted imported-CAD heavy API + accepted ANYfem V6/native/UI handoff
  -> ANYfem owner V7 persistence/headless reference assets
  -> default-install V7 cached-preview/offline gate (no heavy provider)
  -> accepted exact-object GeometryReadLease implementation/proof
  -> structural geometry-adapter integration
  -> ANYfem CAD scene/UI (reference-only; excluded from mesh/solve)
  -> solver-owner dependency return and consumer isolation
  -> leased wheel/resolver/correctness gates
  -> leased performance gates
  -> evidence reports and ecosystem Boss completion review
```

ANYgeometry 0.2.1 is already delivered and read-only. This CAD work does not
create ANYmesher 0.1.1, modify ANYmesh, or create an ANYsolver compatibility
release. A completion review is not publication authority. Push, package-index
upload, tag, release, and publication remain separately authorized future
actions after compatible artifacts and observed gates exist.

The ANYmesher handoff gates dependency-floor and semantic/resolver work; it is
not a prerequisite for disjoint provider code that consumes only the frozen
core CAD API. Exact implementation plans still name their real upstream commit
and may impose a narrower registered merge edge. No diagram here silently
authorizes an edit or strengthens a Boss-registered gate.
