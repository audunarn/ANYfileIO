"""Command line interface.

``python -m anyfileio <command>``, or ``anyfileio <command>`` once installed.
``--lenient`` collects diagnostics instead of failing on the first error, and
``--json`` prints machine-readable output.

The ``inspect``, ``validate``, ``roundtrip`` and ``summary`` commands keep the
names and behaviour they had in ``anysolver.sesam_fem``, so an existing script
does not have to change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Sequence

from .calculix.dat import parse_dat
from .calculix.frd import parse_frd
from .calculix.inp import summarize_deck
from .diagnostics import FileFormatError
from .formats import describe, read, supported_suffixes
from .sesam.document import read_sesam_fem_document
from .sesam.exporter import write_sesam_fem_document
from .sesam.semantics import read_sesam_semantics
from .sesam.sif import read_sesam_sif_stress
from .sesam.validation import validate_sesam_fem_document

__all__ = ["main"]


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _document_summary(document: Any) -> Dict[str, Any]:
    return {
        "source": str(document.source_path) if document.source_path else None,
        "records": len(document.raw_records),
        "nodes": len(document.nodes),
        "elements": len(document.elements),
        "materials": len(document.materials),
        "sections": len(document.sections),
        "boundaries": len(document.boundaries),
        "load_records": len(document.load_records),
        "dependencies": len(document.dependencies),
        "element_count_by_type": {
            str(code): count
            for code, count in sorted(
                {
                    element.type_code: sum(
                        1 for other in document.elements.values() if other.type_code == element.type_code
                    )
                    for element in document.elements.values()
                }.items()
            )
        },
        "diagnostics": [item.as_dict() for item in document.diagnostics],
    }


def _command_formats(args: argparse.Namespace) -> int:
    from .formats import READERS

    if args.json:
        _print_json({suffix: label for suffix, (_reader, label) in sorted(READERS.items())})
        return 0
    print("recognized suffixes:")
    for suffix, (_reader, label) in sorted(READERS.items()):
        print(f"  {suffix:<6} {label}")
    return 0


def _command_inspect(args: argparse.Namespace) -> int:
    suffix = Path(args.input).suffix.lower()
    if suffix == ".fem":
        document = read_sesam_fem_document(args.input, strict=args.strict)
        summary = _document_summary(document)
    elif suffix == ".sif":
        summary = read_sesam_sif_stress(args.input).__dict__.copy()
        summary["nodes"] = len(summary.pop("nodes", {}))
        summary["element_nodes"] = len(summary.pop("element_nodes", {}))
        summary["nodal_stress"] = len(summary.pop("nodal_stress", {}))
        summary["element_stress"] = len(summary.pop("element_stress", {}))
    elif suffix in (".frd", ".dat"):
        parser = parse_frd if suffix == ".frd" else parse_dat
        summary = parser(args.input).summary()
    elif suffix == ".inp":
        summary = summarize_deck(args.input)
    else:
        raise FileFormatError(
            f"unrecognized suffix {suffix!r}; expected one of {', '.join(supported_suffixes())}",
            code="FEM010",
        )

    if args.json:
        _print_json(summary)
        return 0
    print(f"{args.input}  ({describe(args.input)})")
    for key, value in summary.items():
        if key == "diagnostics":
            continue
        print(f"  {key:<22} {value}")
    for item in summary.get("diagnostics", ()):
        print(f"  [{item['severity']}] {item['code']} {item['message']}")
    return 0


def _command_validate(args: argparse.Namespace) -> int:
    document = read_sesam_fem_document(args.input, strict=args.strict)
    diagnostics = list(document.diagnostics) + list(validate_sesam_fem_document(document))
    payload = {
        "source": str(args.input),
        "valid": not any(item.severity.lower() == "error" for item in diagnostics),
        "diagnostics": [item.as_dict() for item in diagnostics],
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"{args.input}: {'ok' if payload['valid'] else 'INVALID'}")
        for item in diagnostics:
            print(f"  [{item.severity}] {item.code} {item.message}")
    return 0 if payload["valid"] else 1


def _command_roundtrip(args: argparse.Namespace) -> int:
    document = read_sesam_fem_document(args.input, strict=args.strict)
    report = write_sesam_fem_document(
        document, args.output, mode=args.mode, overwrite=args.overwrite
    )
    payload = {
        "input": str(args.input),
        "output": str(report.path),
        "mode": report.mode,
        "records_written": report.records_written,
        "bytes_written": report.bytes_written,
        "diagnostics": [item.as_dict() for item in report.diagnostics],
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"{args.input} -> {report.path}")
        print(f"  mode            {report.mode}")
        print(f"  records         {report.records_written}")
        print(f"  bytes           {report.bytes_written}")
    return 0


def _command_summary(args: argparse.Namespace) -> int:
    semantics = read_sesam_semantics(args.input, strict=args.strict)
    payload = semantics.summary()
    if args.json:
        _print_json(payload)
        return 0
    print(f"{args.input}")
    for key, value in payload.items():
        if key == "diagnostics":
            continue
        print(f"  {key:<22} {value}")
    for item in payload.get("diagnostics", ()):
        print(f"  [{item['severity']}] {item['code']} {item['message']}")
    return 0


def _command_convert(args: argparse.Namespace) -> int:
    # Deliberately narrow: canonicalizing a SESAM document this package parsed is
    # supported, and synthesizing one from a model it never read is not.  A file
    # written outside that gate would look like an interchange file and would not
    # be one.
    if Path(args.input).suffix.lower() != ".fem" or Path(args.output).suffix.lower() != ".fem":
        raise FileFormatError(
            "convert canonicalizes a SESAM .fem document into another .fem file. "
            "Semantic export from an arbitrary model is not offered, because a file "
            "written outside a parsed document's gate would look authoritative "
            "without being so.",
            code="FEM011",
        )
    args.mode = "canonical"
    return _command_roundtrip(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anyfileio", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--lenient",
        action="store_true",
        help="collect diagnostics instead of failing on the first error",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("formats", help="list recognized file suffixes")

    inspect = sub.add_parser("inspect", help="show a summary of any supported file")
    inspect.add_argument("input")

    validate = sub.add_parser("validate", help="validate a SESAM FEM file")
    validate.add_argument("input")

    roundtrip = sub.add_parser("roundtrip", help="rewrite a SESAM FEM file")
    roundtrip.add_argument("input")
    roundtrip.add_argument("output")
    roundtrip.add_argument("--mode", choices=("canonical", "raw"), default="canonical")
    roundtrip.add_argument("--overwrite", action="store_true")

    convert = sub.add_parser("convert", help="canonicalize a SESAM FEM file")
    convert.add_argument("input")
    convert.add_argument("output")
    convert.add_argument("--overwrite", action="store_true")

    summary = sub.add_parser("summary", help="resolve a SESAM FEM file into neutral records")
    summary.add_argument("input")

    args = parser.parse_args(argv)
    args.strict = not args.lenient
    handlers = {
        "formats": _command_formats,
        "inspect": _command_inspect,
        "validate": _command_validate,
        "roundtrip": _command_roundtrip,
        "convert": _command_convert,
        "summary": _command_summary,
    }
    try:
        return handlers[args.command](args)
    except (FileExistsError, FileNotFoundError, FileFormatError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
