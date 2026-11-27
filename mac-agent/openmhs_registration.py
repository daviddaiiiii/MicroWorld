"""Translation-only microscopy registration and focus-aware fusion."""
import time
import os
import cv2
import numpy as np
from PIL import Image


# This limit belongs to the Mac-side final refine, not the live browser
# monitor.  The microscope frames are currently 640 px wide, so this keeps
# registration at native resolution while avoiding an accidental memory blow
# up if a future camera produces much larger frames.
HQ_REGISTRATION_MAX_WIDTH = max(
    640, int(os.environ.get('OPENMHS_HQ_REGISTRATION_MAX_WIDTH', '1024'))
)


def gray(im):
    a = np.asarray(im)
    return cv2.cvtColor(a.astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32) if a.ndim == 3 else a.astype(np.float32)


def patches(a, b, dx, dy):
    dx, dy = int(round(dx)), int(round(dy))
    h, w = a.shape
    x0, y0, x1, y1 = max(0, dx), max(0, dy), min(w, w+dx), min(h, h+dy)
    if x1-x0 < 24 or y1-y0 < 24: return None
    return a[y0:y1, x0:x1], b[y0-dy:y1-dy, x0-dx:x1-dx]


def ncc(a, b):
    x, y = a.ravel()-a.mean(), b.ravel()-b.mean()
    return float(np.dot(x,y)/(np.linalg.norm(x)*np.linalg.norm(y)+1e-9))


def pair_score(a, b, delta):
    p = patches(a, b, *delta)
    if p is None or min(float(p[0].std()),float(p[1].std())) < 2: return 0.
    return ncc(*p)


def features(g):
    sift = cv2.SIFT_create(nfeatures=1600, contrastThreshold=.02)
    return sift.detectAndCompute(np.clip(g,0,255).astype(np.uint8), None)


