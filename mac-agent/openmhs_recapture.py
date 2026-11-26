"""User/agent-requested ROI retakes. Immutable plans, versioned tiles, no auto policy."""
import hashlib,json,threading,time,uuid
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler
from urllib.parse import urlparse
import openmhs_mosaic_store as store

_lock=threading.Lock()
active_sessions=set()
HTTP=build_opener(ProxyHandler({}))

def _digest(obj):return hashlib.sha256(json.dumps(obj,sort_keys=True).encode()).hexdigest()

def _json(path,data):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,indent=2));tmp.replace(path)

def _request(path,body=None):
    base=store._microscope_base().rstrip('/')
    req=Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    with HTTP.open(req,timeout=15) as r:return json.load(r)

def _id(value):return str(uuid.UUID(value))

def plan(sid,roi):
    values=[float(roi[k]) for k in ('x','y','width','height')]
    x,y,w,h=values
    if not (0<=x<1 and 0<=y<1 and 0<w<=1-x+1e-6 and 0<h<=1-y+1e-6):raise ValueError('ROI must be normalized to the displayed Slide')
    man=store.read_manifest(sid);layout=man.get('layout') or {}
    chosen=[]
    for t in layout.get('tiles',[]):
        if min(x+w,t['x']+t['w'])<=max(x,t['x']) or min(y+h,t['y']+t['h'])<=max(y,t['y']):continue
        ix,iy=int(t['ix']),int(t['iy']);p=store.tile_path(sid,ix,iy)
        cell=next(c for c in man['cells']+man.get('canvas_assets',[]) if (int(c['ix']),int(c['iy']))==(ix,iy))
        metadata=dict(cell)
        recovered=store.capture_metadata(p)
        for key,val in recovered.items():
            if key not in metadata:metadata[key]=val
        if not metadata.get('stage_position'):raise ValueError('Original XYZ unavailable for tile %s,%s; no guessed Z allowed'%(ix,iy))
        chosen.append({'ix':ix,'iy':iy,'stage_position':metadata['stage_position'],
                       'sha256':recovered['sha256'],'relpath':cell['relpath']})
    if not chosen:raise ValueError('No archived tiles intersect ROI')
    if len(chosen)>16:raise ValueError('Choose a smaller ROI (at most 16 intersecting tiles)')
    result={'plan_id':str(uuid.uuid4()),'session_id':sid,'roi':roi,'tiles':chosen,
            'layout_sha256':_digest(layout),'created_at':time.time(),
            'notice':'Retakes every intersecting source tile. Confirm unchanged specimen and coordinate reference.'}
    folder=store.session_dir(sid)/'recapture-plans';folder.mkdir(exist_ok=True)
    _json(folder/(result['plan_id']+'.json'),result)
    return result

def status(sid,job_id):
    return json.loads((store.session_dir(sid)/'recapture-plans'/(_id(job_id)+'.status.json')).read_text())

def _validate(sid,p):
    man=store.read_manifest(sid)
    if _digest(man.get('layout') or {})!=p['layout_sha256']:raise ValueError('Slide layout changed; draw ROI again')
    if p.get('kind') in ('placement','fov-move'):
        import openmhs_canvas as canvas
        if _digest(canvas.document(sid))!=p['canvas_digest']:raise ValueError('Canvas changed; position the new tile again')
    for t in p.get('references',p['tiles']):
        if hashlib.sha256(store.tile_path(sid,t['ix'],t['iy']).read_bytes()).hexdigest()!=t['sha256']:
            raise ValueError('Source tile changed; create a new plan')
    return man

def start(sid,plan_id,confirm_same_sample=False):
    import openmhs_canvas as canvas
    with canvas.LOCK:
        return _start(sid,plan_id,confirm_same_sample)

