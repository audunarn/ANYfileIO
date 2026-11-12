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
                matrices = [a[1].matrix for a in arguments if a[0] == 'TRANS']
                if len(matrices) > 1:
                    raise ValueError('multiple law transforms')
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
                            else:
                                old = item[1]
                                new = Part.BSplineCurve()
                                new.buildFromPolesMultsKnots([matrix.multVec(p) for p in old.getPoles()], old.getMultiplicities(), old.getKnots(), False, old.Degree, old.getWeights())
                                item = ('nurbs', new)
                        parts.append(item)
                if not parts and depth == 0:
                    raise ValueError('empty law range')
                return parts
            data = getattr(curve, 'spline', None)
            if data is None or data.uPeriodic:
                raise ValueError('unsupported missing/periodic source spline')
            geometry = Part.BSplineCurve()
            geometry.buildFromPolesMultsKnots(data.poles, data.uMults, data.uKnots, False, data.uDegree, data.weights)
            if lo < geometry.FirstParameter - 1e-07 or hi > geometry.LastParameter + 1e-07:
                raise ValueError('trim outside spline domain')
            geometry.segment(max(lo, geometry.FirstParameter), min(hi, geometry.LastParameter))
            return [('nurbs', geometry)]
        output = {'vertices': {}, 'edges': {}, 'surfaces': {}, 'errors': []}
        for record in records:
            entity = getattr(record, 'entity', None)
            try:
                if record.name == 'body' and entity.getTransform() is not None:
                    raise ValueError('body transforms are not supported')
                if record.name == 'vertex':
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
                        for part in parts:
                            if part[0] == 'line':
                                extent = _numerical_zero_line_extent(tuple(part[1]), tuple(part[2]), source_accuracy)
                                if extent is not None:
                                    numerical_zero['count'] += 1
                                    numerical_zero['max_spatial_length_m'] = max(numerical_zero['max_spatial_length_m'], extent / 1000.0)
                                    continue
                                made.append({'kind': 'line', 'start': vector(part[1]), 'end': vector(part[2])})
                            else:
                                g = part[1]
                                made.append({'kind': 'nurbs', 'degree': g.Degree, 'knots': [k for k, m in zip(g.getKnots(), g.getMultiplicities()) for _ in range(m)], 'controls': [vector(p) for p in g.getPoles()], 'weights': g.getWeights(), 'domain': [g.FirstParameter, g.LastParameter]})
                        if not made:
                            raise ValueError('source edge collapses entirely below numerical resolution')
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
