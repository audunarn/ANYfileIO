"""Private optional FreeCAD/InventorLoader adapter, run in an isolated process."""
import json
import math
import re
import sys
from pathlib import Path

def _affine_rows(values, length_scale):
    """Decode ACIS's column-major linear part and length-valued translation."""
    values = tuple((float(value) for value in values))
    if len(values) != 13 or not all((math.isfinite(value) for value in values)):
        raise ValueError('transform requires thirteen finite source values')
    if abs(values[12] - 1.0) > 16 * math.ulp(1.0):
        raise ValueError('unsupported nonunit transform scale')
    rows = tuple((tuple((values[column * 3 + row] for column in range(3))) + (values[9 + row] * length_scale,) for row in range(3)))
    a, b, c = (row[:3] for row in rows)
    determinant = a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])
    if not math.isfinite(determinant) or determinant == 0:
        raise ValueError('singular or unrepresentable affine transform')
    return rows + ((0.0, 0.0, 0.0, 1.0),)


def _numerical_zero_line_extent(start, end, source_accuracy):
    """Return a machine-zero image extent after every source transform, or None."""
    coordinates = (*start, *end)
    if not all(math.isfinite(value) for value in coordinates):
        raise ValueError('line image must have finite coordinates')
    extent = math.dist(start, end)
    coordinate_ulp = max(math.ulp(value) for value in coordinates)
    if not math.isfinite(source_accuracy) or source_accuracy <= 0:
        raise ValueError('source precision must be finite and positive')
    return extent if extent <= min(16 * coordinate_ulp, source_accuracy) else None

def _clamped_form_of_periodic_flag(degree, mults, pole_count):
    """Whether a periodic-flagged source spline is stored as a valid clamped open spline.

    ACIS may flag a closed curve (for example a full rational circle) as periodic
    while storing it with clamped end knots. Inside its knot domain that stored
    form is the curve exactly; only wrap-around evaluation would need periodicity.
    """
    degree = int(degree)
    mults = [int(m) for m in mults]
    return (degree >= 1 and len(mults) >= 2 and mults[0] == mults[-1] == degree + 1
            and all(1 <= m <= degree for m in mults[1:-1])
            and int(pole_count) == sum(mults) - degree - 1)


def _chord_deviation_bound(poles, knots, degree, lo, hi):
    """Upper bound on the distance between a non-rational B-spline span and its chord.

    ``knots`` is the flat knot vector. The second-derivative control points bound
    ``|C''|`` by the convex-hull property, and linear interpolation error gives
    ``|C(t) - L(t)| <= (hi - lo)**2 / 8 * max|C''|`` for every ``t`` in the span.
    """
    p = int(degree)
    if p < 2:
        raise ValueError('chord bound needs degree two or higher')
    points = [tuple(float(v) for v in point) for point in poles]
    u = [float(k) for k in knots]
    if len(u) != len(points) + p + 1:
        raise ValueError('knot vector and poles disagree')
    # The interpolation bound needs a C1 span: refuse a knot of multiplicity >= degree inside it.
    for knot in set(u):
        if float(lo) < knot < float(hi) and u.count(knot) >= p:
            raise ValueError('chord bound span crosses a tangent discontinuity')

    def difference(values, order, offset):
        out = []
        for i in range(len(values) - 1):
            span = u[i + order + offset] - u[i + offset]
            if span == 0:
                out.append((0.0, 0.0, 0.0))
            else:
                out.append(tuple(order * (b - a) / span for a, b in zip(values[i], values[i + 1])))
        return out
    first = difference(points, p, 1)
    second = difference(first, p - 1, 2)
    largest = max((math.sqrt(sum(c * c for c in vector)) for vector in second), default=0.0)
    bound = (float(hi) - float(lo)) ** 2 / 8.0 * largest
    if not math.isfinite(bound):
        raise ValueError('chord bound is not finite')
    return bound


def _linear_gain(rows):
    """Frobenius norm of a 3x3 linear map: an upper bound on how much it can stretch a length."""
    values = [float(value) for row in rows for value in row]
    if len(values) != 9 or not all(math.isfinite(value) for value in values):
        raise ValueError('linear map requires nine finite values')
    return math.sqrt(sum(value * value for value in values))


