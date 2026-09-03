# Third-party notices

ANYfileio 0.3.0 has one direct runtime dependency:

| Dependency | Declared version | Upstream | License | Bundled |
| --- | --- | --- | --- | --- |
| NumPy | `numpy>=1.26` | https://numpy.org/ | BSD-3-Clause | No |

NumPy is installed separately by the Python package installer. Its source or
object code is not included in the ANYfileio wheel or source distribution.
NumPy retains its own copyright and license terms:
https://github.com/numpy/numpy/blob/main/LICENSE.txt

The machine-readable release inventory is in `dependency-licenses.json`.

The `dev` extra contains development and release tools (`build`, `pytest`, and
`twine`). They are not runtime dependencies and are not bundled in ANYfileio
distributions. Their licenses remain those supplied by their respective
projects.

The optional source-development semantic owners (ANYgeometry, ANYmesher, and
ANYmaterial) and the deferred native provider (ANYfileio-occt / OCP) are not
runtime dependencies and are not bundled in ANYfileio 0.3.0.
