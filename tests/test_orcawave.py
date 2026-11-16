import json
from types import SimpleNamespace
import numpy as np
import pytest
from anyfileio.orcawave import OrcaWavePressureDocument, OrcaWavePressureError, OrcaWavePressureCancelled, read_orcawave_pressure
from anyfileio import OrcaWaveMissingOutputError, OrcaWaveUnsupportedFormatError, OrcaWaveInvalidDataError


def fake_api():
    geometry=np.zeros(2,dtype=[('vertices',float,(4,3)),('centroid',float,(3,)),('normal',float,(3,)),
        ('area',float),('objectId',int),('meshFilePanelIndex',int),('panelType',int)])
    geometry['vertices'][0]=[[0,0,0],[1,0,0],[0,1,0],[np.nan]*3]
    geometry['vertices'][1]=[[0,0,0],[1,0,0],[1,1,0],[0,1,0]]
    geometry['centroid']=[[1/3,1/3,0],[.5,.5,0]]; geometry['normal']=[0,0,1]
    geometry['area']=[.5,1]; geometry['objectId']=[0,1]; geometry['meshFilePanelIndex']=[1,1]
    obj=SimpleNamespace(state=2,panelGeometry=geometry,periods=np.array([6.,3.]),headings=np.array([0.,90.]),
        panelPressureDiffraction=np.full((2,2,2),1+2j),panelPressure=np.full((2,2,2),3-4j),
        WaterDepth=1e300,WaterDensity=1.025,g=9.81,LengthUnits='m',MassUnits='te',ForceUnits='kN',
        LoadResults=lambda filename:None)
    return SimpleNamespace(Diffraction=lambda:obj,DiffractionState=SimpleNamespace(CalculationComplete=2),DLLVersion=lambda:'11.6c'),obj


def test_read_raw_and_portable_without_api(tmp_path):
    api,obj=fake_api(); path=tmp_path/'case.owr'; path.write_bytes(b'fixture')
    doc=read_orcawave_pressure(path,api=api)
    assert doc.panel_ids==(0,1) and doc.mesh_panel_ids==(1,1)
    assert np.array_equal(doc.vertices[0,2],doc.vertices[0,3])
    assert doc.pressure_imag[0,0,0,0]==2 and doc.metadata['water_depth'] is None
    assert not doc.vertices.flags.writeable
    target=tmp_path/'portable.json'; doc.save(target)
    restored=read_orcawave_pressure(target)
    assert restored.content_sha256==doc.content_sha256
    damaged=doc.to_dict(); damaged['pressure_real'][0][0][0][0]=99
    with pytest.raises(OrcaWavePressureError,match='identity'): OrcaWavePressureDocument.from_dict(damaged)


def test_refuses_incomplete_missing_nonfinite_and_cancel(tmp_path):
    api,obj=fake_api(); path=tmp_path/'case.owr'; path.write_bytes(b'fixture')
    obj.state=0
    with pytest.raises(OrcaWavePressureError,match='not complete'):read_orcawave_pressure(path,api=api)
    obj.state=2; obj.panelPressure=None; obj.panelPressureDiffraction=None
    with pytest.raises(OrcaWavePressureError,match='unavailable'):read_orcawave_pressure(path,api=api)
    with pytest.raises(OrcaWavePressureCancelled):read_orcawave_pressure(path,cancel=lambda:True,api=api)
    api,obj=fake_api(); obj.panelPressure[0,0,0]=np.nan
    with pytest.raises(OrcaWavePressureError,match='nonfinite'):read_orcawave_pressure(path,api=api)


def test_license_error_typed(tmp_path):
    path=tmp_path/'case.owr'; path.write_bytes(b'fixture')
    def failed():raise RuntimeError('Error code25: licence unavailable')
    api=SimpleNamespace(Diffraction=failed)
    with pytest.raises(OrcaWavePressureError,match='code25'):read_orcawave_pressure(path,api=api)


