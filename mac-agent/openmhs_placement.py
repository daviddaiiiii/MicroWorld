"""Explicit free-position acquisition using existing bounded retake execution."""
import math
import uuid
import time
import hashlib
import shutil
import numpy as np
import openmhs_canvas as canvas
import openmhs_mosaic_store as store
import openmhs_recapture as retake
import openmhs_canvas_reference as coordinates

def plan(sid, body):
    doc=canvas.document(sid);man=store.read_manifest(sid)
    if body.get('revision')!=doc['revision']:raise ValueError('Canvas changed; reload before positioning')
    x,y=float(body['x']),float(body['y'])
    if not all(math.isfinite(v) and abs(v)<1000000 for v in (x,y)):raise ValueError('Invalid placement')
    references=[]
    for t in doc['tiles']:
        if t.get('asset'):continue
        cell=next((c for c in man['cells'] if (c['ix'],c['iy'])==(t['ix'],t['iy'])),{})
        xyz=cell.get('stage_position') or store.capture_metadata(canvas.tile_path(sid,t)).get('stage_position')
        if xyz and len(xyz)==3:references.append((t,xyz))
    ref=doc.get('reference')
    if not ref or not references:raise ValueError('No fixed physical coordinate reference available')
    # Fit actual XYZ against original placements, never the user's manual layout edits.
    residual=ref['mapping_residual_px']
    target_xy=coordinates.stage(ref,[x,y])
    anchor,xyz=min(references,key=lambda tp:sum((np.array(tp[1][:2])-target_xy)**2))
    delta=target_xy-np.array(xyz[:2])
    target=[int(round(target_xy[0])),int(round(target_xy[1])),int(xyz[2])]
    intent=body.get('intent','placement')
    if intent not in ('placement','fov-move','capture-current'):raise ValueError('Unknown FOV operation')
    if intent=='capture-current':
        actual=body.get('stage_position')
        if not isinstance(actual,list) or len(actual)!=3 or any(type(v) is not int for v in actual):raise ValueError('Current XYZ required')
        if any(abs(actual[i]-target[i])>2 for i in (0,1)):raise ValueError('FOV and current XY disagree')
        target=actual
    if max(abs(float(v)) for v in delta)>20000:raise ValueError('Placement is too far from acquired references; extend in smaller steps')
    ident=str(uuid.uuid4());asset=str(uuid.uuid4())
    tile=dict(id=asset,asset=asset,ix=min([-999999999]+[t['ix'] for t in doc['tiles']+doc.get('deleted_tiles',[])])-1,iy=0,
              x=x,y=y,w=anchor['w'],h=anchor['h'],auto_x=x,auto_y=y,hidden=False,manual=True,
              stage_position=target)
    result=dict(plan_id=ident,session_id=sid,created_at=time.time(),kind='fov-move' if intent=='fov-move' else 'placement',capture_current=intent=='capture-current',roi=None,
                canvas_revision=doc['revision'],canvas_digest=retake._digest(doc),
                layout_sha256=retake._digest(man['layout']),placement=tile,
                tiles=[dict(ix=tile['ix'],iy=tile['iy'],stage_position=target)],
                references=[dict(ix=t['ix'],iy=t['iy'],sha256=hashlib.sha256(canvas.tile_path(sid,t).read_bytes()).hexdigest()) for t,_ in references],
                mapping_residual_px=residual,anchor_id=anchor['id'],
                notice='Estimated XY from acquired tiles; Z starts at the nearest reference then autofocuses. Confirm unchanged specimen, objective and coordinate reference.')
    folder=store.session_dir(sid)/'recapture-plans';folder.mkdir(exist_ok=True)
    retake._json(folder/(ident+'.json'),result)
    return result

def commit(sid,p,replacement):
    doc=canvas.document(sid)
    folder=store.session_dir(sid);backup=folder/'canvas-original'
    if not backup.exists():
        backup.mkdir()
        for name in ('manifest.json','layout.json','slide.jpg','refined.jpg','preview.jpg'):
            if (folder/name).exists():shutil.copy2(folder/name,backup/name)
    tile=dict(p['placement'],stage_position=replacement['stage_position'],
              capture_relpath=replacement['relpath'],captured_at=replacement['captured_at'])
    doc['tiles'].append(tile);doc['revision']+=1
    canvas._write(canvas._path(sid),doc)
    canvas.render(sid,doc)
