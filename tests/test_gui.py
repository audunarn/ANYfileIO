"""Inspector smoke tests.

These drive the real widgets: load a file, read back the status, the summary and
the diagnostics list.  Skipped when no display is available, which is the case on
Linux CI runners.

One module-scoped root is used throughout; creating and destroying Tk roots per
test is unreliable on Windows.
"""

from __future__ import annotations

import json
import tkinter as tk

import pytest

pytest.importorskip("tkinter.ttk", reason="the inspector needs a tkinter build")


def _mixed_shell_fem() -> str:
    return "\n".join(
        [
            "IDENT          100               1",
            "MISOSEL          1  2.100000D+11  3.000000D-01  7.850000D+03",
            "GELTH           10  2.000000D-02",
            "GCOORD           1               0               0               0",
            "GCOORD           2               1               0               0",
            "GCOORD           3               0               1               0",
            "GCOORD           4               1               1               0",
            "GNODE            1               1               6          123456",
            "GNODE            2               2               6          123456",
            "GNODE            3               3               6          123456",
            "GNODE            4               4               6          123456",
            "GELMNT1        100               0              25               0               1               2               3",
            "GELREF1        100               1              10",
            "GELMNT1        200               0              24               0               1               2               4               3",
            "GELREF1        200               1              10",
            "BNBCD            1               6               1               1               1               0               0               0",
            "FOOBAR          99",
            "IEND",
            "",
        ]
    )


@pytest.fixture(scope="module")
def root():
    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available for tkinter")
    window.geometry("900x740+40+40")
    window.update()
    yield window
    window.destroy()


@pytest.fixture
def inspector(root):
    from anyfileio.gui import InspectorWindow

    frame = InspectorWindow(root)
    frame.pack(fill="both", expand=True)
    root.update()
    yield frame
    frame.destroy()
    root.update()


@pytest.fixture
def fem_file(tmp_path):
    path = tmp_path / "model.FEM"
    path.write_text(_mixed_shell_fem(), encoding="ascii")
    return path


def test_the_inspector_opens_empty(inspector, root) -> None:
    assert inspector.summary == {}
    assert inspector.diagnostics == ()


def test_loading_a_fem_file_fills_every_panel(inspector, root, fem_file) -> None:
    pytest.importorskip("anymesher")
    pytest.importorskip("anymaterial")

    inspector.load(fem_file)
    root.update()

    summary = inspector.summary
    assert summary["nodes"] == 4
    assert summary["elements"] == 2
    assert summary["records"] > 10
    assert summary["element_types"] == {"25 (T3)": 1, "24 (Q4)": 1}
    # The semantic layer runs too, so the mesh counts are there without a
    # separate command.
    assert summary["mesh"] == {"quads": 1, "tris": 1, "beams": 0, "supports": 1}

    # Records are listed with the source lines the record layer carried up.
    rows = inspector._records.get_children()
    assert len(rows) == summary["records"]
    first = inspector._records.item(rows[0])["values"]
    assert first[0] == "IDENT"
    assert "-" in str(first[1])


def test_an_unrecognized_record_is_reported_as_a_warning(inspector, root, fem_file) -> None:
    inspector.load(fem_file)
    root.update()

    codes = {item.code for item in inspector.diagnostics}
    assert "FEM110" in codes
    assert "warning" in inspector.status_text
    assert inspector._issues.get_children()

    # Preserved rather than dropped, which is the point of reporting it.
    assert any(
        inspector._records.item(row)["values"][0] == "FOOBAR"
        for row in inspector._records.get_children()
    )


def test_reading_is_lenient_so_a_broken_file_still_shows_its_contents(inspector, root, tmp_path) -> None:
    path = tmp_path / "missing_iend.FEM"
    path.write_text(
        "IDENT          1\nGCOORD           1               0               0               0\n",
        encoding="ascii",
    )

    inspector.load(path)
    root.update()

    # Strict reading would have refused the whole file; the inspector exists to
    # say what is in it, so it reports the problem and shows the rest.
    assert any(item.code == "FEM003" for item in inspector.diagnostics)
    assert inspector.summary["records"] >= 2
    assert "error" in inspector.status_text


def test_an_unsupported_suffix_is_reported_not_raised(inspector, root, tmp_path) -> None:
    path = tmp_path / "model.xyz"
    path.write_text("nothing", encoding="ascii")

    inspector.load(path)
    root.update()

    assert "unrecognized suffix" in inspector.status_text
    assert inspector.summary == {}


def test_calculix_results_load_and_say_what_they_lack(inspector, root, tmp_path) -> None:
    path = tmp_path / "case.frd"
    path.write_text(
        "\n".join(
            [
                "    1C",
                "    2C                   2                                     1",
                " -1         1 0.00000E+00 0.00000E+00 0.00000E+00",
                " -1         2 1.00000E+00 0.00000E+00 0.00000E+00",
                "    3C",
                " -4  DISP        4    1",
                " -5  D1          1    2    1    0",
                " -5  D2          1    2    2    0",
                " -5  D3          1    2    3    0",
                " -1         1 0.00000E+00 0.00000E+00 0.00000E+00",
                " -1         2 1.00000E-03 0.00000E+00-2.00000E-03",
                " -3",
                " 9999",
                "",
            ]
        ),
        encoding="ascii",
    )

    inspector.load(path)
    root.update()

    assert inspector.summary["displacement_nodes"] == 2
    text = inspector._summary_text.get("1.0", "end")
    assert "no rotations" in text


def test_the_report_is_json_safe(inspector, root, fem_file, tmp_path) -> None:
    inspector.load(fem_file)
    root.update()

    report = inspector.report()
    written = json.loads(json.dumps(report, default=str))

    assert written["summary"]["nodes"] == 4
    assert written["diagnostics"]
    assert str(fem_file) in written["source"]


def test_loading_a_second_file_clears_the_first(inspector, root, fem_file, tmp_path) -> None:
    inspector.load(fem_file)
    root.update()
    assert inspector._records.get_children()

    other = tmp_path / "tiny.FEM"
    other.write_text("IDENT          1\nIEND\n", encoding="ascii")
    inspector.load(other)
    root.update()

    assert inspector.summary["nodes"] == 0
    assert inspector.summary["elements"] == 0
    assert len(inspector._records.get_children()) == 2
    assert inspector.diagnostics == ()


def test_open_inspector_helper_embeds_and_preloads_a_file(root, fem_file) -> None:
    from anyfileio.gui import open_inspector

    window, embedded = open_inspector(root, fem_file, title="Host inspector")
    root.update()

    assert window.title() == "Host inspector"
    assert embedded.summary["nodes"] == 4
    window.destroy()
    root.update()


def test_the_window_tears_down_cleanly(root) -> None:
    # Widget attributes that collide with tkinter's own internals only fail on
    # destroy, and only sometimes, so the teardown path is asserted directly.
    from anyfileio.gui import InspectorWindow

    frame = InspectorWindow(root)
    frame.pack(fill="both", expand=True)
    root.update()
    frame.destroy()
    root.update()

    assert not frame.winfo_exists()