def pair(a, b, expected, mode='hybrid', fa=None, fb=None):
    h,w = a.shape
    candidates = [('stage',np.asarray(expected,dtype=float))]
    # High-pass phase correlation is insensitive to smooth illumination gradients.
    aa = a-cv2.GaussianBlur(a,(0,0),3)
    bb = b-cv2.GaussianBlur(b,(0,0),3)
    shift,response = cv2.phaseCorrelate(aa,bb)
    if np.isfinite(shift).all():
        # FFT correlation is periodic: low-overlap true shifts wrap by width/height.
        for ox in (-w,0,w):
            for oy in (-h,0,h):
                candidates.append(('phase',-np.asarray(shift)+[ox,oy]))
    phase_best=max(pair_score(a,b,d) for _,d in candidates)
    if mode == 'hybrid' and phase_best < .8:
        # Narrow boundary patches still fit when actual overlap is much smaller
        # than the old stage calibration predicts. Validate on the entire overlap.
        horizontal=abs(expected[0])>=abs(expected[1])
        pw,ph=(32,64) if horizontal else (64,32)
        for frac in (.2,.5,.8):
            tx=(8 if expected[0]<0 else w-pw-8) if horizontal else int((w-pw)*frac)
            ty=int((h-ph)*frac) if horizontal else (8 if expected[1]<0 else h-ph-8)
            template=aa[ty:ty+ph,tx:tx+pw]
            if template.shape!=(ph,pw) or template.std()<2:continue
            r=cv2.matchTemplate(bb,template,cv2.TM_CCOEFF_NORMED)
            for _ in range(2):
                _,_,_,loc=cv2.minMaxLoc(r)
                candidates.append(('boundary-template',np.array([tx-loc[0],ty-loc[1]],float)))
                xx,yy=loc;r[max(0,yy-8):yy+9,max(0,xx-8):xx+9]=-1
        ka,da = fa if fa is not None else features(a)
        kb,db = fb if fb is not None else features(b)
        if da is not None and db is not None and len(db)>1:
            matches=cv2.BFMatcher().knnMatch(da,db,k=2)
            deltas=np.array([np.array(ka[m.queryIdx].pt)-np.array(kb[m.trainIdx].pt)
                             for mm in matches if len(mm)==2 for m,n in [mm] if m.distance < .72*n.distance])
            if len(deltas)>=4:
                # Translation consensus: no arbitrary scale/shear of microscope tissue.
                counts=np.sum(np.linalg.norm(deltas[:,None]-deltas[None,:],axis=2)<3,axis=1)
                seed=deltas[np.argmax(counts)]
                inliers=deltas[np.linalg.norm(deltas-seed,axis=1)<3]
                if len(inliers)>=4: candidates.append(('sift',np.median(inliers,axis=0)))
        # Two-dimensional template search; allow cross-axis drift unlike full-width strips.
        dx,dy=map(lambda v:int(round(v)),expected)
        x0,y0,x1,y1=max(0,dx),max(0,dy),min(w,w+dx),min(h,h+dy)
        pw,ph=min(160,max(0,x1-x0-16)),min(160,max(0,y1-y0-16))
        if min(pw,ph)>=32:
            tx,ty=(x0+x1-pw)//2,(y0+y1-ph)//2
            template=aa[ty:ty+ph,tx:tx+pw]
            if template.std()>2:
                result=cv2.matchTemplate(bb,template,cv2.TM_CCOEFF_NORMED)
                for _ in range(3):
                    _,_,_,loc=cv2.minMaxLoc(result)
                    candidates.append(('template',np.array([tx-loc[0],ty-loc[1]],float)))
                    xx,yy=loc
                    result[max(0,yy-8):yy+9,max(0,xx-8):xx+9]=-1
    # Repeated nuclei and seminiferous-tubule texture can produce a very high
    # SIFT/NCC score at an unrelated location.  Registration is allowed to
    # correct the stage prior, but not to search the whole tile.  This keeps
    # the image evidence local to the physically plausible overlap.
    max_drift = max(48.0, 0.30 * min(w, h))
    ranked=[]
    for name,d in candidates:
        if not np.isfinite(d).all() or abs(d[0])>w-24 or abs(d[1])>h-24: continue
        if float(np.linalg.norm(np.asarray(d, dtype=float) - np.asarray(expected, dtype=float))) > max_drift:
            continue
        sc=pair_score(a,b,d)
        ranked.append((sc,name,d))
    ranked.sort(key=lambda x:x[0],reverse=True)
    sc,name,d=ranked[0] if ranked else (0.,'stage',np.array(expected,float))
    # Subpixel ECC reports the sampling shift in moving coordinates: SUBTRACT it.
    p=patches(a,b,*d)
    if p is not None and sc>.15:
        try:
            warp=np.eye(2,3,dtype=np.float32)
            _,warp=cv2.findTransformECC(p[0],p[1],warp,cv2.MOTION_TRANSLATION,
                                       (cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,30,1e-4),None,3)
            refined=np.round(d)-warp[:,2]
            score=pair_score(a,b,refined)
            if score>=sc-.005 and np.linalg.norm(refined-d)<8:
                d,sc=refined,score
        except cv2.error: pass
    # Confidence controls relative graph weights, never whether a tile is included.
    if sc<.1: d,name=np.array(expected,float),'stage'
    return {'delta':d.tolist(),'ncc':float(sc),'method':name,
            'candidates':[{'method':nm,'ncc':float(s),'delta':v.tolist()} for s,nm,v in ranked]}


def solve(keys, edges, prior):
    n=len(keys); index={k:i for i,k in enumerate(keys)}
    if n==1:return dict(prior)
    rows=[]; rhs=[]; weights=[]
    for e in edges:
        row=np.zeros(n);row[index[e['a']]]=-1;row[index[e['b']]]=1
        rows.append(row);rhs.append(e['delta'])
        weights.append(max(.002,max(0,e['ncc'])**4) if e['method']!='stage' else .001)
    # Weak priors keep disconnected/blank components in position without rank collapse.
    for k in keys:
        row=np.zeros(n);row[index[k]]=1
        rows.append(row);rhs.append(prior[k]);weights.append(1. if k==keys[0] else .00001)
    A,B,W=np.array(rows),np.array(rhs),np.array(weights)
    robust=np.ones(len(W))
    for _ in range(5):
        sw=np.sqrt(W*robust)
        x=np.linalg.lstsq(A*sw[:,None],B*sw[:,None],rcond=None)[0]
        residual=np.linalg.norm(A@x-B,axis=1)
        robust[:len(edges)]=np.minimum(1,3/np.maximum(residual[:len(edges)],1e-6))
    for i,e in enumerate(edges): e['residual_px']=float(residual[i])
    return {k:tuple(x[index[k]]) for k in keys}


