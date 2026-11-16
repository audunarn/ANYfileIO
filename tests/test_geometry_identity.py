"""Actual-file editable CAD identity is checked by the real atomic facade."""
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from anyfileio.geometry import GeometryWriteReport,GeometryWriteOptions,GeometryInterchangeError,write_geometry
from anyfileio.cad import CadEntityRef,cad_import_document_id,CadOperationCancelled


@pytest.fixture
def scenario(monkeypatch):
    import anyfileio.geometry as facade
    owner=pytest.importorskip('anygeometry')
    model=owner.GeometryModel()
    with model.transaction():
        first=model.add_point(0,0,0);second=model.add_point(1,0,0)
        model.add_line(first,second)
    assert model.revision==1 # True would compare equal here without the strict type check.
    backend=SimpleNamespace(backend_id='occt',backend_compatibility_version=1,backend_version='identity-test')
    data={'mutation':lambda report:report,'written':False}
    def exported(geometry,path,*,options,read_lease=None,cancellation=None):
        path.write_bytes(b'actual serialized CAD bytes')
        digest=hashlib.sha256(path.read_bytes()).hexdigest();unit=options.output_length_unit or 'm'
        did=cad_import_document_id(source_sha256=digest,source_format=path.suffix[1:],effective_source_length_unit=unit,
            backend_id=backend.backend_id,backend_compatibility_version=backend.backend_compatibility_version)
        data['did']=did;data['written']=True
        report=GeometryWriteReport(path.suffix[1:],geometry.model_id,geometry.revision,digest,geometry.units,unit,
            geometry_to_cad={geometry.handle('vertex',first):(CadEntityRef(did,'vertex',1),)},
            backend_version=backend.backend_version,output_document_id=did)
        return data['mutation'](report)
    monkeypatch.setattr(facade,'_provider',lambda name:SimpleNamespace(export_geometry=exported))
    monkeypatch.setattr('anyfileio.cad_backend._load_backend',lambda:backend)
    return model,data


@pytest.mark.parametrize('fmt',['step','iges','brep'])
@pytest.mark.parametrize('unit',['m','mm'])
def test_actual_file_identity_success(tmp_path,scenario,fmt,unit):
    model,state=scenario;path=tmp_path/('output.'+fmt)
    report=write_geometry(path,model,options=GeometryWriteOptions(output_length_unit=unit))
    assert report.output_document_id==state['did']
    assert report.output_sha256==hashlib.sha256(path.read_bytes()).hexdigest()
    assert all(r.document_id==report.output_document_id for refs in report.geometry_to_cad.values() for r in refs)


@pytest.mark.parametrize('change',[
    lambda r:replace(r,output_document_id=None),
    lambda r:replace(r,output_document_id='cad-geometry-v1:'+'0'*64),
    lambda r:replace(r,output_document_id=cad_import_document_id(source_sha256='1'*64,source_format=r.format,effective_source_length_unit=r.output_unit)),
    lambda r:replace(r,source_unit='mm'),lambda r:replace(r,output_unit='mm'),
    lambda r:replace(r,backend_version='another'),lambda r:replace(r,source_revision=r.source_revision+1),
    lambda r:replace(r,source_revision=True),
    lambda r:replace(r,source_model_id='foreign'),lambda r:replace(r,format='iges'),
])
def test_invalid_report_is_atomic(tmp_path,scenario,change):
    model,state=scenario;path=tmp_path/'out.step';path.write_bytes(b'old');state['mutation']=change
    with pytest.raises(GeometryInterchangeError,match='geometry.report.mismatch'):write_geometry(path,model)
    assert path.read_bytes()==b'old' and list(tmp_path.iterdir())==[path]


@pytest.mark.parametrize('kind',['local_ref','foreign','unknown','retired','replaced','empty','target_type','foreign_target','scalar_tuple'])
def test_invalid_mapping_is_atomic(tmp_path,scenario,kind):
    from anygeometry import GeometryModel
    from anygeometry.entities import EntityRef
    from anygeometry.identity import EntityHandle
    model,state=scenario;path=tmp_path/'out.step';path.write_bytes(b'old')
    retired=model.add_point(9,0,0);old=model.handle('vertex',retired);model.remove_vertex(retired)
    if kind=='replaced':
        first=model.add_point(10,0,0);last=model.add_point(11,0,0);edge=model.add_line(first,last)
        replaced=model.handle('edge',edge);model.split_edge(edge,.5)
    def mutation(report):
        handle=next(iter(report.geometry_to_cad));refs=report.geometry_to_cad[handle]
        if kind=='local_ref':handle=EntityRef(handle.kind,handle.id)
        if kind=='foreign':
            other=GeometryModel();identifier=other.add_point(0,0,0);handle=other.handle('vertex',identifier)
        if kind=='unknown':handle=EntityHandle(model.model_id,'vertex',99999)
        if kind=='retired':handle=old
        if kind=='replaced':handle=replaced
        if kind=='empty':refs=()
        if kind=='target_type':refs=('vertex-1',)
        if kind=='foreign_target':refs=(CadEntityRef('cad-import-v1:'+'0'*64,'vertex',1),)
        if kind=='scalar_tuple':refs=refs[0]
        return replace(report,geometry_to_cad={handle:refs})
    state['mutation']=mutation
    with pytest.raises(GeometryInterchangeError,match='geometry.report.mismatch'):write_geometry(path,model)
    assert path.read_bytes()==b'old' and list(tmp_path.iterdir())==[path]


