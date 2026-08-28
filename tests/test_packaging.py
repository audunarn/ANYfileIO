"""Packaging metadata has to agree with the code it describes.

A version that drifts from pyproject.toml is invisible until a release is cut
with the wrong number on it, and an allowlist that drifts from the declared
dependencies turns the layering check into decoration.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

import anyfileio
from test_layering import ALLOWED_THIRD_PARTY, OPTIONAL_IMPORT_EXCEPTIONS, SEMANTIC_IMPORTS

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+")

VERIFIER = REPOSITORY_ROOT / "tools" / "verify_release_authority.py"
DISTRIBUTION = "ANYfileio"
NORMALIZED = "anyfileio"
VERSION = "0.2.1"
TAG = f"v{VERSION}"
EXPECTED_TERMINAL = "ACCEPTED_ANYFILEIO_0_2_1_RELEASE"
WRONG_TAG = "v0.2.0"
WHEEL = f"{NORMALIZED}-{VERSION}-py3-none-any.whl"
SDIST = f"{NORMALIZED}-{VERSION}.tar.gz"
LEDGER = Path("docs/release") / f"{NORMALIZED}-{VERSION}-ledger.json"
CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_ACTION = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
PUBLISH_ACTION = (
    "pypa/gh-action-pypi-publish@"
    "dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Release Authority Test",
            "-c",
            "user.email=release-authority@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _metadata(distribution: str = DISTRIBUTION) -> bytes:
    return (
        "Metadata-Version: 2.1\n"
        f"Name: {distribution}\n"
        f"Version: {VERSION}\n\n"
    ).encode("utf-8")


def _write_wheel(
    path: Path,
    payload: bytes,
    *,
    distribution: str = DISTRIBUTION,
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{NORMALIZED}/__init__.py", payload)
        archive.writestr(
            f"{NORMALIZED}-{VERSION}.dist-info/METADATA",
            _metadata(distribution),
        )


def _write_sdist(path: Path) -> None:
    info = tarfile.TarInfo(f"{NORMALIZED}-{VERSION}/PKG-INFO")
    metadata = _metadata()
    info.size = len(metadata)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(metadata))


def _write_checksums(assets: Path) -> None:
    text = "".join(
        f"{hashlib.sha256((assets / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted((WHEEL, SDIST))
    )
    (assets / "SHA256SUMS").write_text(text, encoding="ascii", newline="\n")


def _run_verifier(tmp_path: Path, mutation: str = "") -> subprocess.CompletedProcess[str]:
    repository = tmp_path / "repository"
    remote = tmp_path / "origin.git"
    assets = tmp_path / "release-assets"
    repository.mkdir(parents=True)
    remote.mkdir()
    assets.mkdir()
    _git(repository, "init", "--quiet")
    _git(remote, "init", "--bare", "--quiet")
    (repository / "source.txt").write_text("frozen artifact source\n", encoding="utf-8")
    source_paths = ["source.txt"]
    if mutation == "textconv-diff-driver":
        (repository / ".gitattributes").write_text(
            "* diff=release-bypass\n",
            encoding="utf-8",
        )
        source_paths.append(".gitattributes")
    _git(repository, "add", *source_paths)
    _git(repository, "commit", "--quiet", "-m", "freeze artifact source")
    source_commit = _git(repository, "rev-parse", "HEAD")
    source_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    _git(repository, "branch", "-M", "main")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "--quiet", "-u", "origin", "main")

    attribute_source_commit = ""
    if mutation == "git-attr-source":
        _git(repository, "checkout", "--quiet", "-b", "attack-attributes")
        (repository / ".gitattributes").write_text(
            "* diff=release-bypass\n",
            encoding="utf-8",
        )
        _git(repository, "add", ".gitattributes")
        _git(repository, "commit", "--quiet", "-m", "attacker attributes")
        attribute_source_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "checkout", "--quiet", "main")

    _write_wheel(assets / WHEEL, b"accepted build\n")
    if mutation == "wrong-metadata":
        _write_wheel(
            assets / WHEEL,
            b"accepted build\n",
            distribution="DifferentDistribution",
        )
    _write_sdist(assets / SDIST)
    artifact_rows = []
    for name in sorted((WHEEL, SDIST)):
        raw = (assets / name).read_bytes()
        artifact_rows.append(
            {
                "bytes": len(raw),
                "filename": name,
                "sha256": hashlib.sha256(raw).hexdigest().upper(),
            }
        )
    ledger = {
        "artifact_source": {"commit": source_commit, "tree": source_tree},
        "artifacts": artifact_rows,
        "distribution": DISTRIBUTION,
        "publication_authorized": True,
        "qualification": {
            "accepted_terminal": EXPECTED_TERMINAL,
            "evidence_sha256": "A" * 64,
            "independent_review_sha256": "B" * 64,
        },
        "schema": "anyecosystem.release-ledger-v1",
        "tag": TAG,
        "version": VERSION,
    }
    if mutation == "wrong-byte-count":
        ledger["artifacts"][0]["bytes"] += 1
    elif mutation == "wrong-terminal":
        ledger["qualification"]["accepted_terminal"] = "REJECTED_RELEASE"
    elif mutation == "evidence-hash":
        ledger["qualification"]["evidence_sha256"] = "0" * 64
    elif mutation == "review-hash":
        ledger["qualification"]["independent_review_sha256"] = "A" * 64
    elif mutation == "noncanonical-tag-ref":
        ledger["tag"] = f"{TAG}^{{commit}}"
    if mutation == "wrong-source":
        ledger["artifact_source"]["tree"] = "0" * 40

    target = repository / LEDGER
    target.parent.mkdir(parents=True)
    if mutation == "noncanonical":
        target.write_text(json.dumps(ledger), encoding="utf-8")
    else:
        target.write_text(
            json.dumps(ledger, allow_nan=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    _git(repository, "add", LEDGER.as_posix())
    if mutation == "extra-child-path":
        (repository / "unexpected.txt").write_text("not ledger-only\n", encoding="utf-8")
        _git(repository, "add", "unexpected.txt")
    _git(repository, "commit", "--quiet", "-m", "docs: authorize release artifacts")
    _git(repository, "tag", TAG)
    if mutation != "unmerged-tag-child":
        _git(repository, "push", "--quiet", "origin", "HEAD:main")

    git_directory = Path(_git(repository, "rev-parse", "--git-dir"))
    if not git_directory.is_absolute():
        git_directory = repository / git_directory
    git_info = git_directory / "info"
    git_info.mkdir(exist_ok=True)
    if mutation == "moved-tag-ref":
        _git(repository, "tag", "--force", TAG, source_commit)
    elif mutation == "missing-tag-ref":
        _git(repository, "tag", "--delete", TAG)
    elif mutation == "replacement-ref":
        _git(
            repository,
            "replace",
            source_commit,
            _git(repository, "rev-parse", "HEAD"),
        )
    elif mutation == "graft-file":
        (git_info / "grafts").write_text(
            _git(repository, "rev-parse", "HEAD") + "\n",
            encoding="ascii",
        )
    elif mutation == "info-attributes":
        (git_info / "attributes").write_text(
            "* diff=release-bypass\n",
            encoding="utf-8",
        )

    _write_checksums(assets)
    invoked_tag = (
        f"{TAG}^{{commit}}"
        if mutation == "noncanonical-tag-ref"
        else TAG
    )
    verifier_environment = os.environ.copy()
    attacker_marker = tmp_path / "attacker.marker"
    attacker = tmp_path / "attacker.py"
    attacker.write_text(
        "from pathlib import Path\n"
        f"Path({str(attacker_marker)!r}).write_text("
        "'invoked\\n', encoding='utf-8')\n"
        "raise SystemExit(97)\n",
        encoding="utf-8",
    )
    attacker_command = shlex.join((sys.executable, str(attacker)))
    external_attributes = tmp_path / "external.attributes"
    external_attributes.write_text(
        "* diff=release-bypass\n",
        encoding="utf-8",
    )
    external_config = tmp_path / "external.gitconfig"
    external_config.write_text("", encoding="utf-8")
    _git(
        repository,
        "config",
        "--file",
        str(external_config),
        "core.attributesFile",
        str(external_attributes),
    )
    _git(
        repository,
        "config",
        "--file",
        str(external_config),
        "diff.external",
        attacker_command,
    )
    _git(
        repository,
        "config",
        "--file",
        str(external_config),
        "diff.release-bypass.textconv",
        attacker_command,
    )
    assert (
        _git(
            repository,
            "config",
            "--file",
            str(external_config),
            "--get",
            "diff.external",
        )
        == attacker_command
    )
    if mutation == "global-attributes-config":
        verifier_environment["GIT_CONFIG_GLOBAL"] = str(external_config)
    elif mutation == "system-attributes-config":
        verifier_environment["GIT_CONFIG_SYSTEM"] = str(external_config)
    elif mutation == "core-attributes-config":
        _git(
            repository,
            "config",
            "core.attributesFile",
            str(external_attributes),
        )
        _git(
            repository,
            "config",
            "diff.release-bypass.textconv",
            attacker_command,
        )
    elif mutation == "environment-external-diff":
        verifier_environment["GIT_EXTERNAL_DIFF"] = attacker_command
    elif mutation == "local-external-diff":
        _git(repository, "config", "diff.external", attacker_command)
    elif mutation == "textconv-diff-driver":
        _git(
            repository,
            "config",
            "diff.release-bypass.textconv",
            attacker_command,
        )
    elif mutation == "git-attr-source":
        verifier_environment["GIT_ATTR_SOURCE"] = attribute_source_commit
        _git(
            repository,
            "config",
            "diff.release-bypass.textconv",
            attacker_command,
        )
    if mutation == "paired-replacement":
        _write_wheel(assets / WHEEL, b"replacement build\n")
        _write_checksums(assets)
    elif mutation == "checksum":
        (assets / "SHA256SUMS").write_text(
            "0" * 64 + f"  {WHEEL}\n"
            + hashlib.sha256((assets / SDIST).read_bytes()).hexdigest()
            + f"  {SDIST}\n",
            encoding="ascii",
            newline="\n",
        )
    elif mutation == "extra-asset":
        (assets / "unregistered.txt").write_text("extra\n", encoding="utf-8")
    elif mutation == "tag":
        invoked_tag = WRONG_TAG

    return subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            "--repository-root",
            str(repository),
            "--ledger",
            LEDGER.as_posix(),
            "--assets",
            str(assets),
            "--output",
            str(tmp_path / "dist"),
            "--tag",
            invoked_tag,
            "--protected-ref",
            "refs/remotes/origin/main",
            "--expected-terminal",
            EXPECTED_TERMINAL,
            "--distribution",
            DISTRIBUTION,
            "--version",
            VERSION,
            "--artifact",
            WHEEL,
            "--artifact",
            SDIST,
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        env=verifier_environment,
    )


def _pyproject() -> dict:
    return tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirement_names(requirements: list[str] | tuple[str, ...]) -> set[str]:
    names = set()
    for requirement in requirements:
        match = _REQUIREMENT_NAME.match(requirement.strip())
        if match:
            names.add(match.group(0).lower().replace("_", "-"))
    return names


def _base_dependencies() -> tuple[str, ...]:
    return tuple(_pyproject()["project"].get("dependencies", ()))


def _extra_dependencies(name: str) -> tuple[str, ...]:
    extras = _pyproject()["project"].get("optional-dependencies", {})
    return tuple(extras.get(name, ()))


def _declared_dependencies() -> set[str]:
    project = _pyproject()["project"]
    requirements = list(project.get("dependencies", ()))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    return _requirement_names(requirements)


def _repository_text(relative: str) -> str:
    return (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")


def _workflow_jobs() -> dict[str, str]:
    text = _repository_text(".github/workflows/ci.yml")
    headings = list(re.finditer(r"(?m)^  ([a-z][a-z0-9-]*):\n", text))
    return {
        heading.group(1): text[
            heading.start() : headings[index + 1].start() if index + 1 < len(headings) else None
        ]
        for index, heading in enumerate(headings)
    }


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def _importorskip_modules(function: ast.FunctionDef) -> set[str]:
    modules = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != "pytest" or node.func.attr != "importorskip":
            continue
        if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            modules.add(node.args[0].value)
    return modules


def test_version_matches_pyproject() -> None:
    assert anyfileio.__version__ == _pyproject()["project"]["version"]


def test_base_dependencies_are_numpy_only() -> None:
    assert _base_dependencies() == ("numpy>=1.26",)


def test_release_advertises_the_qualified_semantics_runtime() -> None:
    extras = _pyproject()["project"]["optional-dependencies"]
    assert set(extras) == {"gui", "dev", "semantics"}
    assert _extra_dependencies("semantics") == (
        "ANYmesher>=0.3.2,<0.4",
        "ANYmaterial>=0.1.1,<0.2",
    )


def test_distribution_name_does_not_collide_with_the_async_library() -> None:
    # The repository is ANYfileIO.  `anyio` on PyPI is the async compatibility
    # library, and an import package by that name would shadow it for every
    # environment that has httpx or starlette installed.  Asserted rather than
    # assumed, because a rename back to the repository name would be an easy
    # tidy-up to make and a very hard breakage to diagnose.
    assert _pyproject()["project"]["name"] == "ANYfileio"
    assert (REPOSITORY_ROOT / "src" / "anyfileio").is_dir()
    assert not (REPOSITORY_ROOT / "src" / "anyio").exists()


def test_project_urls_use_canonical_repository() -> None:
    assert _pyproject()["project"]["urls"] == {
        "Homepage": "https://github.com/audunarn/ANYfileIO",
        "Repository": "https://github.com/audunarn/ANYfileIO",
        "Issues": "https://github.com/audunarn/ANYfileIO/issues",
    }


def test_allowed_third_party_imports_are_declared_dependencies() -> None:
    declared = _declared_dependencies()
    permitted = set(ALLOWED_THIRD_PARTY)
    for extra in OPTIONAL_IMPORT_EXCEPTIONS.values():
        permitted |= set(extra)
    undeclared = sorted(
        name
        for name in permitted - set(SEMANTIC_IMPORTS)
        if name.lower().replace("_", "-") not in declared
    )
    assert not undeclared, (
        "the layering allowlist permits imports that pyproject.toml does not "
        f"install in any extra: {undeclared}"
    )


def test_semantic_runtime_imports_are_declared_by_the_extra() -> None:
    declared = _declared_dependencies()
    expected = {name.lower().replace("_", "-") for name in SEMANTIC_IMPORTS}
    assert declared & expected == expected
    assert expected == {"anymesher", "anymaterial"}


def test_run_gui_bootstraps_without_semantic_sibling_paths() -> None:
    """The IDE Run-button entry point must work in a bare checkout.

    Executed with a run_name other than ``__main__`` so the path bootstrap and
    the import run but the window does not open.
    """

    import runpy

    script = REPOSITORY_ROOT / "run_gui.py"
    assert script.is_file()

    namespace = runpy.run_path(str(script), run_name="not_main")
    assert callable(namespace["main"])
    assert namespace["main"].__module__ == "anyfileio.gui"
    text = script.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":\n    raise SystemExit(main())' in text
    assert 'parent / "ANYmesh"' not in text
    assert 'parent / "ANYmaterial"' not in text


def test_ci_separates_numpy_only_base_from_semantics() -> None:
    workflow = _repository_text(".github/workflows/ci.yml")
    jobs = _workflow_jobs()

    ci_checkout = (
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
    )
    ci_setup = (
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
    )
    uses = re.findall(r"(?m)^\s*- uses: (\S+)", workflow)
    assert uses == [ci_checkout, ci_setup] * 4
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", use) for use in uses)

    assert "pytest" not in jobs
    assert {"base-only", "semantics", "coexists-with-anyio", "wheel"} <= set(jobs)
    assert "SIBLINGS" not in workflow

    base = jobs["base-only"]
    assert 'python -m pip install -e ".[dev]"' in base
    assert "git+https://" not in base
    assert "[dev,semantics]" not in base
    for distribution, module in (
        ("ANYgeometry", "anygeometry"),
        ("ANYmesher", "anymesher"),
        ("ANYmaterial", "anymaterial"),
    ):
        assert f'("{distribution}", "{module}")' in base
    assert "metadata.distribution(distribution)" in base
    assert "util.find_spec(module) is None" in base

    assert "python -m pytest" in base
    assert "python -m pytest" in jobs["semantics"]
    assert "git+https://" not in jobs["coexists-with-anyio"]
    assert "git+https://" not in jobs["wheel"]


def test_semantics_ci_freezes_owner_sources_and_pep610_provenance() -> None:
    semantics = _workflow_jobs()["semantics"]
    installs = (
        'python -m pip install --no-deps "git+https://github.com/audunarn/ANYgeometry.git@6a8b023ef6f65805519c96b56e025b4e3b457a1f"',
        'python -m pip install --no-deps "git+https://github.com/audunarn/ANYmesh.git@e79d14a03ef605afd947948e8588ccb8428eb52f"',
        'python -m pip install --no-deps "git+https://github.com/audunarn/ANYmaterial.git@0591d4833806ee95bdd710c352a1f836af7b910e"',
    )
    positions = [semantics.index(command) for command in installs]
    assert positions == sorted(positions)
    assert 'python -m pip install -e ".[dev]"' in semantics
    assert "[dev,semantics]" not in semantics
    assert "--index" not in semantics
    assert "--extra-index-url" not in semantics

    assert re.findall(r'"(git\+https://[^\"]+)"', semantics) == [
        command.split('"', 1)[1].rsplit('"', 1)[0] for command in installs
    ]
    for version in ("0.4.1", "0.3.2", "0.1.1"):
        assert version in semantics
    assert 'Path(os.environ["GITHUB_WORKSPACE"]).resolve()' in semantics
    assert "Path(sys.prefix).resolve()" in semantics
    assert "not origin.is_relative_to(workspace)" in semantics
    assert "origin.is_relative_to(environment)" in semantics
    assert 'distribution.read_text("direct_url.json")' in semantics
    assert 'set(direct_url) == {"url", "vcs_info"}' in semantics
    assert 'direct_url["url"] == repository' in semantics
    assert 'direct_url["vcs_info"] == {' in semantics
    assert '"requested_revision": commit' in semantics
    assert '"commit_id": commit' in semantics


def test_dependency_matrix_keeps_source_and_wheel_evidence_separate() -> None:
    matrix = _repository_text("DEPENDENCY_MATRIX.md")
    assert "5513881827cdee9fd337497a2730a5912d8ea751" in matrix
    assert "1f0b5780df7f025fc786fd3db2cba9da2104fb5c" in matrix
    for commit in (
            "6a8b023ef6f65805519c96b56e025b4e3b457a1f",
            "e79d14a03ef605afd947948e8588ccb8428eb52f",
            "0591d4833806ee95bdd710c352a1f836af7b910e",
    ):
        assert commit in matrix
    assert "Release candidate; publication `UNRUN`" in matrix
    assert "Coordinated candidate; publication `UNRUN`" in matrix
    assert "publication remains `UNRUN`" in matrix
    assert "These are source-cell inputs, not built-wheel, resolver, or release evidence." in matrix


def test_release_workflow_builds_but_cannot_publish() -> None:
    workflow = _repository_text(".github/workflows/publish.yml")
    assert "workflow_dispatch:" in workflow
    assert "release:" not in workflow
    assert "sha256sum *.whl *.tar.gz > SHA256SUMS" in workflow
    assert "id-token" not in workflow
    assert "gh-action-pypi-publish" not in workflow
    assert 'set(extras) != {"dev", "gui", "semantics"}' in workflow
    assert '"ANYmesher>=0.3.2,<0.4"' in workflow
    assert '"ANYmaterial>=0.1.1,<0.2"' in workflow
    assert 'provides != {"dev", "gui", "semantics"}' in workflow
    assert 'ANYmesher<0.4,>=0.3.2; extra == "semantics"' in workflow
    assert 'ANYmaterial<0.2,>=0.1.1; extra == "semantics"' in workflow
    assert "timeout-minutes:" not in workflow


def test_production_publish_uses_verified_prebuilt_release_assets() -> None:
    workflow = _repository_text(".github/workflows/publish-release-assets.yml")
    assert "types: [published]" in workflow
    assert "github.event.release.prerelease == false" in workflow
    assert f"ref: ${{{{ github.event.release.tag_name }}}}" in workflow
    assert "fetch-depth: 0" in workflow
    assert "--protected-ref refs/remotes/origin/main" in workflow
    assert "--expected-terminal " + EXPECTED_TERMINAL in workflow
    assert CHECKOUT_ACTION in workflow
    assert SETUP_ACTION in workflow
    assert PUBLISH_ACTION in workflow
    assert "@release/v1" not in workflow
    assert 'gh release download "$RELEASE_TAG"' in workflow
    assert "--pattern" not in workflow
    assert "tools/verify_release_authority.py" in workflow
    assert LEDGER.as_posix() in workflow
    assert "--artifact " + WHEEL in workflow
    assert "--artifact " + SDIST in workflow
    assert "python -m build" not in workflow
    assert "timeout-minutes: 20" not in workflow
    assert "id-token: write" in workflow


def test_release_authority_accepts_exact_ledger_bound_artifacts(tmp_path: Path) -> None:
    completed = _run_verifier(tmp_path)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "paired-replacement",
        "checksum",
        "extra-asset",
        "tag",
        "wrong-source",
        "unmerged-tag-child",
        "wrong-terminal",
        "evidence-hash",
        "review-hash",
        "wrong-byte-count",
        "wrong-metadata",
        "extra-child-path",
        "noncanonical",
        "moved-tag-ref",
        "missing-tag-ref",
        "noncanonical-tag-ref",
        "replacement-ref",
        "graft-file",
        "info-attributes",
    ],
)
def test_release_authority_rejects_mutation(tmp_path: Path, mutation: str) -> None:
    completed = _run_verifier(tmp_path / mutation, mutation)
    assert completed.returncode != 0, mutation
    expected_errors = {
        "graft-file": "Git grafts are forbidden",
        "info-attributes": "Git info attributes are forbidden",
        "missing-tag-ref": "release tag ref does not resolve to a commit",
        "moved-tag-ref": "release tag ref does not identify the ledger HEAD",
        "noncanonical-tag-ref": "release tag is not canonical",
        "replacement-ref": "Git replacement objects are forbidden",
    }
    if mutation in expected_errors:
        assert expected_errors[mutation] in completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "core-attributes-config",
        "environment-external-diff",
        "git-attr-source",
        "global-attributes-config",
        "local-external-diff",
        "system-attributes-config",
        "textconv-diff-driver",
    ],
)
def test_release_authority_neutralizes_external_git_configuration(
    tmp_path: Path,
    mutation: str,
) -> None:
    case = tmp_path / mutation
    completed = _run_verifier(case, mutation)

    assert completed.returncode == 0, completed.stderr
    assert not (case / "attacker.marker").exists()


def test_paired_asset_and_checksum_replacement_is_not_authority(tmp_path: Path) -> None:
    completed = _run_verifier(tmp_path, "paired-replacement")
    assert completed.returncode != 0
    assert "committed authority" in completed.stderr


def test_public_release_separates_numpy_only_base_from_semantics_extra() -> None:
    readme = _repository_text("README.md")
    changelog = _repository_text("CHANGELOG.md")
    assert 'pip install ANYfileio`' in readme
    assert 'pip install "ANYfileio[semantics]"' in readme
    assert "The base remains independent" in readme
    assert "optional semantic owner integration" in readme
    assert "Publish the qualified semantic dependency extra" in changelog
    assert "ANYmesher 0.3.2" in changelog
    assert "ANYmaterial 0.1.1" in changelog
    assert "## 0.2.1 - 2026-08-27" in changelog
    assert "## 0.2.0 - 2026-08-20" in changelog
    assert "No native OCCT provider" in changelog


def test_semantic_consumer_tests_gate_optional_owners_locally() -> None:
    expected_functions = {
        "tests/test_calculix.py": ("_semantic_types",),
        "tests/test_sesam.py": (
            "test_semantics_resolves_a_neutral_mesh_and_records",
            "test_semantics_resolves_explicit_shell_local_axes",
            "test_semantics_maps_gunivec_to_beam_orientation",
        ),
        "tests/test_formats_and_cli.py": (
            "test_summary_resolves_the_document_into_neutral_records",
        ),
        "tests/test_gui.py": ("test_loading_a_fem_file_fills_every_panel",),
    }

    for relative, functions in expected_functions.items():
        tree = ast.parse(_repository_text(relative), filename=relative)
        top_level_imports = {
            alias.name.split(".")[0]
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        top_level_imports |= {
            node.module.split(".")[0]
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not ({"anymesher", "anymaterial"} & top_level_imports)
        for function_name in functions:
            function = _function(tree, function_name)
            assert _importorskip_modules(function) == {"anymesher", "anymaterial"}