def register(tiles,gx,gy,axis_sign=(-1,-1),mode='hybrid',grid_coordinates=None):
    start=time.monotonic()
    keys=sorted(tiles,key=lambda k:(k[1],k[0]))
    supplied={k:tuple(float(v) for v in grid_coordinates[k])
              for k in (grid_coordinates or {}) if k in tiles}
    coords={k:supplied.get(k,(float(k[0]),float(k[1]))) for k in keys}
    regular=[k for k in keys if k not in supplied]
    # Keep the stage/grid origin stable even when an extra asset has a
    # negative reserved index or a fractional centre coordinate.
    base_keys=regular or keys
    origin_coord=(min(coords[k][0] for k in base_keys),
                  min(coords[k][1] for k in base_keys))
    prior={k:((coords[k][0]-origin_coord[0])*gx*axis_sign[0],
              (coords[k][1]-origin_coord[1])*gy*axis_sign[1]) for k in keys}
    images={k:gray(tiles[k]) for k in keys}
    feats={}
    edges=[]
    # Regular lattice edges: four directional neighbours give redundant
    # evidence without turning the Pi preview into a registration worker.
    for a in regular:
        for ox,oy in ((1,0),(0,1),(1,1),(-1,1)):
            b=(a[0]+ox,a[1]+oy)
            if b not in images or b in supplied:continue
            expected=np.subtract(prior[b],prior[a])
            if abs(expected[0])>=images[a].shape[1]-24 or abs(expected[1])>=images[a].shape[0]-24:continue
            e=pair(images[a],images[b],expected,mode,feats.get(a),feats.get(b))
            e.update(a=a,b=b);edges.append(e)
    # Extra stage-positioned assets (the centre hub) connect to all nearby
    # regular cells.  Their fractional grid coordinates are used for the
    # expected overlap, so they strengthen the graph without becoming a fake
    # integer cell in Continue/fill-gaps.
    for a in supplied:
        for b in regular:
            delta_coord=np.subtract(coords[a],coords[b])
            if max(abs(float(delta_coord[0])),abs(float(delta_coord[1])))>1.01:
                continue
            expected=np.subtract(prior[a],prior[b])
            if abs(expected[0])>=images[b].shape[1]-24 or abs(expected[1])>=images[b].shape[0]-24:continue
            e=pair(images[b],images[a],expected,mode,feats.get(b),feats.get(a))
            e.update(a=b,b=a);edges.append(e)
    positions=solve(keys,edges,prior)

    # Enforce a coherent lattice when there is enough direct evidence.  The
    # unconstrained graph solve is useful for exploration, but can accumulate
    # a few-pixel differences between the two horizontal/vertical edges of a
    # 2x2+ scan.  Median direct steps are robust to one bad edge and keep the
    # Slide and tile layout in the same coordinate system.
    direct_x = [e['delta'][0] for e in edges
                if e['a'] not in supplied and e['b'] not in supplied
                and abs(coords[e['a']][1]-coords[e['b']][1]) < 1e-6
                and e['ncc'] >= .35]
    direct_y = [e['delta'][1] for e in edges
                if e['a'] not in supplied and e['b'] not in supplied
                and abs(coords[e['a']][0]-coords[e['b']][0]) < 1e-6
                and e['ncc'] >= .35]
    direct_x_cross = [e['delta'][1] for e in edges
                      if e['a'] not in supplied and e['b'] not in supplied
                      and abs(coords[e['a']][1]-coords[e['b']][1]) < 1e-6
                      and e['ncc'] >= .35]
    direct_y_cross = [e['delta'][0] for e in edges
                      if e['a'] not in supplied and e['b'] not in supplied
                      and abs(coords[e['a']][0]-coords[e['b']][0]) < 1e-6
                      and e['ncc'] >= .35]
    grid_step = [float(np.median(direct_x)) if direct_x else None,
                 float(np.median(direct_y)) if direct_y else None]

    def step_spread(values):
        if not values:
            return None
        values = np.asarray(values, dtype=float)
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median)))
        # MAD is stable for a small grid and does not let one bad overlap
        # veto the rest. Report a sigma-like spread for diagnostics.
        return 1.4826 * mad

    spread_x = step_spread(direct_x)
    spread_y = step_spread(direct_y)
    cross_x = max((abs(float(v)) for v in direct_x_cross), default=None)
    cross_y = max((abs(float(v)) for v in direct_y_cross), default=None)
    # A regular lattice is useful only when repeated edge measurements agree.
    # The previous implementation always locked to the median, which turned
    # row/column registration errors into a visible cross-shaped seam. Keep
    # the graph solution when the measured step spread is too large. The
    # cross-axis drift threshold is intentionally stricter than the step
    # spread: a few pixels of shear are enough to make the four-way overlap
    # disagree even when both horizontal steps have the same length.
    # `register()` commonly runs at half resolution, so this corresponds to
    # roughly 4-6 px at the rendered tile resolution.
    cross_limit_x = max(2.0, 0.010 * images[keys[0]].shape[0])
    cross_limit_y = max(2.0, 0.010 * images[keys[0]].shape[1])
    step_ok = bool(
        direct_x
        and direct_y
        and spread_x <= max(5.0, 0.025 * images[keys[0]].shape[1])
        and spread_y <= max(5.0, 0.025 * images[keys[0]].shape[0])
        # A stable step along one axis is not enough: repeated motion may
        # shear the row/column in the other axis. Locking that case to an
        # orthogonal lattice is what produced the previous cross artifacts.
        and cross_x <= cross_limit_x
        and cross_y <= cross_limit_y
    )
    if step_ok:
        anchor_coord = origin_coord
        positions = {
            k: ((coords[k][0] - anchor_coord[0]) * grid_step[0],
                (coords[k][1] - anchor_coord[1]) * grid_step[1])
            for k in keys
        }
    # Recompute residuals against the positions that will actually be
    # rendered. Previously warning_edges described the pre-lock graph, so a
    # bad forced lattice could report zero warnings.
    for edge in edges:
        delta = np.asarray(positions[edge['b']]) - np.asarray(positions[edge['a']])
        edge['residual_px'] = float(np.linalg.norm(delta - np.asarray(edge['delta'])))
    diagnostics={'engine':mode+'-global-v2','seconds':time.monotonic()-start,'edges':edges,
                 'warning_edges':sum(e['ncc']<.35 or e['residual_px']>4 for e in edges),
                 'policy':'always-slide; confidence is advisory; no automatic recapture',
                 'grid_locked': bool(step_ok),
                 'grid_step': grid_step,
                 'grid_step_spread': {'x': spread_x, 'y': spread_y},
                 'grid_cross_drift': {'x': cross_x, 'y': cross_y},
                 'grid_cross_limit': {'x': cross_limit_x, 'y': cross_limit_y},
                 'grid_support': {'x': len(direct_x), 'y': len(direct_y)}}
    return positions,diagnostics


