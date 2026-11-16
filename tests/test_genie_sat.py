"""Contract checks for the initial GeniE XML/SAT reconstruction route."""
import base64
import io
import zipfile

import pytest

from anyfileio.genie_sat import GenieSatError, SatIndex, read_genie_concept


def sat_bytes(curve="straight-curve"):
    records = ["-100 face $600 -1 -1 $-1 $-1 $110 $-1 $-1 $700 forward double out #",
               "-110 loop $-1 -1 -1 $-1 $-1 $120 $100 #",
               "-600 string_attrib-name_attrib-gen-attrib $-1 -1 $-1 $-1 $100 2 1 @6 dnvscp @12 FACE00000001 #",
               "-601 string_attrib-name_attrib-gen-attrib $-1 -1 $-1 $-1 $200 2 1 @6 dnvscp @12 EDGE00000001 #",
               "-700 plane-surface $-1 -1 -1 $-1 0 0 0 0 0 1 1 0 0 forward_v I I I I #"]
    points = [(0, 0, 0), (2, 0, 0), (2, 1, 0), (0, 1, 0)]
    for i, point in enumerate(points):
        j = (i + 1) % 4
        records.extend([
            f"-{120+i} coedge $-1 -1 -1 $-1 ${120+j} ${120+(i-1)%4} $-1 ${200+i} forward $110 $-1 #",
            f"-{200+i} edge $-1 -1 -1 $-1 ${300+i} 0 ${300+j} 1 ${120+i} ${500+i} forward #",
            f"-{300+i} vertex $-1 -1 -1 $-1 ${200+i} ${400+i} #",
            f"-{400+i} point $-1 -1 -1 $-1 {' '.join(map(str,point))} #",
            f"-{500+i} {curve if i == 0 else 'straight-curve'} $-1 -1 -1 $-1 0 0 0 1 0 0 I I #",
        ])
    return ("2000 0 1 0\n" + "\n".join(records)).encode()


def write_xml(tmp_path, sat):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("source.sat", sat)
    data = stream.getvalue()
    # Each segment is independently base64 encoded, as in the real export.
    encoded = "".join(f"<cdata_segment>{base64.b64encode(x).decode()}</cdata_segment>"
                      for x in (data[:31], data[31:]))
    path = tmp_path / "concept.xml"
    path.write_text(f'''<DNV_structure_concept_protocol><model><units><model_units length="m"/></units><structure_domain>
    <properties><thicknesses><thickness name="t"><constant_thickness th="0.01"/></thickness></thicknesses></properties>
    <structures><structure><flat_plate name="Plate" thickness_ref="t" material_ref="Steel"><geometry><face face_ref="FACE00000001"/></geometry></flat_plate></structure>
    <structure><straight_beam name="Beam"><segments><straight_segment section_ref="bar" material_ref="Steel"><edge edge_ref="EDGE00000001"/></straight_segment></segments></straight_beam></structure></structures>
    <geometry><sat_embedded_sequence encoding="base64" compression="zip">{encoded}</sat_embedded_sequence></geometry>
    </structure_domain></model></DNV_structure_concept_protocol>''')
    return path


def test_segmented_archive_and_topology(tmp_path):
    doc = read_genie_concept(write_xml(tmp_path, sat_bytes()))
    assert [s.name for s in doc.structures] == ["Plate", "Beam"]
    assert doc.thicknesses == {"t": 0.01}
    face = doc.sat.face("FACE00000001")
    assert face.loops == (((200, False), (201, False), (202, False), (203, False)),)
    assert doc.sat.edge("EDGE00000001").end == (2, 0, 0)


def test_missing_entity_is_not_ignored(tmp_path):
    data = sat_bytes().replace(b"$400 #", b"$999 #")
    with pytest.raises(GenieSatError, match="missing SAT entity 999"):
        read_genie_concept(write_xml(tmp_path, data))


def test_duplicate_sat_identity_refused():
    with pytest.raises(GenieSatError, match="duplicate SAT entity"):
        SatIndex(sat_bytes() + b"\n-100 face x #")


def test_unknown_named_reference_refused():
    with pytest.raises(GenieSatError, match="missing SAT name"):
        SatIndex(sat_bytes()).face("FACE99999999")


def test_unknown_units_refused(tmp_path):
    path = write_xml(tmp_path, sat_bytes())
    path.write_text(path.read_text().replace('length="m"', 'length="mm"'))
    with pytest.raises(GenieSatError, match="metre model units"):
        read_genie_concept(path)


def test_editable_geometry_and_roundtrip(tmp_path):
    pytest.importorskip("anygeometry")
    from anyfileio.genie_geometry import reconstruct_linear_geometry
    from anygeometry.serialization import to_dict, from_dict
    doc = read_genie_concept(write_xml(tmp_path, sat_bytes()))
    model, mapping = reconstruct_linear_geometry(doc, ["Plate", "Beam"])
    assert len(model.vertices) == 4
    assert len(model.edges) == 4  # shared SAT edge identity survives
    assert len(model.faces) == len(model.members) == 1
    assert mapping == {"Plate": ("face", 1), "Beam": ("member", 1)}
    restored = from_dict(to_dict(model))
    assert restored.validate_topology() == ()
    assert restored.sheets[1].name == "Plate"
    assert restored.members[1].name == "Beam"