def test_total_only_and_cancel_during_native_read(tmp_path):
    api,obj=fake_api();path=tmp_path/'case.owr';path.write_bytes(b'fixture')
    obj.panelPressureDiffraction=None
    assert read_orcawave_pressure(path,api=api).compositions==('total_with_source_motion',)
    cancelled=False
    def load(path):
        nonlocal cancelled
        cancelled=True
        assert obj.progressHandler(obj,50)
        raise RuntimeError('native read cancelled')
    obj.LoadResults=load
    with pytest.raises(OrcaWavePressureCancelled):read_orcawave_pressure(path,api=api,cancel=lambda:cancelled)


def test_missing_output_subclass_and_api_capability_detection(tmp_path):
    api,obj=fake_api();path=tmp_path/'case.owr';path.write_bytes(b'fixture')
    obj.panelPressure=None;obj.panelPressureDiffraction=None
    with pytest.raises(OrcaWaveMissingOutputError):read_orcawave_pressure(path,api=api)
    api,obj=fake_api();del obj.LoadResults
    with pytest.raises(OrcaWaveUnsupportedFormatError,match='capability|schema'):
        read_orcawave_pressure(path,api=api)
    api,obj=fake_api();obj.panelGeometry=np.zeros(1,dtype=[('unknown',float)])
    with pytest.raises(OrcaWaveUnsupportedFormatError,match='schema'):
        read_orcawave_pressure(path,api=api)
    # Capability-compatible future version is not rejected by a guessed cutoff.
    api,obj=fake_api();api.DLLVersion=lambda:'future-compatible-version'
    assert read_orcawave_pressure(path,api=api).metadata['provider_version']=='future-compatible-version'


@pytest.mark.parametrize('units',[dict(length='yard',mass='te',force='kN'),
                                 dict(length='m',mass='slug',force='kN'),
                                 dict(length='m',mass='te',force='unknown')])
def test_unsupported_units_keep_subclass_through_portable_layers(tmp_path,units):
    api,obj=fake_api();path=tmp_path/'case.owr';path.write_bytes(b'fixture')
    data=read_orcawave_pressure(path,api=api).to_dict()
    metadata=json.loads(data['metadata_json']);metadata['units']=units
    data['metadata_json']=json.dumps(metadata)
    with pytest.raises(OrcaWaveUnsupportedFormatError):OrcaWavePressureDocument.from_dict(data)
    portable=tmp_path/'data.json';portable.write_text(json.dumps(data))
    with pytest.raises(OrcaWaveUnsupportedFormatError):OrcaWavePressureDocument.load(portable)
    with pytest.raises(OrcaWaveUnsupportedFormatError):read_orcawave_pressure(portable)
    obj.LengthUnits=units['length'];obj.MassUnits=units['mass'];obj.ForceUnits=units['force']
    with pytest.raises(OrcaWaveUnsupportedFormatError):read_orcawave_pressure(path,api=api)


def test_invalid_array_json_identity_and_schema_subclasses(tmp_path):
    api,obj=fake_api();path=tmp_path/'case.owr';path.write_bytes(b'fixture')
    data=read_orcawave_pressure(path,api=api).to_dict()
    obj.panelPressure=np.ones((2,2,1),complex)
    with pytest.raises(OrcaWaveInvalidDataError):read_orcawave_pressure(path,api=api)
    malformed=dict(data);malformed['pressure_real']=[1]
    with pytest.raises(OrcaWaveInvalidDataError):OrcaWavePressureDocument.from_dict(malformed)
    malformed=dict(data);malformed['type']='unknown_schema'
    with pytest.raises(OrcaWaveUnsupportedFormatError):OrcaWavePressureDocument.from_dict(malformed)
    portable=tmp_path/'bad.json';portable.write_text('{bad')
    with pytest.raises(OrcaWaveInvalidDataError):read_orcawave_pressure(portable)
    malformed=dict(data);malformed['content_sha256']='f'*64
    with pytest.raises(OrcaWaveInvalidDataError):OrcaWavePressureDocument.from_dict(malformed)
    for kind in (OrcaWaveMissingOutputError,OrcaWaveUnsupportedFormatError,OrcaWaveInvalidDataError,OrcaWavePressureCancelled):
        assert issubclass(kind,OrcaWavePressureError)