def update_live_basis(paths, cells, stride, px_per_step, axis_sign, previous):
    """Fit a small preview transform; measure each cardinal pair only once."""
    width = 320
    old = previous or {}
    revisions = {(c["ix"], c["iy"]): c.get("capture_id") or c.get("sha256")
                 for c in cells}
    measurements = dict(old.get("pairs", {}))
    basis = np.diag(np.asarray(stride[:2]) * np.asarray(px_per_step[:2])
                    * np.asarray(axis_sign[:2]) / 640.0)
    images = {}
    def image(key):
        if key not in images:
            g = cv2.imread(paths[key], cv2.IMREAD_REDUCED_GRAYSCALE_2)
            if g is None:
                raise ValueError("No live tile image")
            g = cv2.resize(g, (width, round(g.shape[0]*width/g.shape[1])),
                           interpolation=cv2.INTER_AREA).astype(np.float32)
            images[key] = g - cv2.GaussianBlur(g, (0, 0), 2)
        return images[key]
    for a in sorted(paths):
        for axis, step in enumerate(((1, 0), (0, 1))):
            b = (a[0]+step[0], a[1]+step[1])
            if b not in paths:
                continue
            key = f"{a[0]},{a[1]}:{axis}"
            tag = [revisions.get(a), revisions.get(b)]
            if key in measurements and measurements[key]["tag"] == tag:
                continue
            g, h = image(a), image(b)
            if g.shape != h.shape:
                continue
            rows, cols = g.shape
            expected = basis[axis] * width
            proposals = [expected]
            shift, response = cv2.phaseCorrelate(g, h)
            if np.isfinite(shift).all() and response > .04:
                proposals.extend(np.array([x, y])-shift
                                 for x in (-cols, 0, cols) for y in (-rows, 0, rows))
            score, best = 0., expected
            for offset in proposals:
                if np.linalg.norm(offset-expected) > .34*min(rows, cols):
                    continue
                p = patches(g, h, *offset)
                if p is None or min(v.std() for v in p) < .8:
                    continue
                value = ncc(*p)
                if value > score:
                    score, best = value, offset
            measurements[key] = {"axis": axis, "tag": tag, "score": score,
                                 "offset": (best/width).tolist()}
    counts = []
    for axis in range(2):
        offsets = np.array([m["offset"] for m in measurements.values()
                            if m["axis"] == axis and m["score"] >= .52])
        counts.append(len(offsets))
        if len(offsets):
            basis[axis] = np.median(offsets, axis=0)
    return {"basis": basis.tolist(), "counts": counts, "pairs": measurements}