def test_repeated_refs_preserve_retained_use_multiplicity(tmp_path,scenario):
    model,state=scenario
    def repeated(report):
        handle=next(iter(report.geometry_to_cad));ref=report.geometry_to_cad[handle][0]
        return replace(report,geometry_to_cad={handle:(ref,ref)})
    state['mutation']=repeated
    report=write_geometry(tmp_path/'out.step',model)
    assert len(next(iter(report.geometry_to_cad.values())))==2


def test_record_default_native_and_lazy_behavior(tmp_path):
    record=GeometryWriteReport('step','record-only',1,'0'*64,'m','m')
    assert record.output_document_id is None
    owner=pytest.importorskip('anygeometry')
    model=owner.GeometryModel();model.add_point(0,0,0)
    report=write_geometry(tmp_path/'native.anygeometry.json',model)
    assert report.output_document_id is None


def test_cancel_and_mutation_during_mapping_validation(tmp_path,scenario,monkeypatch):
    model,state=scenario;path=tmp_path/'out.step';path.write_bytes(b'old')
    original=model.resolve_handle
    def resolve(handle):
        result=original(handle);state['resolved']=True;return result
    monkeypatch.setattr(model,'resolve_handle',resolve)
    def cancellation():return state.get('resolved',False)
    with pytest.raises(CadOperationCancelled):write_geometry(path,model,cancellation=cancellation)
    assert path.read_bytes()==b'old'
    state.pop('resolved',None)
    def mutate():
        if state.get('resolved') and not state.get('mutated'):
            state['mutated']=True;model.add_point(2,0,0)
        return False
    with pytest.raises(GeometryInterchangeError,match='geometry.revision.changed'):write_geometry(path,model,cancellation=mutate)
    assert path.read_bytes()==b'old'


def test_cli_json_includes_actual_output_identity(tmp_path,monkeypatch,capsys):
    import json
    import anyfileio.geometry as facade
    from anyfileio.__main__ import main
    did='cad-import-v1:'+'1'*64
    imported=SimpleNamespace(geometry=object(),report=SimpleNamespace(format='anygeometry',approximations=(),metadata_losses=()))
    report=GeometryWriteReport('step','record-only',1,'0'*64,'m','m',output_document_id=did)
    monkeypatch.setattr(facade,'read_geometry',lambda *a,**k:imported)
    monkeypatch.setattr(facade,'write_geometry',lambda *a,**k:report)
    assert main(['--json','geometry-convert','input.anygeometry.json',str(tmp_path/'output.step')])==0
    assert json.loads(capsys.readouterr().out)['output_document_id']==did


def test_revision_mutation_inside_export_wins_over_changed_report_metadata(tmp_path,scenario):
    model,state=scenario;path=tmp_path/'out.step';path.write_bytes(b'old')
    def mutate(report):
        model.add_point(2,0,0)
        return replace(report,source_revision=model.revision)
    state['mutation']=mutate
    with pytest.raises(GeometryInterchangeError,match='geometry.revision.changed'):write_geometry(path,model)
    assert path.read_bytes()==b'old'


def test_owner_capability_refusal_is_typed(tmp_path,scenario,monkeypatch):
    model,_=scenario;monkeypatch.setattr(model,'resolve_handle',None)
    with pytest.raises(GeometryInterchangeError,match='geometry.owner.capability_missing'):
        write_geometry(tmp_path/'out.step',model)


def test_cad_mapping_callback_exception_and_lease_failure_preserve_primary(tmp_path,scenario,monkeypatch):
    model,state=scenario;path=tmp_path/'out.step';path.write_bytes(b'old')
    failure=RuntimeError('callback failure');seen=[]
    original=model.resolve_handle
    def resolve(handle):
        answer=original(handle);state['resolved']=True;return answer
    monkeypatch.setattr(model,'resolve_handle',resolve)
    class Lease:
        def __enter__(self):return SimpleNamespace(geometry=model,model_id=model.model_id,revision=model.revision)
        def __exit__(self,kind,error,tb):
            seen.append(error);raise ValueError('lease release failure')
    def cancellation():
        if state.get('resolved'):raise failure
        return False
    from anyfileio.cad import CadOperationError
    with pytest.raises(CadOperationError,match='cad.cancellation_check.failed') as error:
        write_geometry(path,model,read_lease=Lease(),cancellation=cancellation)
    assert error.value.__cause__ is failure and seen==[error.value]
    assert any('lease release' in note for note in error.value.__notes__)
    assert path.read_bytes()==b'old' and list(tmp_path.iterdir())==[path]


def test_cad_lease_is_held_through_identity_validation_and_publication(tmp_path,scenario,monkeypatch):
    import anyfileio.geometry as facade
    model,state=scenario;state['held']=False;replace_file=facade.os.replace
    class Lease:
        def __enter__(self):
            state['held']=True;return SimpleNamespace(geometry=model,model_id=model.model_id,revision=model.revision)
        def __exit__(self,*args):state['held']=False
    original=model.resolve_handle
    def resolve(handle):assert state['held'];return original(handle)
    def publish(*args):assert state['held'];return replace_file(*args)
    monkeypatch.setattr(model,'resolve_handle',resolve);monkeypatch.setattr(facade.os,'replace',publish)
    write_geometry(tmp_path/'out.step',model,read_lease=Lease())
    assert not state['held']
