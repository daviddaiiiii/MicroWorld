"""Non-destructive canvas layout and image adjustments. Never moves hardware."""
import base64
import copy
import io
import json
import math
import shutil
import threading
import time
import uuid
import numpy as np
import cv2
from PIL import Image
import openmhs_mosaic_store as store
import openmhs_canvas_reference as coordinates

LOCK = threading.RLock()
DEFAULTS = dict(exposure=0, contrast=1, gamma=1, colormap='natural', enhance=False)
# A browser can generate duplicate Canvas loads when a tab is restored, a
# Pi-poll fires, and the user switches views at nearly the same time.  Keep a
# very short-lived response cache so requests that arrive immediately after a
# render reuse the same JSON/base64 payload instead of decoding and composing
# every tile again.  The cache is deliberately tiny and invalidated on every
# edit; it is a request coalescer, not a second document store.
_LOAD_CACHE = {}
_LOAD_CACHE_TTL = 2.0
_LOAD_CACHE_MAX = 3
_STATUS_CACHE = {}
_STATUS_CACHE_TTL = 0.75
_STATUS_LOCK = threading.Lock()
_LOAD_INFLIGHT = {}
_LOAD_INFLIGHT_LOCK = threading.Lock()

def _path(sid):
    return store.session_dir(sid)/'canvas.json'

def _write(path, data):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,indent=2));tmp.replace(path)