def register_overlap_graph(tiles, gx, gy, axis_sign=(-1, -1), grid_coordinates=None, asset_keys=None):
    """Register overlapping tiles with bounded phase correlation and a robust pose graph."""
    started = time.monotonic()
    keys = sorted(tiles, key=lambda k: (k[1], k[0]))
    coords = {k: np.asarray((grid_coordinates or {}).get(k, k), dtype=float) for k in keys}
    h, w = next(iter(tiles.values())).shape[:2]
    nominal = np.diag(np.asarray([gx, gy]) * np.asarray(axis_sign))
    images = {}
    for key, tile in tiles.items():
        g = gray(tile)
        images[key] = g - cv2.GaussianBlur(g, (0, 0), 3)

    def correlation(a, b, delta):
        p = patches(a, b, *delta)
        if p is None or min(p[0].std(), p[1].std()) < .65:
            return 0.
        return ncc(*p)

    def estimate(a, b, expected, coarse=False):
        a, b = images[a], images[b]
        limit = max(36, .33 * min(w, h)) if coarse else max(16, .065 * min(w, h))
        proposals = [('stage', np.round(expected))]
        if coarse:
            (dx, dy), response = cv2.phaseCorrelate(a, b)
            if np.isfinite([dx, dy, response]).all():
                proposals.extend(('coarse-phase', np.array([x-dx, y-dy]))
                                 for x in (-w, 0, w) for y in (-h, 0, h))
        overlap = patches(a, b, *expected)
        if overlap is not None:
            for p, q in (overlap, (overlap[0][8:-8, 8:-8], overlap[1][8:-8, 8:-8])):
                if min(p.shape) < 24 or min(p.std(), q.std()) < .65:
                    continue
                window = cv2.createHanningWindow((p.shape[1], p.shape[0]), cv2.CV_32F)
                shift, response = cv2.phaseCorrelate(p.copy(), q.copy(), window)
                if response >= .08 and np.isfinite(shift).all():
                    proposals.append(('overlap-phase', np.round(expected) - shift))
        ranked = [(correlation(a, b, delta), method, delta)
                  for method, delta in proposals if np.linalg.norm(delta-expected) <= limit]
        score, method, delta = max(ranked, key=lambda v: v[0])
        if score >= .24:
            p, q = patches(a, b, *delta)
            try:
                _, warp = cv2.findTransformECC(
                    p, q, np.eye(2, 3, dtype=np.float32), cv2.MOTION_TRANSLATION,
                    (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 1e-4), None, 3,
                )
                refined = np.round(delta) - warp[:, 2]
                refined_score = correlation(a, b, refined)
                if np.linalg.norm(refined-delta) < 3 and refined_score >= score - .012:
                    delta, score, method = refined, refined_score, method + '+ecc'
            except cv2.error:
                pass
        return {'delta': delta.tolist(), 'ncc': float(score), 'method': method}

    # Fit each stage basis vector independently from cardinal neighbours.
    # This corrects an outdated scale and allows camera/stage skew.
    assets = set(grid_coordinates or {}) if asset_keys is None else set(asset_keys)
    base = nominal.copy()
    cardinal = []
    for i, step in enumerate(((1, 0), (0, 1))):
        edges = []
        for a in keys:
            if a in assets:
                continue
            b = a[0]+step[0], a[1]+step[1]
            if b not in images or b in assets:
                continue
            step_delta = coords[b] - coords[a]
            edge = estimate(a, b, step_delta @ nominal, coarse=True)
            edge.update(a=a, b=b)
            edges.append(edge)
        cardinal.extend(edges)
        strong = [np.asarray(e["delta"]) / (coords[e["b"]]-coords[e["a"]])[i]
                  for e in edges if e["ncc"] >= .32]
        if strong:
            base[i] = np.median(strong, axis=0)

    prior = {k: tuple((coords[k]-coords[keys[0]]) @ base) for k in keys}
    edges = []
    coarse_edges = {(e['a'], e['b']): e for e in cardinal}
    # Spatial buckets keep neighbourhood discovery bounded for sparse scans.
    buckets = {}
    for k, point in coords.items():
        buckets.setdefault(tuple(np.floor(point).astype(int)), []).append(k)
    for a in keys:
        ix, iy = np.floor(coords[a]).astype(int)
        for ox in (-2, -1, 0, 1, 2):
            for oy in (-2, -1, 0, 1, 2):
                for b in buckets.get((ix+ox, iy+oy), ()):
                    if b <= a:
                        continue
                    delta = (coords[b]-coords[a] if a in assets or b in assets
                             else np.subtract(b, a))
                    if np.max(np.abs(delta)) > 1.01:
                        continue
                    expected = np.subtract(prior[b], prior[a])
                    if patches(images[a], images[b], *expected) is None:
                        continue
                    edge = estimate(a, b, expected)
                    earlier = coarse_edges.get((a, b))
                    if (earlier and earlier['ncc'] > edge['ncc']
                            and np.linalg.norm(np.subtract(earlier['delta'], expected)) < .08 * min(w, h)):
                        edge = dict(earlier)
                    edge.update(a=a, b=b)
                    edges.append(edge)

    accepted = [e for e in edges if e['ncc'] >= .28]
    poses = solve(keys, accepted, prior)
    # A strong image match can still be wrong. Test inconsistent constraints
    # after robust fitting, then re-fit without them. Blank components stay
    # tied to the calibrated stage through the soft priors.
    if accepted:
        cutoff = max(4.0, .009 * min(w, h))
        accepted = [e for e in accepted if e['residual_px'] <= cutoff]
        poses = solve(keys, accepted, prior)
    used = {(e['a'], e['b']) for e in accepted}
    for edge in edges:
        edge['accepted'] = (edge['a'], edge['b']) in used
        edge['residual_px'] = float(np.linalg.norm(
            np.subtract(poses[edge['b']], poses[edge['a']]) - edge['delta']))
    return poses, {
        'engine': 'overlap-phase-pose-graph-v1', 'seconds': time.monotonic()-started,
        'edges': edges, 'warning_edges': len(edges)-len(accepted),
        'grid_locked': False, 'stage_basis': base.tolist(), 'accepted_edges': len(accepted),
    }


