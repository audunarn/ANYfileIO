"""Read-only OrcaWave panel results and a portable NumPy-only document.

Values retain the provider's units, complex convention and panel reference normals.
Body normals point out of the fluid; dipole normals orient an arbitrary sheet and
their result is the signed jump p_minus-p_plus, exerting force along that normal.
Hydrodynamic normalization belongs to ANYhydro, not this interchange module.
"""
from dataclasses import dataclass
import hashlib
import importlib
import json
import re
from pathlib import Path
import numpy as np

SCHEMA = 'orcawave_pressure_document_v1'
COMPOSITIONS = ('fixed_diffraction', 'total_with_source_motion')
SUPPORTED_UNITS = {'length': frozenset(('m','mm','cm','ft')),
                   'mass': frozenset(('kg','te','t','lb')),
                   'force': frozenset(('N','kN','lbf','kip'))}


class OrcaWavePressureError(ValueError):
    """Missing, invalid, unsupported or unavailable external pressure results."""


class OrcaWavePressureCancelled(OrcaWavePressureError):
    pass


class OrcaWaveMissingOutputError(OrcaWavePressureError):
    """The calculation or the requested panel result inventory is unavailable."""


class OrcaWaveUnsupportedFormatError(OrcaWavePressureError):
    """Unsupported schema, API capabilities, conventions or unit vocabulary."""


