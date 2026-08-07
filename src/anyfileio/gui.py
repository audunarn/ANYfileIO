"""A tkinter file inspector.

Open a file, see what it contains, see what is wrong with it.  That is the whole
job, and it is the job worth having a window for: the useful question about a file
from another tool is "what is in here and does it make sense", which is tedious
to answer from a shell and immediate to answer from a list.

Records are shown with the source line numbers the record layer carried up, so a
diagnostic can be traced to the text that caused it. Reading is lenient by
default -- a file is opened to find out what is in it, and one unrecognized record
must not hide the rest.

Nothing here is imported by ``anyfileio/__init__.py``, so importing the package
never requires a display or a tkinter build.
"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Sequence

from .calculix.dat import parse_dat
from .calculix.frd import parse_frd
from .calculix.inp import summarize_deck
from .diagnostics import FemDiagnostic, FileFormatError
from .formats import describe, supported_suffixes
from .sesam.document import read_sesam_fem_document
from .sesam.exporter import write_sesam_fem_document
from .sesam.schema import get_element_spec
from .sesam.semantics import read_sesam_semantics
from .sesam.sif import read_sesam_sif_stress
from .sesam.validation import validate_sesam_fem_document

__all__ = ["InspectorWindow", "open_inspector", "main"]

_SEVERITY_COLOURS = {"error": "#a00000", "warning": "#8a5a00", "info": "#404040"}

# Records shown at once.  A real SESAM file runs to hundreds of thousands of
# records, and a Treeview asked to hold all of them stops being usable long
# before it runs out of memory.
_RECORD_LIMIT = 5000


class InspectorWindow(ttk.Frame):
    """The inspector, as a frame so it can be embedded as well as run alone."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)
        self._path: Optional[Path] = None
        self._document: Any = None
        self._summary: Dict[str, Any] = {}
        self._diagnostics: List[FemDiagnostic] = []
        self._message = ""

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=3)
        self.rowconfigure(4, weight=1)
        self._build_toolbar()
        self._build_summary()
        self._build_records()
        self._build_diagnostics()

    # --------------------------------------------------------------- toolbar

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)

        ttk.Button(bar, text="Open...", command=self.open).grid(row=0, column=0)
        self._path_label = ttk.Label(bar, text="no file open", anchor="w")
        self._path_label.grid(row=0, column=1, sticky="ew", padx=8)
        self._save_button = ttk.Button(
            bar, text="Save report...", command=self.save_report, state="disabled"
        )
        self._save_button.grid(row=0, column=2)
        self._canonicalize_button = ttk.Button(
            bar, text="Canonicalize...", command=self.canonicalize, state="disabled"
        )
        self._canonicalize_button.grid(row=0, column=3, padx=(4, 0))

        self._status = ttk.Label(self, text="", anchor="w", wraplength=760, justify="left")
        self._status.grid(row=1, column=0, sticky="ew", pady=(6, 6))

    # --------------------------------------------------------------- panels

    def _build_summary(self) -> None:
        frame = ttk.LabelFrame(self, text="Contents", padding=6)
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self._summary_text = tk.Text(
            frame, height=9, wrap="none", font=("TkFixedFont", 9), state="disabled"
        )
        self._summary_text.grid(row=0, column=0, sticky="nsew")

    def _build_records(self) -> None:
        frame = ttk.LabelFrame(self, text="Records", padding=6)
        frame.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self._records = ttk.Treeview(
            frame, columns=("name", "lines", "values"), show="headings", height=8
        )
        for column, heading, width in (
            ("name", "Record", 110),
            ("lines", "Lines", 90),
            ("values", "First values", 520),
        ):
            self._records.heading(column, text=heading)
            self._records.column(column, width=width, anchor="w")
        self._records.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._records.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self._records.configure(yscrollcommand=scroll.set)

    def _build_diagnostics(self) -> None:
        frame = ttk.LabelFrame(self, text="Diagnostics", padding=6)
        frame.grid(row=4, column=0, sticky="nsew", pady=(6, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self._issues = ttk.Treeview(
            frame, columns=("severity", "code", "line", "message"), show="headings", height=5
        )
        for column, heading, width in (
            ("severity", "Severity", 80),
            ("code", "Code", 70),
            ("line", "Line", 60),
            ("message", "Message", 540),
        ):
            self._issues.heading(column, text=heading)
            self._issues.column(column, width=width, anchor="w")
        self._issues.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._issues.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self._issues.configure(yscrollcommand=scroll.set)
        for severity, colour in _SEVERITY_COLOURS.items():
            self._issues.tag_configure(severity, foreground=colour)

    # ---------------------------------------------------------------- state

    @property
    def status_text(self) -> str:
        """The message currently shown to the user."""

        return self._message

    @property
    def summary(self) -> Dict[str, Any]:
        """The contents summary of the open file."""

        return dict(self._summary)

    @property
    def diagnostics(self) -> Sequence[FemDiagnostic]:
        """Diagnostics from reading the open file."""

        return tuple(self._diagnostics)

    def _set_status(self, message: str, colour: str) -> None:
        self._message = message
        self._status.configure(text=message, foreground=colour)

    def _set_summary_text(self, lines: Sequence[str]) -> None:
        self._summary_text.configure(state="normal")
        self._summary_text.delete("1.0", "end")
        self._summary_text.insert("1.0", "\n".join(lines))
        self._summary_text.configure(state="disabled")

    # -------------------------------------------------------------- loading

    def load(self, path: str | Path) -> None:
        """Read a file and fill the panels.  Lenient: report, do not refuse."""

        target = Path(path)
        self._path = target
        self._document = None
        self._summary = {}
        self._diagnostics = []
        self._records.delete(*self._records.get_children())
        self._issues.delete(*self._issues.get_children())
        self._save_button.configure(state="disabled")
        self._canonicalize_button.configure(state="disabled")
        self._path_label.configure(text=str(target))

        try:
            kind = describe(target)
        except FileFormatError as error:
            self._set_status(str(error), "#a00000")
            self._set_summary_text([])
            return

        try:
            self._load_by_suffix(target)
        except (OSError, FileFormatError, ValueError) as error:
            # Even a refusal is informative, so it lands in the status line and
            # whatever diagnostics came with it land in the list.
            self._set_status(f"{kind}: {error}", "#a00000")
            self._diagnostics = list(getattr(error, "diagnostics", ()) or ())
            self._fill_diagnostics()
            self._set_summary_text([])
            return

        self._fill_diagnostics()
        self._save_button.configure(state="normal")
        if self._document is not None:
            self._canonicalize_button.configure(state="normal")
        errors = sum(1 for item in self._diagnostics if item.severity.lower() == "error")
        warnings = len(self._diagnostics) - errors
        if errors:
            self._set_status(f"{kind}: {errors} error(s), {warnings} warning(s)", "#a00000")
        elif warnings:
            self._set_status(f"{kind}: {warnings} warning(s)", "#8a5a00")
        else:
            self._set_status(f"{kind}: no diagnostics", "#006000")

    def _load_by_suffix(self, target: Path) -> None:
        suffix = target.suffix.lower()
        if suffix in (".fem", ".sif"):
            # A SIF file uses the same record formatting as a FEM file, so the
            # document view -- record tree, diagnostics, counts -- works for both.
            # A SIF additionally carries results, so those are appended.
            self._load_document(target)
            if suffix == ".sif":
                self._append_sif_stress(target)
        elif suffix == ".frd":
            self._load_results(parse_frd(target), "FRD")
        elif suffix == ".dat":
            self._load_results(parse_dat(target), "DAT")
        elif suffix == ".inp":
            self._summary = summarize_deck(target)
            self._set_summary_text(
                [f"{key:<22} {value}" for key, value in self._summary.items()]
                + [
                    "",
                    "A generated deck is a reproducibility handoff, not a result:",
                    "nothing here says it was ever run.",
                ]
            )
        else:  # pragma: no cover - describe() already refused
            raise FileFormatError(f"unrecognized suffix {suffix!r}", code="FEM010")

    def _load_document(self, target: Path) -> None:
        document = read_sesam_fem_document(target, strict=False)
        self._document = document
        self._diagnostics = list(document.diagnostics) + list(validate_sesam_fem_document(document))

        histogram: Dict[str, int] = {}
        for element in document.elements.values():
            spec = get_element_spec(element.type_code)
            label = f"{element.type_code} ({spec.name})" if spec else f"{element.type_code} (unsupported)"
            histogram[label] = histogram.get(label, 0) + 1

        self._summary = {
            "records": len(document.raw_records),
            "nodes": len(document.nodes),
            "elements": len(document.elements),
            "materials": len(document.materials),
            "sections": len(document.sections),
            "boundaries": len(document.boundaries),
            "load_records": len(document.load_records),
            "dependencies": len(document.dependencies),
            "unknown_records": len(document.unknown_records),
            "element_types": histogram,
        }
        lines = [f"{key:<22} {value}" for key, value in self._summary.items() if key != "element_types"]
        lines.append("")
        lines.append("element types")
        for label, count in sorted(histogram.items()):
            lines.append(f"  {label:<20} {count}")
        if document.unknown_records:
            lines.append("")
            lines.append("Unrecognized records are preserved, not dropped, so a")
            lines.append("canonical rewrite does not delete what it did not understand.")
        self._set_summary_text(lines)
        self._fill_records(document)

        try:
            semantics = read_sesam_semantics(document, strict=False)
        except (FileFormatError, ValueError):
            return
        self._summary["mesh"] = {
            "quads": len(semantics.mesh.quads),
            "tris": len(semantics.mesh.tris),
            "beams": len(semantics.mesh.beams),
            "supports": len(semantics.supports),
        }

    def _append_sif_stress(self, target: Path) -> None:
        try:
            stress = read_sesam_sif_stress(target)
        except (OSError, FileFormatError, ValueError) as error:
            self._diagnostics.append(
                FemDiagnostic("SIF900", f"no readable stress results: {error}", severity="warning")
            )
            return
        self._summary["results"] = {
            "components": list(stress.components),
            "nodal_stress": len(stress.nodal_stress),
            "element_stress": len(stress.element_stress),
            "units": stress.units,
        }
        current = self._summary_text.get("1.0", "end").rstrip("\n").splitlines()
        current.extend(
            [
                "",
                "results",
                *(f"  {key:<20} {value}" for key, value in self._summary["results"].items()),
            ]
        )
        self._set_summary_text(current)

    def _load_results(self, parsed: Any, label: str) -> None:
        self._summary = parsed.summary()
        lines = [f"{key:<22} {value}" for key, value in self._summary.items() if key != "warnings"]
        lines.append("")
        # A CalculiX result file carries three displacement components and no
        # rotations.  Saying so is the difference between an absent field and a
        # zero one.
        lines.append(f"{label} results carry no rotations; absent components stay absent.")
        self._set_summary_text(lines)
        self._diagnostics = [
            FemDiagnostic("CCX900", message, severity="warning") for message in parsed.warnings
        ]

    def _fill_records(self, document: Any) -> None:
        for index, record in enumerate(document.raw_records):
            if index >= _RECORD_LIMIT:
                self._records.insert(
                    "",
                    "end",
                    values=(
                        "...",
                        "",
                        f"{len(document.raw_records) - _RECORD_LIMIT} further records not listed",
                    ),
                )
                break
            values = ", ".join(f"{value:g}" for value in record.numeric_fields[:6])
            if record.text_fields:
                values = f"{values}  {' '.join(record.text_fields[:2])}".strip()
            self._records.insert(
                "",
                "end",
                values=(
                    record.name,
                    f"{record.source_line_start}-{record.source_line_end}",
                    values,
                ),
            )

    def _fill_diagnostics(self) -> None:
        for item in self._diagnostics:
            severity = item.severity.lower()
            self._issues.insert(
                "",
                "end",
                values=(
                    severity,
                    item.code,
                    "" if item.line_start is None else str(item.line_start),
                    item.message,
                ),
                tags=(severity,),
            )

    # ------------------------------------------------------------ file menu

    def open(self) -> None:
        patterns = " ".join(f"*{suffix}" for suffix in supported_suffixes())
        path = filedialog.askopenfilename(
            title="Open file",
            filetypes=[("Supported files", patterns), ("All files", "*.*")],
        )
        if path:
            self.load(path)

    def report(self) -> Dict[str, Any]:
        """The open file's summary and diagnostics, as JSON-safe data."""

        return {
            "source": str(self._path) if self._path else None,
            "summary": self._summary,
            "diagnostics": [item.as_dict() for item in self._diagnostics],
        }

    def save_report(self) -> None:
        if self._path is None:
            messagebox.showerror("Save failed", "no file is open")
            return
        path = filedialog.asksaveasfilename(
            title="Save report", defaultextension=".json", filetypes=[("JSON", "*.json")]
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(self.report(), indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            messagebox.showerror("Save failed", str(error))

    def canonicalize(self) -> None:
        if self._document is None:
            messagebox.showerror("Canonicalize failed", "no SESAM document is open")
            return
        path = filedialog.asksaveasfilename(
            title="Write canonical FEM", defaultextension=".FEM", filetypes=[("SESAM FEM", "*.FEM")]
        )
        if not path:
            return
        try:
            report = write_sesam_fem_document(self._document, path, overwrite=True)
        except (OSError, FileFormatError) as error:
            messagebox.showerror("Canonicalize failed", str(error))
            return
        messagebox.showinfo(
            "Canonicalized",
            f"wrote {report.records_written} records ({report.bytes_written} bytes) to {report.path}",
        )


def open_inspector(
    master: tk.Misc,
    path: Optional[str | Path] = None,
    *,
    title: str = "ANYfileio",
) -> tuple[tk.Toplevel, InspectorWindow]:
    """Open an embeddable inspector, optionally with ``path`` loaded."""

    window = tk.Toplevel(master)
    window.title(title)
    window.minsize(860, 700)
    inspector = InspectorWindow(window)
    inspector.pack(fill="both", expand=True)
    if path is not None:
        inspector.load(path)
    return window, inspector


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Open the inspector, optionally on a file given on the command line."""

    import sys

    arguments = list(sys.argv[1:] if argv is None else argv)
    root = tk.Tk()
    root.title("ANYfileio")
    root.minsize(860, 700)
    window = InspectorWindow(root)
    window.pack(fill="both", expand=True)
    if arguments:
        window.load(arguments[0])
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