def _photometric_normalize(tiles):
    """Gently align exposure and colour without changing source tiles."""
    stats = {}
    for key, tile in tiles.items():
        arr = np.asarray(tile, dtype=np.float32)
        flat = arr.reshape(-1, 3)
        g = gray(arr)
        threshold = max(180.0, float(np.percentile(g, 70)))
        mask = g >= threshold
        sample = arr[mask] if int(mask.sum()) >= 256 else flat
        stats[key] = {
            'background': np.median(sample, axis=0),
            'low': np.percentile(flat, 5, axis=0),
            'high': np.percentile(flat, 95, axis=0),
        }
    if not stats:
        return dict(tiles), {'method': 'none', 'tiles': {}}

    target_bg = np.median(np.stack([s['background'] for s in stats.values()]), axis=0)
    target_low = np.median(np.stack([s['low'] for s in stats.values()]), axis=0)
    target_high = np.median(np.stack([s['high'] for s in stats.values()]), axis=0)
    target_span = np.maximum(target_high - target_low, 8.0)
    normalized = {}
    diagnostics = {'method': 'robust-background-affine-v1', 'tiles': {}}
    for key, tile in tiles.items():
        s = stats[key]
        source_span = np.maximum(s['high'] - s['low'], 8.0)
        # Restrained correction: suppress obvious camera drift, but do not
        # flatten real stain variation or invent biological contrast.
        gain = np.clip(1.0 + 0.35 * (target_span / source_span - 1.0), 0.82, 1.18)
        offset = np.clip(0.65 * (target_bg - gain * s['background']), -24.0, 24.0)
        normalized[key] = np.clip(np.asarray(tile, dtype=np.float32) * gain + offset, 0.0, 255.0)
        diagnostics['tiles']['{},{}'.format(*key)] = {
            'gain': [float(v) for v in gain],
            'offset': [float(v) for v in offset],
            'background_before': [float(v) for v in s['background']],
            'background_target': [float(v) for v in target_bg],
        }
    return normalized, diagnostics