def _file_stamp(path):
    try:
        stat = path.stat()
        return (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (0, 0)

def _load_cache_key(sid):
    """Return a cheap key that changes when acquisition/layout data changes."""
    folder = store.session_dir(sid)
    manifest_path = folder / 'manifest.json'
    layout_path = folder / 'layout.json'
    canvas_path = _path(sid)
    try:
        manifest = store.read_manifest(sid)
        sync = manifest.get('sync') or {}
        phase = sync.get('phase') or ''
        preview_revision = manifest.get('preview_revision') or manifest.get('live_preview_revision') or 0
        cell_count = len(manifest.get('cells') or []) + len(manifest.get('canvas_assets') or [])
    except Exception:
        phase = ''
        preview_revision = 0
        cell_count = 0
    return (
        str(sid),
        _file_stamp(canvas_path),
        _file_stamp(manifest_path),
        _file_stamp(layout_path),
        str(phase),
        int(cell_count),
        str(preview_revision),
    )

def _cached_load(sid, key):
    now = time.monotonic()
    item = _LOAD_CACHE.get(str(sid))
    if item and item[0] == key and now - item[1] <= _LOAD_CACHE_TTL:
        return item[2]
    if item:
        _LOAD_CACHE.pop(str(sid), None)
    return None

def _cache_load(sid, key, response):
    _LOAD_CACHE[str(sid)] = (key, time.monotonic(), response)
    while len(_LOAD_CACHE) > _LOAD_CACHE_MAX:
        oldest = min(_LOAD_CACHE.items(), key=lambda item: item[1][1])[0]
        _LOAD_CACHE.pop(oldest, None)

def _invalidate_load_cache(sid):
    _LOAD_CACHE.pop(str(sid), None)
    _STATUS_CACHE.pop(str(sid), None)

def _canvas_bounds(doc):
    visible = [t for t in doc.get('tiles', []) if not t.get('hidden')]
    if not visible:
        return dict(x=0, y=0, w=1, h=1)
    x0 = min(float(t['x']) for t in visible)
    y0 = min(float(t['y']) for t in visible)
    x1 = max(float(t['x']) + float(t['w']) for t in visible)
    y1 = max(float(t['y']) + float(t['h']) for t in visible)
    return dict(x=x0, y=y0, w=max(1.0, x1 - x0), h=max(1.0, y1 - y0))

def _document(sid):
    man=store.read_manifest(sid); layout=man['layout']; cw,ch=layout['canvas']
    # Pi rule-grid layouts use abstract units (tile_w=1), not image pixels.
    # Recover each axis independently: rounding canvas dimensions loses aspect ratio.
    if not _path(sid).exists() and layout.get('tiles') and (layout.get('rule_grid') or float(layout.get('tile_w') or 0) <= 2):
        t=next(t for t in layout['tiles'] if not t.get('asset') and store.tile_path(sid,t['ix'],t['iy']).is_file())
        with Image.open(store.tile_path(sid,t['ix'],t['iy'])) as im:
            cw,ch=im.width/float(t['w']),im.height/float(t['h'])
    if _path(sid).exists():
        doc=json.loads(_path(sid).read_text())
        # Repair documents saved by the old unit-grid importer without altering originals.
        acquired=[t for t in doc['tiles'] if not t.get('asset')]
        if acquired and max(t['w'] for t in acquired)<4:
            first=acquired[0]
            with Image.open(store.tile_path(sid,first['ix'],first['iy'])) as im:
                sx,sy=im.width/first['w'],im.height/first['h']
            for t in acquired:
                for k in ('x','w','auto_x'):t[k]*=sx
                for k in ('y','h','auto_y'):t[k]*=sy
        known={(t['ix'],t['iy']):t for t in doc['tiles']}
        assets={(int(a['ix']),int(a['iy'])):a for a in (man.get('canvas_assets') or [])
                if a.get('ix') is not None and a.get('iy') is not None}
        anchor=next((t for t in layout['tiles'] if (t['ix'],t['iy']) in known and not t.get('asset')),None)
        ratio=1.;dx=dy=0.
        if anchor:
            old=known[anchor['ix'],anchor['iy']]
            ratio=old['w']/(anchor['w']*cw)
            dx=old['auto_x']-anchor['x']*cw*ratio;dy=old['auto_y']-anchor['y']*ch*ratio
        # Continued scans add materials without discarding manually edited positions.
        for t in layout['tiles']:
            if (t['ix'],t['iy']) in known:continue
            if '%s,%s'%(t['ix'],t['iy']) in doc.get('removed',[]):continue
            x,y=t['x']*cw*ratio+dx,t['y']*ch*ratio+dy
            ref=doc.get('reference')
            cell=next((c for c in man.get('cells',[]) if (c['ix'],c['iy'])==(t['ix'],t['iy'])),{})
            if ref and cell.get('stage_position'):x,y=coordinates.world(ref,cell['stage_position'])
            asset=assets.get((int(t['ix']),int(t['iy'])))
            item=dict(id='%s,%s'%(t['ix'],t['iy']),ix=t['ix'],iy=t['iy'],x=x,y=y,
                      w=t['w']*cw*ratio,h=t['h']*ch*ratio,auto_x=x,auto_y=y,
                      hidden=False,manual=False,asset=bool(t.get('asset') or asset))
            if asset:
                item['capture_relpath']=asset.get('relpath')
                if asset.get('stage_position'):item['stage_position']=asset['stage_position']
                if asset.get('center_tile'):item['center_tile']=True
            doc['tiles'].append(item)
        return doc
    assets={(int(a['ix']),int(a['iy'])):a for a in (man.get('canvas_assets') or [])
            if a.get('ix') is not None and a.get('iy') is not None}
    tiles=[]
    for t in layout['tiles']:
        asset=assets.get((int(t['ix']),int(t['iy'])))
        item=dict(id='%s,%s'%(t['ix'],t['iy']),ix=t['ix'],iy=t['iy'],
                  x=t['x']*cw,y=t['y']*ch,w=t['w']*cw,h=t['h']*ch,
                  auto_x=t['x']*cw,auto_y=t['y']*ch,hidden=False,manual=False,
                  asset=bool(t.get('asset') or asset))
        if asset:
            item['capture_relpath']=asset.get('relpath')
            if asset.get('stage_position'):item['stage_position']=asset['stage_position']
            if asset.get('center_tile'):item['center_tile']=True
        tiles.append(item)
    return dict(revision=0, adjustments=dict(DEFAULTS), tiles=tiles)

def document(sid):
    doc=_document(sid)
    if store.read_manifest(sid).get('sync',{}).get('phase') in ('calibrating','scanning','stitching','refining'):
        doc['tiles']=[t for t in doc['tiles'] if tile_path(sid,t).is_file()]
    doc['reference']=coordinates.reference(sid,doc['tiles'])
    manifest=store.read_manifest(sid)
    cells=manifest.get('cells',[])+manifest.get('canvas_assets',[])
    for t in doc['tiles']:
        cell=next((c for c in cells if (c['ix'],c['iy'])==(t['ix'],t['iy'])),{})
        if cell.get('stage_position'):t['stage_position']=cell['stage_position']
        if cell.get('sha256'):t['content_revision']=cell['sha256'][:16]
        t.setdefault('opacity',1.)
        # Reconcile only automatic captures with the latest refined physical
        # mapping. A manually dragged tile is user-owned and must remain at
        # its explicit x/y, even when global registration is refreshed.
        if (
            doc.get('reference')
            and t.get('stage_position')
            and not t.get('asset')
            and not t.get('manual')
        ):
            x, y = coordinates.world(doc['reference'], t['stage_position'])
            t['auto_x'], t['auto_y'] = float(x), float(y)
            t['x'], t['y'] = float(x), float(y)
    if not _path(sid).exists() and doc['reference']:
        anchor=next((t for t in doc['tiles'] if t.get('stage_position')),None)
        if anchor:
            x,y=coordinates.world(doc['reference'],anchor['stage_position'])
            dx,dy=x-anchor['auto_x'],y-anchor['auto_y']
            for t in doc['tiles']:
                t['x']+=dx;t['auto_x']+=dx;t['y']+=dy;t['auto_y']+=dy
    return doc

def adjustments(p):
    result=dict(DEFAULTS);result.update(p or {})
    for k,lo,hi in [('exposure',-3,3),('contrast',.25,3),('gamma',.25,3)]:
        result[k]=float(result[k])
        if not math.isfinite(result[k]) or not lo<=result[k]<=hi:raise ValueError('Invalid '+k)
    if result['colormap'] not in ('natural','gray','viridis','magma'):raise ValueError('Invalid colormap')
    result['enhance']=bool(result['enhance'])
    return result

def adjusted(a,p):
    a=np.clip((a.astype(np.float32)/255*2**p['exposure']-.5)*p['contrast']+.5,0,1)**(1/p['gamma'])
    a=np.uint8(a*255)
    if p['enhance']:
        a=cv2.bilateralFilter(a,5,18,18)
        a=cv2.addWeighted(a,1.25,cv2.GaussianBlur(a,(0,0),1),-.25,0)
    if p['colormap']!='natural':
        gray=cv2.cvtColor(a,cv2.COLOR_RGB2GRAY)
        if p['colormap']=='gray':a=cv2.cvtColor(gray,cv2.COLOR_GRAY2RGB)
        else:a=cv2.cvtColor(cv2.applyColorMap(gray,{'viridis':cv2.COLORMAP_VIRIDIS,'magma':cv2.COLORMAP_MAGMA}[p['colormap']]),cv2.COLOR_BGR2RGB)
    return a

def tile_path(sid,t):
    if t.get('capture_relpath'):
        path=(store.session_dir(sid)/t['capture_relpath']).resolve()
        if store.session_dir(sid).resolve() not in path.parents:raise ValueError('Invalid capture path')
        return path
    if t.get('asset'):
        return store.session_dir(sid)/'canvas-assets'/(str(uuid.UUID(t['asset']))+'.jpg')
    return store.tile_path(sid,int(t['ix']),int(t['iy']))

def data_url(raw):return 'data:image/jpeg;base64,'+base64.b64encode(raw).decode()

def overview_bytes(raw):
    """Return the bounded browser overview for a published Canvas render."""
    with Image.open(io.BytesIO(raw)) as source:
        image=source.convert('RGB')
        edge=int(getattr(store,'MAC_LIVE_PREVIEW_MAX_EDGE',2048))
        if image.width>edge or image.height>edge:
            resampling=getattr(Image,'Resampling',Image)
            image.thumbnail((edge,edge),resampling.BILINEAR)
        buf=io.BytesIO();image.save(buf,format='JPEG',quality=82,optimize=True)
        image.close()
        return buf.getvalue()

def load(sid, hydrate_tiles=False):
    """Load Canvas metadata without blocking on every tile decode.

    The Slide is sourced from the Mac refined preview below.  Tile images are
    requested by the browser as cached thumbnails and promoted to full-size
    only when a tile is selected.  Keeping the old opt-in hydration argument
    makes this safe for legacy callers that explicitly need data URLs.
    """
    doc=document(sid)
    if hydrate_tiles:
        for t in doc['tiles']:
            _load_tile_url(sid, doc, t)
    return doc


def _published_slide(sid):
    """Return the Mac-owned fused overview when the Canvas is pristine."""
    folder = store.session_dir(sid)
    refined = folder / 'refined.jpg'
    # Recover old records whose sync overwrote the published preview.
    if not store.read_manifest(sid).get('refined') and refined.is_file():
        with Image.open(refined) as im:
            im.thumbnail((store.MAC_LIVE_PREVIEW_MAX_EDGE, store.MAC_LIVE_PREVIEW_MAX_EDGE))
            output = io.BytesIO()
            im.convert('RGB').save(output, format='JPEG', quality=90)
            return output.getvalue()
    path = folder / 'preview.jpg'
    if not path.is_file() or path.stat().st_size <= 32:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _is_pristine_document(doc):
    """Whether the presentation still matches the refined source Slide."""
    adjustments = doc.get('adjustments') or {}
    if (
        float(adjustments.get('exposure', 0) or 0) != 0
        or float(adjustments.get('contrast', 1) or 1) != 1
        or float(adjustments.get('gamma', 1) or 1) != 1
        or adjustments.get('colormap', 'natural') != 'natural'
        or bool(adjustments.get('enhance'))
        or bool(doc.get('layered'))
    ):
        return False
    for tile in doc.get('tiles') or []:
        if tile.get('hidden') or tile.get('manual'):
            return False
        if abs(float(tile.get('opacity', 1) or 1) - 1) > 1e-6:
            return False
        if tile.get('adjustments'):
            return False
        if abs(float(tile.get('x', 0)) - float(tile.get('auto_x', tile.get('x', 0)))) > 0.5:
            return False
        if abs(float(tile.get('y', 0)) - float(tile.get('auto_y', tile.get('y', 0)))) > 0.5:
            return False
    return True

def _load_tile_url(sid, doc, t):
    """Refresh one browser tile preview without compositing the whole Slide."""
    with Image.open(tile_path(sid,t)) as im:a=adjusted(np.asarray(im.convert('RGB')),t.get('adjustments') or doc['adjustments'])
    buf=io.BytesIO();Image.fromarray(a).save(buf,format='JPEG',quality=93)
    t['url']=data_url(buf.getvalue())

def load_tile_previews(sid, doc, ids=None):
    wanted=set(ids or [])
    result=[]
    for t in doc['tiles']:
        if wanted and t['id'] not in wanted: continue
        _load_tile_url(sid, doc, t)
        result.append({'id':t['id'],'url':t['url']})
    return result

def load_legacy(sid):
    """Compatibility wrapper for callers that expect a fully hydrated document."""
    doc=document(sid)
    for t in doc['tiles']:
        with Image.open(tile_path(sid,t)) as im:a=adjusted(np.asarray(im.convert('RGB')),t.get('adjustments') or doc['adjustments'])
        buf=io.BytesIO();Image.fromarray(a).save(buf,format='JPEG',quality=93)
        t['url']=data_url(buf.getvalue())
    return doc

def render(sid,doc,publish=True):
    visible=[t for t in doc['tiles'] if not t.get('hidden')]
    if not visible:
        buf=io.BytesIO();Image.new('RGB',(1,1),(245,245,245)).save(buf,format='JPEG')
        if publish:
            folder=store.session_dir(sid)
            for name in ('slide.jpg','refined.jpg','preview.jpg'):(folder/name).write_bytes(buf.getvalue())
            man=store.read_manifest(sid);man.update(layout=dict(canvas=[1,1],tiles=[],tile_w=1),canvas_edited=True,canvas_reference_locked=True,canvas_revision=doc['revision'])
            man['canvas_assets']=[];man['canvas_trashed_coordinates']=doc.get('trashed_coordinates',[])
            man['preview_revision']=int(time.time()*1000)
            store.write_manifest(sid,man);_write(folder/'layout.json',man['layout'])
            try:store.ensure_pyramid(sid,force=True)
            except Exception:pass
        return dict(slide=data_url(buf.getvalue()),bounds=dict(x=0,y=0,w=1,h=1))
    x0=min(t['x'] for t in visible);y0=min(t['y'] for t in visible)
    width=max(t['x']+t['w'] for t in visible)-x0
    height=max(t['y']+t['h'] for t in visible)-y0
    scale=min(1,3200/max(width,height))
    cw,ch=math.ceil(width*scale)+1,math.ceil(height*scale)+1
    acc=np.zeros((ch,cw,3),np.float32);weights=np.zeros((ch,cw),np.float32)
    for t in visible:
        w,h=max(1,round(t['w']*scale)),max(1,round(t['h']*scale))
        x,y=round((t['x']-x0)*scale),round((t['y']-y0)*scale)
        with Image.open(tile_path(sid,t)) as im:a=adjusted(np.asarray(im.convert('RGB').resize((w,h))),t.get('adjustments') or doc['adjustments']).astype(np.float32)
        yy,xx=np.mgrid[:h,:w];weight=np.maximum(1,np.minimum.reduce([xx,w-1-xx,yy,h-1-yy])).astype(np.float32)
        opacity=t.get('opacity',1.)
        if doc.get('layered'):
            alpha=opacity
            acc[y:y+h,x:x+w]=a*alpha+acc[y:y+h,x:x+w]*(1-alpha)
            weights[y:y+h,x:x+w]=alpha+weights[y:y+h,x:x+w]*(1-alpha)
        else:
            weight*=opacity
            acc[y:y+h,x:x+w]+=a*weight[:,:,None];weights[y:y+h,x:x+w]+=weight
    a=acc/np.maximum(weights,1e-6)[:,:,None]
    if doc.get('layered'):a=acc+245*(1-weights[:,:,None])
    a=np.uint8(np.clip(a,0,255))
    a[weights==0]=245
    buf=io.BytesIO();Image.fromarray(a).save(buf,format='JPEG',quality=93)
    layout={'canvas':[cw,ch],'tile_w':round(visible[0]['w']*scale),'tile_h':round(visible[0]['h']*scale),
            'tiles':[dict(ix=t['ix'],iy=t['iy'],asset=t.get('asset'),x=(t['x']-x0)*scale/cw,y=(t['y']-y0)*scale/ch,
                          w=t['w']*scale/cw,h=t['h']*scale/ch) for t in visible]}
    raw=buf.getvalue()
    if publish:
        folder=store.session_dir(sid)
        for name in ('slide.jpg','refined.jpg'):
            tmp=folder/(name+'.tmp');tmp.write_bytes(raw);tmp.replace(folder/name)
        tmp=folder/'preview.jpg.tmp';tmp.write_bytes(overview_bytes(raw));tmp.replace(folder/'preview.jpg')
        man=store.read_manifest(sid);man['layout']=layout;man['canvas_edited']=True;man['canvas_reference_locked']=True
        man['preview_revision']=int(time.time()*1000)
        man['canvas_trashed_coordinates']=doc.get('trashed_coordinates',[])
        man['canvas_revision']=doc['revision'];man['refined']=True
        # Imported materials must never participate in motor-grid occupancy/hydration.
        old_assets={(int(a['ix']),int(a['iy'])):a for a in (man.get('canvas_assets') or [])
                    if a.get('ix') is not None and a.get('iy') is not None}
        assets=[]
        for t in doc['tiles']:
            if not t.get('asset'):continue
            old=old_assets.get((int(t['ix']),int(t['iy'])),{})
            assets.append(dict(ix=t['ix'],iy=t['iy'],relpath=t.get('capture_relpath') or old.get('relpath') or 'canvas-assets/'+t['asset']+'.jpg',
                               stage_position=t.get('stage_position') or old.get('stage_position'),
                               position_source=old.get('position_source') or ('free-position-capture' if t.get('stage_position') else 'import-no-stage-coordinate'),
                               center_tile=bool(t.get('center_tile') or old.get('center_tile')),
                               extra=bool(t.get('extra') or old.get('extra'))))
        # A scan may finish just before the Mac refine writes the hub into
        # layout.json.  Preserve such an asset across an early Canvas render;
        # once refinement publishes layout it will become an editable tile.
        represented={(int(a['ix']),int(a['iy'])) for a in assets}
        for old in (man.get('canvas_assets') or []):
            key=(int(old.get('ix')),int(old.get('iy')))
            if key not in represented:
                assets.append(old)
        man['canvas_assets']=assets
        store.write_manifest(sid,man);_write(folder/'layout.json',layout)
        try:store.ensure_pyramid(sid,force=True)
        except Exception:pass
    return dict(slide=data_url(raw),bounds=dict(x=x0,y=y0,w=cw/scale,h=ch/scale))

def _status_response(sid, manifest=None):
    """Return image-free Canvas state for live acquisition views.

    Status must not wait behind a full Slide render. A scan can add a tile
    while another browser tab is decoding the previous Slide, so this path is
    intentionally cheap and relies on atomically-written document files.
    """
    with _STATUS_LOCK:
        now=time.monotonic()
        cached=_STATUS_CACHE.get(str(sid))
        if cached and now-cached[0] <= _STATUS_CACHE_TTL:
            return cached[1]
        manifest=manifest or store.read_manifest(sid)
        sync=manifest.get('sync') or {}
        phase=sync.get('phase')
        refine=store.refine_status(sid)
        doc=document(sid)
        result=dict(
            revision=doc.get('revision', 0),
            adjustments=doc.get('adjustments', dict(DEFAULTS)),
            tiles=[{k:v for k,v in t.items() if k != 'url'} for t in doc.get('tiles', [])],
            reference=doc.get('reference'),
            bounds=_canvas_bounds(doc),
            locked=phase in ('calibrating','scanning','stitching','refining'),
            phase=phase or 'idle',
            refined=bool(manifest.get('refined') or refine.get('refined')),
            refine_phase=refine.get('phase') or 'idle',
            sync_complete=bool(sync.get('complete')),
        )
        _STATUS_CACHE[str(sid)]=(time.monotonic(),result)
        return result

def _load_response(sid, locked):
    """Render one full Canvas per session and coalesce duplicate requests."""
    key=_load_cache_key(sid)
    cached=_cached_load(sid,key)
    if cached is not None:
        return cached
    with _LOAD_INFLIGHT_LOCK:
        cached=_cached_load(sid,key)
        if cached is not None:
            return cached
        if str(sid) in _LOAD_INFLIGHT:
            response=dict(_status_response(sid))
            response['deferred_load']=True
            response['load_in_flight']=True
            response['message']='Canvas render already in progress; live status returned'
            return response
        token=object()
        _LOAD_INFLIGHT[str(sid)]=token
    try:
        with LOCK:
            manifest=store.read_manifest(sid)
            phase=(manifest.get('sync') or {}).get('phase')
            locked=phase in ('calibrating','scanning','stitching','refining')
            if locked:
                response=dict(_status_response(sid,manifest))
                response['deferred_load']=True
                return response
            key=_load_cache_key(sid)
            cached=_cached_load(sid,key)
            if cached is not None:
                return cached
            doc=load(sid, hydrate_tiles=False)
            # A pristine Canvas must show the actual Mac refine/fusion result,
            # not a second browser-side weighted paste of raw tiles.  More
            # importantly, do not decode every source tile just to throw that
            # temporary composition away afterwards.
            manifest = store.read_manifest(sid)
            pristine = (
                store.refine_status(sid).get('refined')
                and _is_pristine_document(doc)
            )
            fused = _published_slide(sid) if pristine else None
            if fused:
                response=dict(
                    doc,
                    locked=locked,
                    refined=True,
                    slide=data_url(fused),
                    bounds=_canvas_bounds(doc),
                )
            else:
                response=dict(doc,locked=locked,**render(sid,doc,publish=False))
            _cache_load(sid,key,response)
            return response
    finally:
        with _LOAD_INFLIGHT_LOCK:
            if _LOAD_INFLIGHT.get(str(sid)) is token:
                _LOAD_INFLIGHT.pop(str(sid),None)

def request(sid,body):
    # Keep live status out of the global render lock. Previously a slow
    # action=load could block status behind it; the Pi proxy then reported a
    # misleading HTTP 502 even though the scan and Mac sync were healthy.
    action=body.get('action','load')
    manifest=store.read_manifest(sid)
    sync=manifest.get('sync') or {}
    phase=sync.get('phase')
    locked=phase in ('calibrating','scanning','stitching','refining')
    if action in ('status','live'):
        return _status_response(sid,manifest)
    # A full render is never useful while acquisition is changing the tile
    # set. Return the live document immediately instead of making the Pi wait
    # for every tile to be decoded and base64-encoded.
    if action=='load' and locked:
        response=dict(_status_response(sid,manifest))
        response['deferred_load']=True
        response['message']='Canvas is updating; live tile status returned instead of a full Slide render'
        return response
    if action=='load':
        return _load_response(sid,locked)
    with LOCK:
        manifest=store.read_manifest(sid)
        phase=(manifest.get('sync') or {}).get('phase')
        locked=phase in ('calibrating','scanning','stitching','refining')
        if locked:
            raise ValueError('Scanning: canvas edits are locked until acquisition completes')
        from openmhs_recapture import active_sessions
        if sid in active_sessions or store.refine_status(sid).get('phase')=='refining':
            raise ValueError('Wait for the active retake or refinement')
        current=document(sid)
        if body.get('revision')!=current['revision']:raise ValueError('Canvas changed on another device. Reopen before editing.')
        if action=='placement-plan':
            from openmhs_placement import plan
            return plan(sid,body)
        if action=='trash-canvas':
            if body.get('confirm') is not True:raise ValueError('Confirm moving the entire canvas to recovery trash')
            from openmhs_views import trash_record
            return trash_record(sid)
        doc=copy.deepcopy(current)
        moves=[]
        if action=='delete':
            ids=body.get('tile_ids') or []
            if not ids or not set(ids)<=set(t['id'] for t in doc['tiles']):raise ValueError('Unknown tile selection')
            if body.get('storage') not in ('keep','trash'):raise ValueError('Choose keep files or move files to recovery trash')
            removed=[t for t in doc['tiles'] if t['id'] in ids]
            doc['tiles']=[t for t in doc['tiles'] if t['id'] not in ids]
            doc['removed']=list(set(doc.get('removed',[])+ids))
            doc.setdefault('deleted_tiles',[]).extend(removed)
            if body['storage']=='trash':
                trash=store.session_dir(sid)/'canvas-trash'/str(uuid.uuid4());trash.mkdir(parents=True)
                for t in removed:
                    source=tile_path(sid,t).resolve()
                    if store.session_dir(sid).resolve() not in source.parents:raise ValueError('Unsafe tile path')
                    if any(tile_path(sid,other).resolve()==source for other in doc['tiles']):raise ValueError('Tile file is still used by another layer')
                    if source not in [s for s,_ in moves]:moves.append((source,trash/(str(len(moves))+'.jpg')))
                _write(trash/'recovery.json',dict(tiles=removed,files=[dict(source=str(s),trash=str(d)) for s,d in moves]))
                doc.setdefault('trashed_coordinates',[]).extend([[t['ix'],t['iy']] for t in removed])
        elif action=='import':
            raw=base64.b64decode(body['image'].split(',',1)[1],validate=True)
            if len(raw)>8*1024*1024:raise ValueError('Use an image smaller than 8 MB')
            with Image.open(io.BytesIO(raw)) as im:
                if im.width*im.height>24000000:raise ValueError('Image exceeds 24 megapixels')
                im=im.convert('RGB');im.thumbnail((1600,1600));asset=str(uuid.uuid4())
                folder=store.session_dir(sid)/'canvas-assets';folder.mkdir(exist_ok=True)
                im.save(folder/(asset+'.jpg'),quality=95)
                w,h=im.size
            ix=min([-999999999]+[t['ix'] for t in doc['tiles']+doc.get('deleted_tiles',[])])-1
            x=max([0]+[t['x']+t['w'] for t in doc['tiles']])+24
            doc['tiles'].append(dict(id=asset,asset=asset,ix=ix,iy=0,x=x,y=0,w=w,h=h,auto_x=x,auto_y=0,manual=True,hidden=False))
        elif action=='save':
            candidate=body['document'];old={t['id']:t for t in current['tiles']}
            if set(t['id'] for t in candidate['tiles'])!=set(old):raise ValueError('Tile set changed; reload canvas')
            for t in candidate['tiles']:
                dest=old[t['id']]
                for k in ('x','y'):
                    v=float(t[k])
                    if not math.isfinite(v) or abs(v)>1000000:raise ValueError('Invalid canvas position')
                    dest[k]=v
                dest['hidden']=bool(t.get('hidden'));dest['manual']=bool(t.get('manual'))
                dest['opacity']=float(t.get('opacity',1))
                if not math.isfinite(dest['opacity']) or not 0<=dest['opacity']<=1:raise ValueError('Invalid opacity')
                dest['adjustments']=adjustments(t['adjustments']) if t.get('adjustments') else None
            doc['tiles']=[old[t['id']] for t in candidate['tiles']]
            doc['layered']=bool(candidate.get('layered',False))
            doc['adjustments']=adjustments(candidate.get('adjustments'))
            if doc['tiles'] and not any(not t.get('hidden') for t in doc['tiles']):raise ValueError('Keep at least one visible tile, or delete all tiles')
        else:raise ValueError('Unknown canvas action')
        # Preserve the pre-edit rendering and layout once; never alter acquisition files.
        folder=store.session_dir(sid);backup=folder/'canvas-original'
        if not backup.exists():
            backup.mkdir()
            for name in ('manifest.json','layout.json','slide.jpg','refined.jpg','preview.jpg'):
                if (folder/name).exists():shutil.copy2(folder/name,backup/name)
        doc['revision']+=1
        # Position-only tile drags do not need an immediate full raster
        # render. The browser already has the live tile layers; it will ask
        # for a fresh Slide when switching back to Slide view. This avoids
        # decoding, compositing, and base64-encoding every tile on pointer-up.
        if action == 'save' and body.get('defer_render') is True:
            _write(_path(sid),doc)
            _invalidate_load_cache(sid)
            previews=load_tile_previews(sid,doc,body.get('preview_tile_ids')) if body.get('preview_tile_ids') else []
            return dict(document=doc, deferred=True, tiles=previews, slide=None, bounds=current.get('bounds'))
        # Validate decoding/rendering before moving any requested source files.
        if moves:render(sid,doc,publish=False)
        moved=[]
        try:
            for source,dest in moves:shutil.move(str(source),str(dest));moved.append((source,dest))
            result=render(sid,doc)
            _write(_path(sid),doc)
            _invalidate_load_cache(sid)
        except Exception:
            for source,dest in reversed(moved):shutil.move(str(dest),str(source))
            raise
        _invalidate_load_cache(sid)
        return dict(document=load(sid),**result)
