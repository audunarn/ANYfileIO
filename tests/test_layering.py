"""The package must not import its own consumers.

ANYsolver consumes files read here; ANYfileio does not consume solvers.  If that
arrow ever reverses the family's dependency graph becomes a cycle, and the
packages can no longer be released independently.  The rule is cheap to state
and easy to break by accident -- an import added while chasing a discrepancy
between a parsed document and a built model is exactly how it would happen --
so it is checked rather than documented.

NumPy is the only unconditional third-party dependency.  The semantic mesh and
material packages sit below this package but are loaded only through the private
optional-runtime gate.  ANYsolver and ANYfem sit above it and stay forbidden.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE = "anyfileio"
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / PACKAGE

# Distributions this package may import unconditionally.  Keep in step with the
# `dependencies` list in pyproject.toml -- test_packaging.py checks that.
ALLOWED_THIRD_PARTY = frozenset({"numpy"})
SEMANTIC_IMPORTS = frozenset({"anymesher", "anymaterial"})
SEMANTIC_TYPE_CHECKING_IMPORTS = {
    "calculix/deck.py": frozenset({"anymesher"}),
    "sesam/semantics.py": frozenset({"anymaterial", "anymesher"}),
}

# Importing any of these would invert the dependency direction.
FORBIDDEN = frozenset(
    {
        "anysolver",
        "anyfem",
        "anystructure",
        "anystruct",
    }
)

# Modules allowed one extra module-level import, because the feature is optional
# and the module is only reached when the corresponding extra is installed.
# Anything not listed here must be imported inside the function that needs it,
# so that importing the package never requires an optional dependency.
OPTIONAL_IMPORT_EXCEPTIONS: dict[str, frozenset[str]] = {}

CAD_NEUTRAL_MODULES = ("cad.py", "cad_backend.py", "cad_operations.py", "formats.py")
CAD_FORBIDDEN_IMPORTS = frozenset({"OCP", "cadquery", "anyfileio_occt", "anygeometry"})


def _modules() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(SOURCE_ROOT).as_posix()


def _is_type_checking_guard(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "TYPE_CHECKING"


def _import_sites(path: Path) -> list[tuple[str, bool]]:
    """Return absolute import names and whether they are TYPE_CHECKING-only."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sites: list[tuple[str, bool]] = []

    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.type_checking_depth = 0

        def visit_If(self, node: ast.If) -> None:
            if _is_type_checking_guard(node.test):
                self.type_checking_depth += 1
                for child in node.body:
                    self.visit(child)
                self.type_checking_depth -= 1
                for child in node.orelse:
                    self.visit(child)
                return
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                sites.append((alias.name.split(".")[0], self.type_checking_depth > 0))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if not node.level and node.module:
                sites.append((node.module.split(".")[0], self.type_checking_depth > 0))

    Collector().visit(tree)
    return sites


def _top_level_imports(path: Path) -> set[str]:
    """Every runtime distribution import, ignoring relative/type-only imports."""

    return {name for name, type_only in _import_sites(path) if not type_only}


def _all_imports(path: Path) -> set[str]:
    return {name for name, _type_only in _import_sites(path)}


def test_source_tree_is_importable_layout() -> None:
    assert SOURCE_ROOT.is_dir(), f"missing source tree at {SOURCE_ROOT}"
    assert (SOURCE_ROOT / "__init__.py").is_file()
    assert _modules(), "no modules found to check"


def test_no_module_imports_a_consumer() -> None:
    offenders = {}
    for path in _modules():
        forbidden = sorted(_all_imports(path) & FORBIDDEN)
        if forbidden:
            offenders[_relative(path)] = forbidden
    assert not offenders, (
        "ANYfileio must not import its consumers, or the dependency graph "
        f"becomes a cycle: {offenders}"
    )


def test_third_party_imports_are_declared() -> None:
    offenders = {}
    for path in _modules():
        allowed = ALLOWED_THIRD_PARTY | OPTIONAL_IMPORT_EXCEPTIONS.get(_relative(path), frozenset())
        undeclared = sorted(
            _top_level_imports(path) - sys.stdlib_module_names - {PACKAGE} - allowed
        )
        if undeclared:
            offenders[_relative(path)] = undeclared
    assert not offenders, (
        "these imports are neither standard library nor declared dependencies; "
        f"add them to pyproject.toml and ALLOWED_THIRD_PARTY, or import them lazily: {offenders}"
    )


def test_base_third_party_allowlist_is_numpy_only() -> None:
    assert ALLOWED_THIRD_PARTY == frozenset({"numpy"})


def test_semantic_imports_are_type_checking_or_loader_owned() -> None:
    found: dict[str, set[str]] = {}
    offenders: dict[str, list[str]] = {}
    for path in _modules():
        relative = _relative(path)
        for name, type_only in _import_sites(path):
            if name not in SEMANTIC_IMPORTS:
                continue
            found.setdefault(relative, set()).add(name)
            if not type_only or name not in SEMANTIC_TYPE_CHECKING_IMPORTS.get(relative, ()):
                offenders.setdefault(relative, []).append(name)
    assert not offenders, f"optional semantics packages are imported at runtime: {offenders}"
    assert {path: frozenset(names) for path, names in found.items()} == (
        SEMANTIC_TYPE_CHECKING_IMPORTS
    )
    loader = (SOURCE_ROOT / "_semantic_dependencies.py").read_text(encoding="utf-8")
    assert "import_module(module_name)" in loader


def test_cad_neutral_modules_do_not_import_heavy_or_geometry_packages() -> None:
    offenders = {}
    for relative in CAD_NEUTRAL_MODULES:
        path = SOURCE_ROOT / relative
        forbidden = sorted(_top_level_imports(path) & CAD_FORBIDDEN_IMPORTS)
        if forbidden:
            offenders[relative] = forbidden
    assert not offenders, f"CAD-neutral modules import optional heavy/geometry packages: {offenders}"