def test_curved_boundary_refused_without_approximation(tmp_path):
    pytest.importorskip("anygeometry")
    from anyfileio.genie_geometry import reconstruct_linear_geometry
    doc = read_genie_concept(write_xml(tmp_path, sat_bytes("intcurve-curve")))
    with pytest.raises(GenieSatError, match="curved SAT edge"):
        reconstruct_linear_geometry(doc, ["Plate"])


def test_genie_unit_conversion_opt_in_and_multi_segment_curved_reconstruction(tmp_path):
    pytest.importorskip("anygeometry")
    import math
    from anygeometry import GeometryModel, OrientedEdge
    from anygeometry.curves import EllipticArc
    from anygeometry.nurbs import NurbsCurve
    from anygeometry.surfaces import Plane, Cylinder
    from anyfileio.genie_geometry import reconstruct_genie_geometry, write_genie_concept

    # 1. Explicit allow_unit_conversion=True admits mm GeniE documents cleanly.
    mm_sat = sat_bytes().replace(b"-200 edge $-1 -1 -1 $-1 $300 0 $301 1", b"-200 edge $-1 -1 -1 $-1 $300 0 $301 2").replace(
        b"-501 straight-curve $-1 -1 -1 $-1 0 0 0 1 0 0 I I #", b"-501 straight-curve $-1 -1 -1 $-1 2 0 0 0 1 0 I I #"
    ).replace(
        b"-202 edge $-1 -1 -1 $-1 $302 0 $303 1", b"-202 edge $-1 -1 -1 $-1 $302 0 $303 2"
    ).replace(
        b"-502 straight-curve $-1 -1 -1 $-1 0 0 0 1 0 0 I I #", b"-502 straight-curve $-1 -1 -1 $-1 2 1 0 -1 0 0 I I #"
    ).replace(
        b"-503 straight-curve $-1 -1 -1 $-1 0 0 0 1 0 0 I I #", b"-503 straight-curve $-1 -1 -1 $-1 0 1 0 0 -1 0 I I #"
    )
    mm_xml = write_xml(tmp_path, mm_sat)
    mm_xml.write_text(mm_xml.read_text().replace('length="m"', 'length="mm"'))
    mm_doc = read_genie_concept(mm_xml, allow_unit_conversion=True)
    assert mm_doc.unit_scale_m == pytest.approx(0.001)
    assert mm_doc.thicknesses == {"t": pytest.approx(1e-5)}
    mm_model, _ = reconstruct_genie_geometry(mm_doc)
    assert max(v.position[0] for v in mm_model.vertices.values()) == pytest.approx(0.002)

    # 2. Export + re-import curved shells, flat plates, and multi-segment beams via write_genie_concept -> reconstruct_genie_geometry.
    model = GeometryModel()
    c0, c1, c2, c3 = model.add_points([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)])
    p_face = model.add_face([
        model.add_line(c0, c1),
        model.add_line(c1, c2),
        model.add_line(c2, c3),
        model.add_line(c3, c0),
    ], surface=Plane((0, 0, 0), (1, 0, 0), (0, 1, 0)))
    model.set_face_metadata(p_face, {"material": "S355", "thickness_ref": "plate20", "thickness_m": 0.02})
    model.add_sheet([p_face], name="DeckPlate")

    # Cylindrical quarter shell
    q0, q1, q2, q3 = model.add_points([(1, 0, 2), (0, 1, 2), (0, 1, 4), (1, 0, 4)])
    s_face = model.add_face_from_loop([
        OrientedEdge(model.add_curve(q0, q1, EllipticArc((0, 0, 2), (1, 0, 0), (0, 1, 0), 0.0, math.pi / 2)), True),
        OrientedEdge(model.add_line(q1, q2), True),
        OrientedEdge(model.add_curve(q2, q3, EllipticArc((0, 0, 4), (1, 0, 0), (0, 1, 0), math.pi / 2, -math.pi / 2)), True),
        OrientedEdge(model.add_line(q3, q0), True),
    ], surface=Cylinder((0, 0, 2), (0, 0, 1), (1, 0, 0), 1.0, 2.0, 0.0, math.pi / 2))
    model.set_face_metadata(s_face, {"material": "S420", "thickness_ref": "leg30", "thickness_m": 0.03})
    model.add_sheet([s_face], name="LegShell")

    # Multi-segment curved + straight beam
    b0, b1, b2 = model.add_points([(5, 0, 0), (6, 0, 0), (7, 1, 0)])
    e_b1 = model.add_line(b0, b1)
    e_b2 = model.add_curve(b1, b2, NurbsCurve(2, (0, 0, 0, 1, 1, 1), ((6, 0, 0), (6.5, 0.2, 0), (7, 1, 0))))
    model.add_member([e_b1, e_b2], name="Stiffener", metadata={"material": "S355", "section_ref": "hp200", "section_type": "bulb"})

    out_xml = tmp_path / "roundtrip_concept.xml"
    written = write_genie_concept(out_xml, model)
    assert written == out_xml
    doc2 = read_genie_concept(out_xml)
    assert doc2.thicknesses == {"plate20": pytest.approx(0.02), "leg30": pytest.approx(0.03)}
    assert doc2.sections["hp200"] == "bulb"
    rebuilt, mapping2 = reconstruct_genie_geometry(doc2)
    assert rebuilt.validate_topology() == ()
    assert set(mapping2) == {"DeckPlate", "LegShell", "Stiffener"}
    assert len(rebuilt.sheets) == 2 and len(rebuilt.members) == 1
    assert len(rebuilt.members[1].edge_use_ids) == 2
    assert isinstance(rebuilt.faces[mapping2["LegShell"][1]].surface, Cylinder)
    assert rebuilt.faces[mapping2["DeckPlate"][1]].metadata["thickness_m"] == pytest.approx(0.02)
    assert rebuilt.members[1].metadata["section_ref"] == "hp200"