def _focus_weights(tiles):
    """Return local focus weights plus a robust tile-quality diagnostic."""
    local_maps = {}
    qualities = {}
    for key, tile in tiles.items():
        g = gray(tile)
        lap = np.abs(cv2.Laplacian(g, cv2.CV_32F, ksize=3))
        local = cv2.GaussianBlur(lap, (0, 0), 2.0)
        local_maps[key] = local
        qualities[key] = max(1e-3, float(np.percentile(local, 75)))
    reference = max(1e-3, float(np.median(list(qualities.values()))))
    weights = {}
    diagnostics = {
        'method': 'local-laplacian-quality-v1',
        'reference_quality': reference,
        'tiles': {},
    }
    for key, local in local_maps.items():
        p95 = max(1e-3, float(np.percentile(local, 95)))
        local_norm = np.clip(local / p95, 0.0, 1.0)
        ratio = float(np.clip(qualities[key] / reference, 0.15, 2.0))
        factor = ratio ** 1.7
        # Keep a small floor so a soft tile can still fill a non-overlap area;
        # the robust tile factor strongly suppresses it when a sharp neighbour
        # covers the same pixels.
        weights[key] = np.clip((0.12 + 0.88 * local_norm) * factor, 0.04, 3.0)
        diagnostics['tiles']['{},{}'.format(*key)] = {
            'quality': float(qualities[key]),
            'quality_ratio': ratio,
            'quality_factor': float(factor),
        }
    return weights, diagnostics