def _start(sid,plan_id,confirm_same_sample=False):
    if confirm_same_sample is not True:raise ValueError('Confirm unchanged specimen and coordinate reference')
    if store.read_manifest(sid).get('sync',{}).get('phase') in ('calibrating','scanning','stitching','refining'):
        raise ValueError('Wait for scanning to finish before retaking or adding a tile')
    plan_id=_id(plan_id);folder=store.session_dir(sid)/'recapture-plans'
    p=json.loads((folder/(plan_id+'.json')).read_text())
    if time.time()-p['created_at']>900:raise ValueError('Plan expired; draw ROI again')
    status_path=folder/(plan_id+'.status.json')
    # Repeated HTTP submission never repeats hardware execution.
    if status_path.exists():return status(sid,plan_id)
    _validate(sid,p)
    if not _lock.acquire(False):raise ValueError('Another explicit retake is running')
    try:
        if status_path.exists():
            _lock.release()
            return status(sid,plan_id)
        if store.refine_status(sid).get('phase')=='refining':raise ValueError('Wait for active refinement')
        state={'job_id':plan_id,'phase':'running','completed':0,'total':len(p['tiles'])}
        _json(status_path,state)
        active_sessions.add(sid)
    except Exception:
        _lock.release();raise

    def work():
        try:
            revision=store.session_dir(sid)/'tile-revisions'/plan_id;revision.mkdir(parents=True)
            replacements=[]
            for t in p['tiles']:
                action=_request('/api/v2/extensions/org.openmhs.scan-mosaic/recapture_tile',
                                {'stage_position':t['stage_position'],'confirm_same_sample':True,'autofocus':not p.get('capture_current',False),
                                 'fast_autofocus':True,'autofocus_dz':1000,
                                 'move_only':p.get('kind')=='fov-move','capture_only':p.get('capture_current',False)})
                href=urlparse(action['href']).path
                state['action_href']=action['href'];_json(status_path,state)
                deadline=time.monotonic()+300
                while action.get('status') not in ('completed','error','failed','cancelled'):
                    if time.monotonic()>deadline:raise RuntimeError('Action timeout; do not retry blindly; inspect action_href')
                    time.sleep(.5);action=_request(href)
                if action['status']!='completed':raise RuntimeError(str(action.get('output') or action['status']))
                result=action['output']
                if p.get('kind')=='fov-move':
                    state.update(phase='completed',completed=1,stage_position=result['stage_position'],focus=result.get('focus',{}))
                    return
                path=revision/('t_%s_%s.jpg'%(t['ix'],t['iy']))
                with HTTP.open(store._microscope_base().rstrip('/')+'/api/v2/extensions/org.openmhs.scan-mosaic/recapture_image?token='+result['token'],timeout=20) as r:path.write_bytes(r.read())
                from PIL import Image
                with Image.open(path) as im:im.verify()
                replacements.append(dict(t,relpath=str(path.relative_to(store.session_dir(sid))),
                                         stage_position=result['stage_position'],sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                         captured_at=result['captured_at'],position_source='explicit-retake',focus=result['focus']))
                state['completed']+=1;_json(status_path,state)
            man=_validate(sid,p)
            if p.get('kind')=='placement':
                import openmhs_placement
                openmhs_placement.commit(sid,p,replacements[0])
                state['phase']='completed'
                return
            man.setdefault('tile_revision_history',[]).append({'job_id':plan_id,'previous_cells':man['cells'],'roi':p['roi']})
            lookup={(r['ix'],r['iy']):r for r in replacements}
            man['cells']=[lookup.get((c['ix'],c['iy']),c) for c in man['cells']]
            if man.get('canvas_assets'):
                import openmhs_canvas as canvas
                doc=canvas.document(sid)
                for t in doc['tiles']:
                    r=lookup.get((t['ix'],t['iy']))
                    if r and t.get('asset'):
                        t.update(capture_relpath=r['relpath'],stage_position=r['stage_position'])
                canvas._write(canvas._path(sid),doc)
            store.write_manifest(sid,man)
            state['phase']='refining';_json(status_path,state)
            store._run_refine(sid)
            if store.refine_status(sid).get('phase')=='error':raise RuntimeError('Retakes saved; restitch failed; previous Slide retained')
            state['phase']='completed'
        except Exception as exc:
            state.update(phase='error',error=str(exc))
        finally:
            _json(status_path,state);active_sessions.discard(sid);_lock.release()
    threading.Thread(target=work,name='explicit-retake-'+plan_id,daemon=True).start()
    return state