def _anchored_omissions(pieces, source_accuracy):
    """Admit omitted edge pieces only beside the kept curve, after every transform.

    ``pieces`` lists one source edge's pieces in order as ``(keep, start, end, extent)``
    with final (transformed) 3-tuples; ``extent`` bounds an omitted piece's whole image
    around its ``start``. Each omitted piece must lie within the source resolution of
    the kept curve point beside it: the end of the previous kept piece, or for a
    leading run the start of the first kept piece. Kept pieces' joins are checked
    later against the source resolution. Returns the worst admitted reach.
    """
    kept = [i for i, piece in enumerate(pieces) if piece[0]]
    if not kept:
        raise ValueError('source edge collapses entirely below numerical resolution')
    worst = 0.0
    for i, (keep, start, end, extent) in enumerate(pieces):
        if keep:
            continue
        before = [k for k in kept if k < i]
        anchor = pieces[before[-1]][2] if before else pieces[kept[0]][1]
        reach = math.dist(anchor, start) + float(extent)
        if not math.isfinite(reach) or reach > source_accuracy:
            raise ValueError(f'omitted piece reaches {reach} from the kept curve, beyond source resolution {source_accuracy}')
        worst = max(worst, reach)
    return worst


def _periodic_trim_inside(lo, hi, first, last):
    """A periodic-flagged spline gets no parameter slack: clipping could hide a wrapped turn."""
    return first <= lo < hi <= last


def _contiguous_domains(domains):
    """Law piece domains must be ordered and abut exactly, so clipping can never skip a gap."""
    values = [(float(a), float(b)) for a, b in domains]
    return bool(values) and all(a < b for a, b in values) and all(left[1] == right[0] for left, right in zip(values, values[1:]))


def _forward_intcurve(sense):
    """Only forward intcurves are read: a reversed one is C(t) = cache(-t), which is not composed here."""
    return sense == 'forward'


def _law_references_in_order(expression, argument_count):
    """Law pieces are paired with EDGE arguments by position, so require CUR(EDGE1..n) in order."""
    references = [int(n) for n in re.findall(r'CUR\(EDGE(\d+)\)', expression)]
    return references == list(range(1, argument_count + 1))


def _leaf_fit_tolerance(subtype, fit_tolerance):
    """Declared cache fit tolerance of a spline leaf (worker units); its admission is the caller's.

    ``exact_int_cur`` caches are the curve itself (declared 0). Procedural intcurves
    (surface intersections, parameter-space curves, ...) are represented by their
    cache, which the source declares accurate to its fit tolerance.
    """
    if subtype in ('law_int_cur', 'ref') or not isinstance(subtype, str):
        raise ValueError(f'intcurve {subtype} is not a spline leaf')
    if isinstance(fit_tolerance, bool) or not isinstance(fit_tolerance, (int, float)) or not math.isfinite(fit_tolerance) or fit_tolerance < 0:
        raise ValueError(f'intcurve {subtype} has no finite declared fit tolerance')
    return float(fit_tolerance)