def test_genie_holes_cone_and_nurbs_surface_roundtrip(tmp_path):
    pytest.importorskip("anygeometry")
    import math
    from anygeometry import GeometryModel, OrientedEdge
    from anygeometry.exact_curves import EllipticArc
    from anygeometry.nurbs import CurveOnSurface, NurbsCurve, NurbsSurface
    from anygeometry.serialization import from_dict, to_dict
    from anygeometry.surfaces import Cone, Plane
    from anyfileio.genie_geometry import reconstruct_genie_geometry, write_genie_concept
    from anyfileio.sat_import import _inject_face_holes

    model = GeometryModel()
    # 1. Flat plate with rectangular hole
    outer = model.add_points([(0, 0, 0), (4, 0, 0), (4, 4, 0), (0, 4, 0)])
    inner = model.add_points([(1, 1, 0), (1, 3, 0), (3, 3, 0), (3, 1, 0)])
    ol = model.add_polyline(outer, close=True)
    hl = model.add_polyline(inner, close=True)
    p_face = model.add_face(ol, surface=Plane((0, 0, 0), (1, 0, 0), (0, 1, 0)))
    model.set_face_metadata(p_face, {"material": "S355", "thickness_ref": "t25", "thickness_m": 0.025})

    # 2. Cone shell
    cv = model.add_points([(2, 0, 5), (0, 2, 5), (0, 1, 7), (1, 0, 7)])
    c_face = model.add_face_from_loop([
        OrientedEdge(model.add_curve(cv[0], cv[1], EllipticArc((0, 0, 5), (2, 0, 0), (0, 2, 0), 0.0, math.pi / 2)), True),
        OrientedEdge(model.add_line(cv[1], cv[2]), True),
        OrientedEdge(model.add_curve(cv[2], cv[3], EllipticArc((0, 0, 7), (1, 0, 0), (0, 1, 0), math.pi / 2, -math.pi / 2)), True),
        OrientedEdge(model.add_line(cv[3], cv[0]), True),
    ], surface=Cone((0, 0, 5), (0, 0, 1), (1, 0, 0), 2.0, 1.0, 2.0, 0.0, math.pi / 2))
    model.set_face_metadata(c_face, {"material": "S355", "thickness_ref": "t25", "thickness_m": 0.025})

    # 3. Curved NURBS shell
    ns = NurbsSurface(1, 2, (0, 0, 1, 1), (0, 0, 0, 1, 1, 1), (((10, 0, 0), (10, 2, 0.4), (10, 4, 0)), ((12, 0, 0), (12, 2, 0.8), (12, 4, 0))))
    uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    nv = [model.add_point(*ns.evaluate(*p)) for p in uvs]
    ne = [model.add_curve(nv[i], nv[(i + 1) % 4], CurveOnSurface(ns, NurbsCurve(1, (0, 0, 1, 1), (uvs[i], uvs[(i + 1) % 4])))) for i in range(4)]
    n_face = model.add_face(ne, surface=ns)
    model.set_face_metadata(n_face, {"material": "S420", "thickness_ref": "t15", "thickness_m": 0.015})

    model = _inject_face_holes(model, {p_face: [[OrientedEdge(e, True) for e in hl]]}, to_dict=to_dict, from_dict=from_dict)
    model.add_sheet([p_face], name="PerforatedPlate")
    model.add_sheet([c_face], name="ConeTransition")
    model.add_sheet([n_face], name="CurvedPanel")

    xml_path = tmp_path / "holes_surfaces.xml"
    write_genie_concept(xml_path, model)
    doc = read_genie_concept(xml_path)
    rebuilt, mapping = reconstruct_genie_geometry(doc)
    assert rebuilt.validate_topology() == ()
    assert set(mapping) == {"PerforatedPlate", "ConeTransition", "CurvedPanel"}
    assert len(rebuilt.faces[mapping["PerforatedPlate"][1]].holes) == 1
    assert isinstance(rebuilt.faces[mapping["ConeTransition"][1]].surface, Cone)
    assert isinstance(rebuilt.faces[mapping["CurvedPanel"][1]].surface, NurbsSurface)
