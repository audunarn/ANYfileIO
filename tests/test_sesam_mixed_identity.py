from pathlib import Path

import pytest

from anyfileio import SesamFemError, read_sesam_fem_document, write_sesam_fem_document


def _mixed(unsupported_internal=24):
    return '\n'.join([
        'MISOSEL 1 2.1E11 .3 7850', 'GELTH 10 .012',
        'GCOORD 1 0 0 0', 'GCOORD 2 1 0 0', 'GCOORD 3 0 1 0',
        'GELMNT1 100 1 25 0 1 2 3',
        f'GELMNT1 200 {unsupported_internal} 18 0 1 2',
        'GELREF1 1 1 10', f'GELREF1 {unsupported_internal} 1 10', 'IEND', ''])


def test_unknown_full_header_retains_complete_numbering_and_roundtrip(tmp_path):
    source = tmp_path/'mixed.FEM'
    source.write_text(_mixed())
    doc = read_sesam_fem_document(source, strict=False)
    assert set(doc.elements) == {100}
    assert set(doc.element_references) == {100, 200}
    assert doc.elements[100].section_id == 10
    assert doc.elements[100].material_id == 1
    assert not any(d.code == 'FEM105' and 'neither internal nor external' in d.message for d in doc.diagnostics)
    target = tmp_path/'roundtrip.FEM'
    write_sesam_fem_document(doc, target)
    reread = read_sesam_fem_document(target, strict=False)
    assert reread.element_internal_to_external == doc.element_internal_to_external
    assert reread.element_references == doc.element_references
    assert [(r.name, r.numeric_fields) for r in reread.raw_records] == [(r.name, r.numeric_fields) for r in doc.raw_records]
    with pytest.raises(SesamFemError):
        read_sesam_fem_document(source, strict=True)


def test_unknown_raw_identity_duplicates_are_rejected(tmp_path):
    source = tmp_path/'duplicate.FEM'
    source.write_text(_mixed(1))
    doc = read_sesam_fem_document(source, strict=False)
    assert any(d.code == 'FEM102' and 'duplicate GELMNT1 internal' in d.message for d in doc.diagnostics)


def test_raw_identity_does_not_guess_ambiguous_reference_namespace(tmp_path):
    source = tmp_path/'ambiguous.FEM'
    source.write_text(_mixed().replace('100 1 25', '24 1 25').replace('200 24 18', '1 24 18'))
    doc = read_sesam_fem_document(source, strict=False)
    assert not doc.element_references
    assert any(d.code == 'FEM102' and 'ambiguous' in d.message for d in doc.diagnostics)


def test_compact_legacy_layout_keeps_external_references(tmp_path):
    source=tmp_path/'compact.FEM'
    source.write_text('\n'.join(['MISOSEL 1 2.1E11 .3 7850','GELTH 10 .012',
        'GCOORD 1 0 0 0','GCOORD 2 1 0 0','GCOORD 3 0 1 0',
        'GELMNT1 300 25 1 2 3','GELREF1 300 1 10','IEND','']))
    doc=read_sesam_fem_document(source,strict=False)
    assert not doc.element_internal_to_external
    assert doc.elements[300].node_ids==(1,2,3)
    assert doc.elements[300].section_id==10
    assert doc.elements[300].material_id==1
