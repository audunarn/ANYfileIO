# Architecture

## Position in the family

```
ANYmaterial ──┐
              ├──→ ANYfileio ──┐
ANYmesher ────┘                ├──→ ANYsolver ──→ ANYfem
                               │         └──────→ ANYstructure
                               └────────────────→ ANYstructure
```

ANYfileio sits above ANYmesher and ANYmaterial and below ANYsolver. It hands back
a neutral mesh and neutral material and section records; turning those into
solver elements is ANYsolver's job. `tests/test_layering.py` enforces the
direction by walking the AST of every module, because the tempting import — pull
in the solver to compare a parsed document against a built model — is exactly the
one that would close the cycle.

The consequence worth stating: `build_fe_model_from_sesam_document` stays in
ANYsolver. This package can say "twelve Q4 shells with these nodes and this
thickness"; it cannot say `ShellElement`.

## Three layers per format

| Layer | Answers | Needs |
| --- | --- | --- |
| Records | what does the file say? | nothing |
| Document | what does it mean? | nothing |
| Semantics | what mesh and materials is that? | ANYmesher, ANYmaterial |

The separation is not ceremony. Most real questions about a file from another
tool stop at the first or second layer: is it well formed, what element types
does it contain, what does it reference that is missing. Those answers must not
require a mesh library, a material library or a solver, and with this layering
they do not.

It also means a file can be round-tripped without being understood. Records this
package cannot interpret are preserved and rewritten, so canonicalizing a file
does not silently delete the parts it did not recognize — which is the failure
mode that makes people distrust converters.

## Diagnostics are data

Reading has a strict mode and a lenient mode. Strict raises on the first error.
Lenient collects `FemDiagnostic` values — a code, a severity, the record name,
the source line range, and context — and returns them alongside whatever was
successfully parsed.

Source line numbers are carried from the record layer upwards for one reason:
a diagnostic that cannot point at the text that caused it is not actionable on a
file of a hundred thousand records. Keeping the line range costs a tuple per
record and is worth it.

Severity matters as much as the code. An element referencing a missing node is an
error; an element referencing an undefined material is a warning, because the
document is still readable and the caller may not care. Collapsing those into one
category would force strict mode to reject files that are fine for the caller's
purpose.

## What is refused, and why

Semantic SESAM export from an arbitrary model is not implemented, and not because
it would be hard. A `.fem` file is an interchange format: whoever receives one
treats it as authoritative. Writing one from a model this package never parsed
would produce a file whose fidelity nobody has established, and it would look
exactly like a file whose fidelity had been. Round-tripping a document parsed
here is supported and guarded; synthesis is not offered at all rather than
offered with a caveat in the docstring.

The same reasoning applies to generated CalculiX decks. A deck that has not been
run is a reproducibility handoff, not evidence of agreement. ANYsolver labels
unexecuted decks `not_executed`; nothing here dresses them up as more.

## Units

SI throughout. A SESAM file declares its own units in its header; those are
honoured on read and converted once, at that boundary, so nothing downstream has
to know the file was not already SI.