class OrcaWaveInvalidDataError(OrcaWavePressureError):
    """Corrupt/nonfinite data, inconsistent array shapes or broken identity."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _freeze(value, dtype=float):
    a = np.asarray(value, dtype=dtype)
    return np.frombuffer(a.tobytes(), dtype=a.dtype).reshape(a.shape)


@dataclass(frozen=True)
class OrcaWavePressureDocument:
    """One row per RESULT panel; triangle rows repeat their last vertex.

    pressure_real/imag have shape (composition, heading, period, panel).
    Metadata is immutable canonical JSON, retaining units and source identity.
    """
    vertices: np.ndarray
    centers: np.ndarray
    normals: np.ndarray
    areas: np.ndarray
    body_ids: tuple
    panel_ids: tuple
    mesh_panel_ids: tuple
    panel_types: tuple
    periods: np.ndarray
    headings: np.ndarray
    compositions: tuple
    pressure_real: np.ndarray
    pressure_imag: np.ndarray
    metadata_json: str
    content_sha256: str = ''

    def __post_init__(self):
        try:
            self._initialize()
        except OrcaWavePressureError:
            raise
        except (TypeError,ValueError,KeyError,IndexError,OverflowError) as error:
            raise OrcaWaveInvalidDataError(f'invalid pressure document data: {error}') from error

    def _initialize(self):
        for name in ('vertices','centers','normals','areas','periods','headings','pressure_real','pressure_imag'):
            raw = np.asarray(getattr(self,name))
            if raw.dtype.kind not in 'fiu':
                raise OrcaWaveInvalidDataError('real numeric document arrays required')
            object.__setattr__(self,name,_freeze(raw))
        for name in ('body_ids','panel_ids','mesh_panel_ids','panel_types','compositions'):
            object.__setattr__(self,name,tuple(getattr(self,name)))
        object.__setattr__(self,'metadata_json',_json(json.loads(self.metadata_json)))
        self._check()
        digest = hashlib.sha256(_json(self._payload()).encode()).hexdigest()
        if self.content_sha256 and self.content_sha256 != digest:
            raise OrcaWaveInvalidDataError('document content identity mismatch')
        object.__setattr__(self,'content_sha256',digest)

    @property
    def metadata(self): return json.loads(self.metadata_json)

    def _check(self):
        n=len(self.panel_ids)
        expected=(len(self.compositions),len(self.headings),len(self.periods),n)
        if (not n or self.vertices.shape!=(n,4,3) or self.centers.shape!=(n,3)
            or self.normals.shape!=(n,3) or self.areas.shape!=(n,)
            or self.pressure_real.shape!=expected or self.pressure_imag.shape!=expected
            or self.periods.ndim!=1 or self.headings.ndim!=1 or not len(self.periods) or not len(self.headings)):
            raise OrcaWaveInvalidDataError('panel/response array shapes differ')
        for name in ('body_ids','panel_ids','mesh_panel_ids','panel_types'):
            vals=getattr(self,name)
            if len(vals)!=n or any(type(v) is not int for v in vals):
                raise OrcaWaveInvalidDataError('integer panel identities required')
        if len(set(self.panel_ids))!=n or any(v<0 for v in self.panel_ids):
            raise OrcaWaveInvalidDataError('unique nonnegative result-panel IDs required')
        if not self.compositions:
            raise OrcaWaveMissingOutputError('pressure outputs unavailable')
        if len(set(self.compositions))!=len(self.compositions):
            raise OrcaWaveInvalidDataError('duplicate pressure composition')
        if any(c not in COMPOSITIONS for c in self.compositions):
            raise OrcaWaveUnsupportedFormatError('unsupported pressure composition')
        for name in ('vertices','centers','normals','areas','periods','headings','pressure_real','pressure_imag'):
            if not np.isfinite(getattr(self,name)).all():
                raise OrcaWaveInvalidDataError('nonfinite panel results')
        if (np.any(self.areas<=0) or np.any(self.periods<=0) or len(set(self.periods))!=len(self.periods)
            or len(set(self.headings % 360))!=len(self.headings)
            or not np.allclose(np.linalg.norm(self.normals,axis=1),1,rtol=0,atol=1e-8)):
            raise OrcaWaveInvalidDataError('positive areas/periods and unit normals required')
        m=self.metadata
        if not isinstance(m,dict) or not isinstance(m.get('source_sha256'),str) or re.fullmatch('[a-f0-9]{64}',m['source_sha256']) is None:
            raise OrcaWaveInvalidDataError('source SHA256 required')
        units=m.get('units')
        if not isinstance(units,dict) or set(units)!=set(SUPPORTED_UNITS):
            raise OrcaWaveUnsupportedFormatError('explicit supported length/mass/force unit vocabulary required')
        if any(type(units[key]) is not str or units[key] not in values for key,values in SUPPORTED_UNITS.items()):
            raise OrcaWaveUnsupportedFormatError(f'unsupported OrcaWave units: {units}')
        if m.get('phasor')!='exp(+i omega t)' or m.get('normals')!='out of fluid':
            raise OrcaWaveUnsupportedFormatError('unsupported provider conventions')
        origin=m.get('phase_origin')
        if (not isinstance(origin,list) or len(origin)!=3
                or any(type(v) not in (int,float) or v!=0 for v in origin)):
            raise OrcaWaveUnsupportedFormatError('explicit phase origin (0,0,0) required')

    def _payload(self):
        result={name:getattr(self,name).tolist() for name in
                ('vertices','centers','normals','areas','periods','headings','pressure_real','pressure_imag')}
        result.update({name:getattr(self,name) for name in
                       ('body_ids','panel_ids','mesh_panel_ids','panel_types','compositions','metadata_json')})
        return result

    def validate(self):
        self._check()
        if hashlib.sha256(_json(self._payload()).encode()).hexdigest()!=self.content_sha256:
            raise OrcaWaveInvalidDataError('document changed')

    def to_dict(self): return dict(type=SCHEMA,**self._payload(),content_sha256=self.content_sha256)

    @classmethod
    def from_dict(cls,data):
        try:
            d=dict(data)
            if d.pop('type',None)!=SCHEMA:
                raise OrcaWaveUnsupportedFormatError('unsupported portable pressure schema')
            if not d.get('content_sha256'): raise OrcaWaveInvalidDataError('portable pressure identity missing')
            return cls(**d)
        except OrcaWavePressureError:
            raise
        except (KeyError,TypeError,ValueError) as e:
            raise OrcaWaveInvalidDataError(f'invalid portable OrcaWave document: {e}') from e

    def save(self,path):
        self.validate()
        Path(path).write_text(_json(self.to_dict()),encoding='utf8')

    @classmethod
    def load(cls,path):
        try: return cls.from_dict(json.loads(Path(path).read_text(encoding='utf8')))
        except OrcaWavePressureError: raise
        except (OSError,ValueError) as e: raise OrcaWaveInvalidDataError(f'cannot read portable pressure document: {e}') from e


def _checkpoint(cancel):
    if cancel is not None and cancel(): raise OrcaWavePressureCancelled('OrcaWave pressure import cancelled')


def read_orcawave_pressure(path, *, cancel=None, api=None):
    """Read completed .owr or our portable JSON. No calculation or fallback solve.

    ``api`` is an injected OrcFxAPI-compatible adapter, primarily for contract tests.
    ``cancel`` returns true to cancel; exceptions raised by it propagate.
    """
    path=Path(path); _checkpoint(cancel)
    if path.suffix.lower()!='.owr':
        document=OrcaWavePressureDocument.load(path); _checkpoint(cancel); return document
    cancelled=False
    def progress(obj,percent):
        nonlocal cancelled
        cancelled=bool(cancel and cancel())
        return cancelled
    try:
        raw=path.read_bytes(); source_hash=hashlib.sha256(raw).hexdigest()
        if api is None:
            api=importlib.import_module('OrcFxAPI')
        if not callable(getattr(api,'Diffraction',None)):
            raise OrcaWaveUnsupportedFormatError('OrcFxAPI lacks diffraction result-reading capability')
        d=api.Diffraction()
        if (not callable(getattr(d,'LoadResults',None)) or not callable(getattr(api,'DLLVersion',None))
                or not hasattr(getattr(api,'DiffractionState',None),'CalculationComplete')):
            raise OrcaWaveUnsupportedFormatError('OrcFxAPI lacks completed diffraction result schema')
        d.progressHandler=progress
        d.LoadResults(str(path))
        _checkpoint(cancel)
        if d.state != api.DiffractionState.CalculationComplete:
            raise OrcaWaveMissingOutputError('OrcaWave calculation is not complete')
        g=d.panelGeometry
        if g is None or not len(g): raise OrcaWaveMissingOutputError('panel geometry unavailable')
        required_fields={'vertices','centroid','normal','area','objectId','meshFilePanelIndex','panelType'}
        if not isinstance(g,np.ndarray) or not required_fields.issubset(g.dtype.names or ()):
            raise OrcaWaveUnsupportedFormatError('unsupported OrcFxAPI panel geometry schema')
        if g.ndim!=1 or any(g[name].dtype.kind not in 'iu' for name in ('objectId','meshFilePanelIndex','panelType')):
            raise OrcaWaveInvalidDataError('invalid panel geometry identity arrays')
        vertices=np.array(g['vertices'],dtype=float,copy=True)
        triangular=np.isnan(vertices[:,3]).all(axis=1)
        vertices[triangular,3]=vertices[triangular,2]
        names=[]; values=[]
        for composition,attr in zip(COMPOSITIONS,('panelPressureDiffraction','panelPressure')):
            value=getattr(d,attr)
            if value is not None and np.asarray(value).size:
                if np.asarray(value).dtype.kind not in 'fciu':
                    raise OrcaWaveInvalidDataError('numeric pressure arrays required')
                names.append(composition); values.append(np.asarray(value,dtype=complex))
        if not values: raise OrcaWaveMissingOutputError('panel pressures unavailable; enable pressure/intermediate outputs in OrcaWave')
        depth=float(d.WaterDepth)
        metadata=dict(source_sha256=source_hash,extractor=SCHEMA,provider_version=str(api.DLLVersion()),
            units=dict(length=str(d.LengthUnits),mass=str(d.MassUnits),force=str(d.ForceUnits)),
            water_depth=None if depth>=1e300 else depth,rho=float(d.WaterDensity),gravity=float(d.g),
            phasor='exp(+i omega t)',normals='out of fluid',phase_origin=[0.,0.,0.],
            panel_pressure_semantics={'0':'body pressure, normal out of fluid',
                '2':'signed jump p_minus-p_plus, positive force along oriented sheet normal'},
            coordinate_frame='OrcaWave global',
            body_names=({str(int(row['objectId'])):str(row['objectName']) for row in g
                         if int(row['objectId'])>=0} if 'objectName' in g.dtype.names else {}))
        # LoadResults must not have changed its source while extraction took place.
        if hashlib.sha256(path.read_bytes()).hexdigest()!=source_hash:
            raise OrcaWaveInvalidDataError('source file changed during import')
        _checkpoint(cancel)
        z=np.stack(values)
        return OrcaWavePressureDocument(vertices,g['centroid'],g['normal'],g['area'],
            tuple(int(v) for v in g['objectId']),tuple(range(len(g))),
            tuple(int(v) for v in g['meshFilePanelIndex']),tuple(int(v) for v in g['panelType']),
            d.periods,d.headings,tuple(names),z.real,z.imag,_json(metadata))
    except OrcaWavePressureError: raise
    except Exception as e:
        if cancelled:
            raise OrcaWavePressureCancelled('OrcaWave pressure import cancelled') from e
        if isinstance(e,(AttributeError,ImportError)):
            raise OrcaWaveUnsupportedFormatError(f'OrcaWave API capability unavailable: {e}') from e
        if isinstance(e,(OSError,TypeError,ValueError,KeyError,IndexError,OverflowError)):
            raise OrcaWaveInvalidDataError(f'invalid OrcaWave result data: {e}') from e
        raise OrcaWavePressureError(f'OrcaWave pressure reader unavailable or invalid results ({type(e).__name__}): {e}') from e