def stitch_cells_hq(cells,nx,ny,tile_w=640,stride_size=None,px_per_step=None,
                    axis_sign=None,cache_out=None,progress_cb=None,
                    grid_coordinates=None,registration_max_width=None,**kwargs):
    with Image.open(next(iter(cells.values()))) as first:
        width = min(int(tile_w), first.width)
        aspect = first.height / first.width

    def decode(size):
        tiles = {}
        for key, path in cells.items():
            with Image.open(path) as image:
                image.draft("RGB", size)
                tiles[key] = np.asarray(image.convert("RGB").resize(size, getattr(Image, 'Resampling', Image).LANCZOS))
        return tiles

    reg_w = min(width, int(registration_max_width or HQ_REGISTRATION_MAX_WIDTH))
    reg_h = max(1, round(reg_w * aspect))
    scale = reg_w / 640.0
    pps = px_per_step or [.1437, .1344]
    gx, gy = (
        [abs(stride_size[i]) * pps[i] * scale for i in (0, 1)]
        if stride_size is not None else [reg_w * .7, reg_h * .7]
    )
    registration_tiles = decode((reg_w, reg_h))
    positions, diagnostics = register_overlap_graph(
        registration_tiles, gx, gy, axis_sign or [-1, -1],
        grid_coordinates={k: tuple(v) for k, v in (grid_coordinates or {}).items()
                          if k in registration_tiles},
        asset_keys=kwargs.get("asset_keys"),
    )
    if progress_cb:
        progress_cb(75)
    minx, miny = np.min(list(positions.values()), axis=0)
    maxx, maxy = np.max(list(positions.values()), axis=0)
    # Plan the memory budget before decoding full-size tiles. Very large jobs
    # retain their native JPEGs even when the fused overview has a lower ceiling.
    edge = max(maxx - minx + reg_w, maxy - miny + reg_h)
    ratio = min(width / reg_w, 4800 / edge,
                np.sqrt(40_000_000 / (len(cells) * reg_w * reg_h)))
    w = max(1, int(np.floor(reg_w * ratio + 1e-8)))
    h = max(1, round(w * aspect))
    ratio = w / reg_w
    positions = {key: ((x - minx) * ratio, (y - miny) * ratio)
                 for key, (x, y) in positions.items()}
    diagnostics["registration_scale"] = reg_w / w
    cw = int(np.ceil(max(x for x, _ in positions.values()) + w))
    ch = int(np.ceil(max(y for _, y in positions.values()) + h))
    if (w, h) == (reg_w, reg_h):
        tiles = registration_tiles
    else:
        del registration_tiles
        tiles = decode((w, h))
    # Photometric correction and focus-aware weights happen only at render
    # time.  Acquisition bytes remain immutable and can still be inspected in
    # Tiles view or used for a later re-render with different policy.
    tiles, photometric = _photometric_normalize(tiles)
    focus_weights, focus = _focus_weights(tiles)
    fusion = {
        'engine': 'seam-aware-focus-photometric-v2',
        'photometric': photometric,
        'focus': focus,
    }
    acc=np.zeros((ch,cw,3),np.float32);total=np.zeros((ch,cw),np.float32)
    # Keep the two strongest sources per output pixel. When their content is
    # consistent, the normal weighted blend is smooth; when they disagree
    # (usually a residual registration error or a soft tile), select the
    # sharper source instead of averaging two offset nuclei into a ghost.
    best_score=np.zeros((ch,cw),np.float32)
    second_score=np.zeros((ch,cw),np.float32)
    best_img=np.zeros((ch,cw,3),np.float32)
    second_img=np.zeros((ch,cw,3),np.float32)
    yy,xx=np.mgrid[:h,:w]
    weight=np.maximum(1,np.minimum.reduce([xx,w-1-xx,yy,h-1-yy])).astype(np.float32)
    for k,tile in tiles.items():
        x,y=map(lambda v:int(round(v)),positions[k])
        source=tile.astype(np.float32)
        ww = weight * focus_weights[k]
        acc[y:y+h,x:x+w]+=source*ww[:,:,None];total[y:y+h,x:x+w]+=ww

        bs=best_score[y:y+h,x:x+w]
        ss=second_score[y:y+h,x:x+w]
        bi=best_img[y:y+h,x:x+w]
        si=second_img[y:y+h,x:x+w]
        promote=ww>bs
        runner=(~promote)&(ww>ss)
        if np.any(promote):
            si[promote]=bi[promote]
            ss[promote]=bs[promote]
            bi[promote]=source[promote]
            bs[promote]=ww[promote]
        if np.any(runner):
            si[runner]=source[runner]
            ss[runner]=ww[runner]

    out=acc/np.maximum(total,1e-6)[:,:,None]
    out[total==0]=245
    overlap=second_score>0
    disagreement=np.mean(np.abs(best_img-second_img),axis=2)
    hard=np.logical_and(
        overlap,
        np.logical_and(disagreement>22.0, best_score>=0.80*second_score),
    )
    # Do not expose the winner map as a one-pixel hard cut. A short blurred
    # selector keeps the seam visually continuous while still preventing a
    # broad overlap from averaging two displaced structures. If a large
    # fraction of the overlap disagrees, however, a winner mask is itself a
    # likely source of a synthetic row/column seam (especially at a 4-way
    # intersection). In that case prefer a much softer transition; the
    # registration diagnostics remain visible and the raw tiles remain
    # available for a later, targeted retake.
    if np.any(hard):
        hard_fraction=float(hard.sum()/max(1,int(overlap.sum())))
        selector_sigma = 14.0 if hard_fraction > 0.30 else 3.0
        selector_strength = 0.35 if hard_fraction > 0.30 else 1.0
        selector=cv2.GaussianBlur(hard.astype(np.float32),(0,0),selector_sigma)
        selector=np.clip(selector*selector_strength,0.0,1.0)
        selector=np.clip(selector,0.0,1.0)[:,:,None]
        out=out*(1.0-selector)+best_img*selector
    fusion['hard_select_pixels']=int(hard.sum())
    fusion['overlap_pixels']=int(overlap.sum())
    fusion['conflict_fraction']=float(hard.sum()/max(1,int(overlap.sum())))
    fusion['hard_select_policy'] = (
        'soft-wide-conflict' if fusion['conflict_fraction'] > 0.30
        else 'blurred-best-focus'
    )
    if cache_out is not None:
        cache_out.update(positions=positions,tile_w=w,tile_h=h,canvas=(cw,ch),engine=diagnostics['engine'],registration=diagnostics,fusion=fusion)
    if progress_cb:progress_cb(100)
    return Image.fromarray(np.clip(out,0,255).astype(np.uint8))