def run(source, destination, loader):
    sys.path.insert(0, loader)
    import FreeCAD
    import Part
    import Acis
    original_set_bulk = Acis.Transform.setBulk

    def set_source_transform(transform, chunks, index):
        values, _ = Acis.getFloats(chunks, index, 13)
        try:
            rows = _affine_rows(values, Acis.getScale())
        except Exception as error:
            print(f'Invalid source transform: {error}; values={values}', file=sys.stderr)
            raise
        following = original_set_bulk(transform, chunks, index)
        transform.matrix = FreeCAD.Matrix(*(value for row in rows for value in row))
        return following
    # Decode raw source entries independently of the dependency matrix bug.
    Acis.Transform.setBulk = set_source_transform
    with open(source, encoding='latin-1') as stream:
        reader = Acis.AcisReader(stream)
        reader.name = 'standalone_sat'
        if not reader.readText():
            raise ValueError('InventorLoader refused SAT text')
        Acis.init()
        Acis.setReader(reader)
        with open(source, encoding='latin-1') as header_stream:
            header = [next(header_stream) for _ in range(3)]
        source_accuracy = float(header[2].split()[1]) * Acis.getScale()
        records = reader.getRecords()
        for record in records:
            if record.name == 'End-of-ACIS-data':
                break
            Acis.createEntity(record)

        def vector(v):
            return [v.x / 1000.0, v.y / 1000.0, v.z / 1000.0]
        numerical_zero = {'count': 0, 'max_spatial_length_m': 0.0}

        def spline_parts(curve, lo, hi, depth=0, roundoff=0.0):
            if depth > 24:
                raise ValueError('cyclic/deep curve reference')
            if isinstance(curve, Acis.CurveStraight):
                return [('line', curve.root + curve.dir * (lo * Acis.getScale()), curve.root + curve.dir * (hi * Acis.getScale()))]
            if not isinstance(curve, Acis.CurveInt):
                raise ValueError('unsupported law leaf ' + type(curve).__name__)
            if not _forward_intcurve(getattr(curve, 'sense', None)):
                raise ValueError('unsupported reversed intcurve')
            if curve.subtype == 'ref':
                return spline_parts(curve.curve, lo, hi, depth + 1, roundoff)
            if curve.subtype == 'law_int_cur':
                expression, arguments = curve.laws[0]
                # Admit only complete explicit piecewise/affine law expressions.
                domains = re.findall('PDOMAIN\\(CUR\\(EDGE\\d+\\),([^,]+),([^)]+)\\)', expression)
                edge_args = [a for a in arguments if a[0] == 'EDGE']
                if not expression.lstrip('(').startswith(('MULTI_CUR(', 'TRANS(MULTI_CUR(')) or len(domains) != len(edge_args):
                    raise ValueError('unsupported ACIS law: ' + expression[:120])
                mapping = re.search('O\\(([^*]+)\\*X\\+([^)]+)\\)$', expression)
                scale, shift = map(float, mapping.groups()) if mapping else (1.0, 0.0)
                grammar = expression[:mapping.start()] if mapping else expression
                while grammar.startswith('(') and grammar.endswith(')'):
                    balance = 0
                    outer = True
                    for position, char in enumerate(grammar):
                        balance += (char == '(') - (char == ')')
                        if balance == 0 and position < len(grammar) - 1:
                            outer = False
                            break
                    if not outer:
                        break
                    grammar = grammar[1:-1]
                number = '[+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[eE][+-]?\\d+)?'
                domain = f'PDOMAIN\\(CUR\\(EDGE\\d+\\),{number},{number}\\)'
                multi = f'MULTI_CUR\\({domain}(?:,{domain})*\\)'
                if re.fullmatch(f'(?:{multi}|TRANS\\({multi},TRANS\\d+\\))', grammar) is None:
                    raise ValueError('unsupported complete law expression: ' + expression[:120])
                if scale <= 0:
                    raise ValueError('nonpositive law parameter scale')
                if not _law_references_in_order(expression, len(edge_args)):
                    raise ValueError('law curve references are not EDGE1..n in argument order')
                if not _contiguous_domains(domains):
                    raise ValueError('law piece domains are not ordered and contiguous')
                matrices = [a[1].matrix for a in arguments if a[0] == 'TRANS']
                if len(matrices) > 1:
                    raise ValueError('multiple law transforms')
                if matrices:
                    m = matrices[0]
                    stretch = _linear_gain(((m.A11, m.A12, m.A13), (m.A21, m.A22, m.A23), (m.A31, m.A32, m.A33)))
                parts = []
                for arg, domain in zip(edge_args, domains):
                    a, b = map(float, domain)
                    if max(abs(a - arg[2][0]), abs(b - arg[2][1])) > 1e-08:
                        raise ValueError('law domain disagrees with referenced curve')
                    left, right = (max(a, lo * scale + shift), min(b, hi * scale + shift))
                    if right <= left:
                        continue
                    error_bound = roundoff * scale + 16 * max(math.ulp(lo * scale), math.ulp(hi * scale), math.ulp(shift), math.ulp(a), math.ulp(b))
                    candidates = spline_parts(arg[1], left, right, depth + 1, error_bound)
                    for item in candidates:
                        if matrices:
                            matrix = matrices[0]
                            if item[0] == 'line':
                                item = ('line', matrix.multVec(item[1]), matrix.multVec(item[2]))
                            elif item[0] == 'omit':
                                # Chord ends map exactly; the deviation bound scales by at most the stretch.
                                item = ('omit', matrix.multVec(item[1]), matrix.multVec(item[2]), item[3] * stretch)
                            else:
                                old = item[1]
                                new = Part.BSplineCurve()
                                new.buildFromPolesMultsKnots([matrix.multVec(p) for p in old.getPoles()], old.getMultiplicities(), old.getKnots(), False, old.Degree, old.getWeights())
                                item = ('nurbs', new, item[2] * stretch)
                        parts.append(item)
                if not parts and depth == 0:
                    raise ValueError('empty law range')
                return parts
            fit = _leaf_fit_tolerance(curve.subtype, getattr(curve, 'tolerance', None))
            data = getattr(curve, 'spline', None)
            if data is None or (data.uPeriodic and not _clamped_form_of_periodic_flag(data.uDegree, data.uMults, len(data.poles))):
                raise ValueError('unsupported missing/periodic source spline')
            geometry = Part.BSplineCurve()
            geometry.buildFromPolesMultsKnots(data.poles, data.uMults, data.uKnots, False, data.uDegree, data.weights)
            if lo < geometry.FirstParameter - 1e-07 or hi > geometry.LastParameter + 1e-07:
                raise ValueError('trim outside spline domain')
            if data.uPeriodic and not _periodic_trim_inside(lo, hi, geometry.FirstParameter, geometry.LastParameter):
                raise ValueError('unsupported periodic trim outside its stored domain')
            first, last = (max(lo, geometry.FirstParameter), min(hi, geometry.LastParameter))
            piece = geometry.copy()
            try:
                piece.segment(first, last)
            except Part.OCCError:
                # A span OCC cannot trim becomes an omission candidate: its chord plus a
                # certified deviation bound. Admission is decided per edge after transforms.
                if geometry.isRational():
                    raise
                start, end = (geometry.value(first), geometry.value(last))
                flat = [k for k, m in zip(data.uKnots, data.uMults) for _ in range(int(m))]
                return [('omit', start, end, _chord_deviation_bound(data.poles, flat, data.uDegree, first, last) + fit)]
            return [('nurbs', piece, fit)]
        output = {'vertices': {}, 'edges': {}, 'surfaces': {}, 'errors': []}
        for record in records:
            entity = getattr(record, 'entity', None)
            try:
                if record.name == 'body' and entity.getTransform() is not None:
                    raise ValueError('body transforms are not supported')
                if record.name in ('vertex', 'tvertex-vertex'):
                    output['vertices'][str(entity.index)] = vector(entity.getPosition())
                if record.name == 'edge':
                    curve = entity.getCurve()
                    a, b = (entity.parameter1, entity.parameter2)
                    if entity.sense != 'forward' or b <= a:
                        raise ValueError('unsupported reversed/nonpositive curve parameter interval')
                    if isinstance(curve, Acis.CurveEllipse):
                        output['edges'][str(entity.index)] = [{'kind': 'ellipse', 'center': vector(curve.center), 'u': vector(curve.major), 'v': vector(curve.axis.cross(curve.major) * curve.ratio), 'start': a, 'sweep': b - a}]
                    else:
                        parts = spline_parts(curve, a, b)
                        made = []
                        pieces = []
                        zero_lines = []
                        untrimmable = 0
                        for part in parts:
                            if part[0] == 'line':
                                extent = _numerical_zero_line_extent(tuple(part[1]), tuple(part[2]), source_accuracy)
                                if extent is not None:
                                    zero_lines.append(extent)
                                    pieces.append((False, tuple(part[1]), tuple(part[2]), extent))
                                    continue
                                pieces.append((True, tuple(part[1]), tuple(part[2]), 0.0))
                                made.append({'kind': 'line', 'start': vector(part[1]), 'end': vector(part[2])})
                            elif part[0] == 'omit':
                                untrimmable += 1
                                pieces.append((False, tuple(part[1]), tuple(part[2]), part[1].distanceToPoint(part[2]) + part[3]))
                            else:
                                g = part[1]
                                pieces.append((True, tuple(g.value(g.FirstParameter)), tuple(g.value(g.LastParameter)), 0.0))
                                made.append({'kind': 'nurbs', 'degree': g.Degree, 'knots': [k for k, m in zip(g.getKnots(), g.getMultiplicities()) for _ in range(m)], 'controls': [vector(p) for p in g.getPoles()], 'weights': g.getWeights(), 'domain': [g.FirstParameter, g.LastParameter]})
                                if part[2] > 0:
                                    made[-1]['declared_fit_tolerance_m'] = part[2] / 1000.0
                        reach = _anchored_omissions(pieces, source_accuracy)
                        numerical_zero['count'] += len(zero_lines)
                        numerical_zero['max_spatial_length_m'] = max([numerical_zero['max_spatial_length_m']] + [extent / 1000.0 for extent in zero_lines])
                        if untrimmable:
                            numerical_zero['untrimmable_sub_resolution_count'] = numerical_zero.get('untrimmable_sub_resolution_count', 0) + untrimmable
                            numerical_zero['untrimmable_sub_resolution_max_reach_m'] = max(numerical_zero.get('untrimmable_sub_resolution_max_reach_m', 0.0), reach / 1000.0)
                        output['edges'][str(entity.index)] = made
                if record.name == 'face':
                    surface = entity.getSurface().build().Surface
                    if type(surface).__name__ == 'Plane':
                        output['surfaces'][str(entity.index)] = {'kind': 'plane', 'origin': vector(surface.Position), 'normal': list(surface.Axis)}
                    elif type(surface).__name__ == 'Cylinder':
                        output['surfaces'][str(entity.index)] = {'kind': 'cylinder', 'origin': vector(surface.Center), 'axis': list(surface.Axis), 'radius': surface.Radius / 1000.0}
                    else:
                        raise ValueError('unsupported surface ' + type(surface).__name__)
            except Exception as error:
                output['errors'].append(f"{record.name} {getattr(entity, 'index', '?')}: {error}")
        output['numerical_zero_intervals'] = numerical_zero
        Path(destination).write_text(json.dumps(output), encoding='utf-8')
if __name__ == '__main__':
    run(*sys.argv[1:])
