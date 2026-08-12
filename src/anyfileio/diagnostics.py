"""Structured diagnostics, shared by every format.

Reading is available in a strict mode and a lenient mode.  Strict raises on the
first error; lenient collects diagnostics and returns them alongside whatever was
successfully parsed.

That distinction is the point of this module.  The useful question about a file
from another tool is usually "what is wrong with all of it", not "what is the
first thing wrong", and answering the first needs errors to be data rather than
control flow.

Source line numbers are carried from the record layer upwards for one reason: a
diagnostic that cannot point at the text that caused it is not actionable on a
file of a hundred thousand records.

Severity matters as much as the code.  An element referencing a missing node is
an error; an element referencing an undefined material is a warning, because the
document is still readable and the caller may not care.  Collapsing the two would
force strict mode to reject files that are fine for the caller's purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Tuple

__all__ = [
    "CalculixError",
    "FemDiagnostic",
    "FileFormatError",
    "SemanticDependencyError",
    "SesamFemError",
    "has_errors",
    "raise_if_errors",
]


@dataclass(frozen=True)
class FemDiagnostic:
    """One structured import or export diagnostic."""

    code: str
    message: str
    severity: str = "error"
    record_name: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    context: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.record_name is not None:
            data["record_name"] = self.record_name
        if self.line_start is not None:
            data["line_start"] = self.line_start
        if self.line_end is not None:
            data["line_end"] = self.line_end
        if self.context:
            data["context"] = dict(self.context)
        return data


class FileFormatError(ValueError):
    """Raised when a file cannot be safely read or written.

    Carries the diagnostics that led to the failure, so a caller that raised in
    strict mode still gets everything lenient mode would have collected.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "FEM000",
        diagnostics: Iterable[FemDiagnostic] | None = None,
    ) -> None:
        self.code = code
        self.diagnostics: Tuple[FemDiagnostic, ...] = tuple(diagnostics or ())
        super().__init__(f"{code}: {message}")


class SesamFemError(FileFormatError):
    """Raised when a SESAM FEM or SIF file cannot be safely read or written."""


class CalculixError(FileFormatError):
    """Raised when a CalculiX deck or result file cannot be read or written."""


class SemanticDependencyError(FileFormatError):
    """Raised when the optional semantics runtime cannot be loaded."""


def has_errors(diagnostics: Iterable[FemDiagnostic]) -> bool:
    """Return True when the diagnostic collection contains errors."""

    return any(item.severity.lower() == "error" for item in diagnostics)


def raise_if_errors(diagnostics: Iterable[FemDiagnostic], message: str) -> None:
    """Raise a SesamFemError when any diagnostic has error severity."""

    items = tuple(diagnostics)
    if has_errors(items):
        first = next(item for item in items if item.severity.lower() == "error")
        raise SesamFemError(message, code=first.code, diagnostics=items)
