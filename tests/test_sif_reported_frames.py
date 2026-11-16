"""Independent tensor checks for reported SIF result frames (synthetic fixtures)."""
from dataclasses import replace
from pathlib import Path
import numpy as np
import pytest
from anyfileio.sesam.records import FemRawRecord, records_to_text
from anyfileio.sesam.sif import read_sesam_sif_stress


def raw(name,values):
    return FemRawRecord(name,tuple(map(float,values)),(),0,0,())


def write_fixture(path, *, rotation=None, global_frame=False, codes=(1,2,4)):
    rotation=np.eye(3) if rotation is None else rotation
    rows=np.array([[10.,20.,3.],[30.,40.,-5.]])
    points=(1.,0.,0.,0.,2.,1.,1.,0.)
    rdp=(0.,7.,1.,24.,24.,2.,0.,0. if global_frame else 1.,0.)+points
    if not global_frame:rdp+=tuple(rotation.flatten(order='F'))
    rdp=(float(len(rdp)),)+rdp[1:]
    ordered=rows[:,[(1,2,4).index(c) for c in codes]]
    records=[raw('IDENT',(1,1,3,0)),raw('UNITS',(5,1,1,1,1)),
        raw('GCOORD',(1,0,0,0)),raw('GCOORD',(2,1,0,0)),raw('GCOORD',(3,1,1,0)),raw('GCOORD',(4,0,1,0)),
        raw('GELMNT1',(100,1,24,0,1,2,3,4)),raw('RDRESREF',(10,1,1,1,0,0,1,10,1,0)),
        raw('RDPOINTS',rdp),raw('RDSTRESS',(6,99,3,*codes)),raw('RVSTRESS',(11,1,1,7,99,*ordered.flatten())),raw('IEND',(0,0,0,0))]
    path.write_bytes(records_to_text(records).encode('ascii'))
    return records,rows


def tensor_oracle(rows,R):
    tensors=[]
    for sx,sy,xy in rows:
        local=np.array([[sx,xy,0],[xy,sy,0],[0,0,0]])
        tensors.append(R@local@R.T)
    mean=sum(tensors)/len(tensors)
    return [mean[0,0],mean[1,1],mean[2,2],mean[0,1],mean[1,2],mean[2,0]]


@pytest.mark.parametrize('angle',[0.,np.pi/2,.37])
def test_reported_common_frame_and_shear_matches_tensor_oracle(tmp_path,angle):
    # Tilt plane as well as rotate in-plane to exercise all six global entries.
    c,s=np.cos(angle),np.sin(angle)
    tilt=.4;C,S=np.cos(tilt),np.sin(tilt)
    R=np.array([[c,-s,0],[s,c,0],[0,0,1]])@np.array([[1,0,0],[0,C,-S],[0,S,C]])
    path=tmp_path/'case.SIF';_,rows=write_fixture(path,rotation=R,codes=(4,2,1))
    result=read_sesam_sif_stress(path,load_case=1)
    np.testing.assert_allclose(result.element_stress[100],tensor_oracle(rows,R),atol=1e-7)
    assert result.frame_provenance[100]['stress_descriptor']==99
    assert result.frame_provenance[100]['frame']=='reported_common_local_to_global'


def test_reported_identity_overrides_geometry_and_global_needs_no_matrix(tmp_path):
    for global_frame in (False,True):
        path=tmp_path/f'{global_frame}.SIF';_,rows=write_fixture(path,global_frame=global_frame)
        result=read_sesam_sif_stress(path)
        np.testing.assert_allclose(result.element_stress[100],[20,30,0,-1,0,0])
        np.testing.assert_allclose(result.nodal_stress[1],result.element_stress[100])


@pytest.mark.parametrize('mutation',['truncated','bad_nfield','nonfinite','reflection','nonorthogonal','layers',
    'per_point','bad_points','wrong_case','complex','components','missing_descriptor','missing_case_descriptor','bad_ref','bad_type','duplicates'])
def test_invalid_or_unsupported_reported_layout_never_falls_back(tmp_path,mutation):
    path=tmp_path/'bad.SIF';records,_=write_fixture(path)
    if mutation=='missing_descriptor':records=[r for r in records if r.name!='RDSTRESS']
    elif mutation=='missing_case_descriptor':records=[r for r in records if r.name!='RDRESREF']
    elif mutation=='duplicates':records.insert(-1,next(r for r in records if r.name=='RVSTRESS'))
    else:
        names={'wrong_case':'RVSTRESS','complex':'RDRESREF','components':'RDSTRESS','bad_ref':'RVSTRESS','bad_type':'RDPOINTS'}
        name=names.get(mutation,'RDPOINTS')
        for i,r in enumerate(records):
            if r.name!=name:continue
            v=list(r.numeric_fields)
            if mutation=='truncated':v=v[:-1];v[0]=len(v)
            elif mutation=='bad_nfield':v[0]+=1
            elif mutation=='nonfinite':v[-1]=float('inf')
            elif mutation=='reflection':v[-1]=-1
            elif mutation=='nonorthogonal':v[-1]=2
            elif mutation=='layers':v[8]=1
            elif mutation=='per_point':v[7]=2
            elif mutation=='bad_points':v[13]=1
            elif mutation=='wrong_case':v[1]=2
            elif mutation=='complex':v[5]=1
            elif mutation=='components':v[-1]=3
            elif mutation=='bad_ref':v[3]=8
            elif mutation=='bad_type':v[4]=25
            records[i]=replace(r,numeric_fields=tuple(v));break
    path.write_bytes(records_to_text(records).encode('ascii'))
    with pytest.raises(ValueError):read_sesam_sif_stress(path,load_case=1)


def test_cutout_grid_and_count(tmp_path):
    path=tmp_path/'cutout.SIF';records,rows=write_fixture(path)
    for i,r in enumerate(records):
        if r.name=='RDPOINTS':
            # One K, one J, three I slots: two existing points and one cutout.
            v=(0.,7.,1.,24.,24.,2.,10103.,1.,0.,1.,0.,0.,0.,-1.,2.,1.,1.,0.,*np.eye(3).flatten(order='F'))
            records[i]=replace(r,numeric_fields=(float(len(v)),)+v[1:])
    path.write_bytes(records_to_text(records).encode('ascii'))
    result=read_sesam_sif_stress(path,load_case=1)
    np.testing.assert_allclose(result.element_stress[100],tensor_oracle(rows,np.eye(3)))
