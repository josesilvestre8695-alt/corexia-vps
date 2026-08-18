# -*- coding: utf-8 -*-
"""Nucleo do indice CLIP (Fase 2 do 'Pergunte ao Corexia'). Roda na Xeon (GPU).
Store por camera/data: <STORE>/<camera_key>/<date>.npy (Nx512 float32 normalizado)
+ <date>.jsonl (metadados por vetor) + <date>.done (arquivos ja indexados)."""
import os, json, time, subprocess, glob, tempfile, shutil
import numpy as np

BASE = "/home/corexia/corexia-ia/busca"
STORE = os.path.join(BASE, "store")
os.makedirs(STORE, exist_ok=True)

try:
    for _ln in open('/home/corexia/corexia-ia/.env'):
        _ln = _ln.strip()
        if '=' in _ln and not _ln.startswith('#'):
            _k, _v = _ln.split('=', 1); os.environ.setdefault(_k, _v)
except Exception:
    pass

_MODEL = None
def model():
    global _MODEL
    if _MODEL is None:
        from inference.models import Clip
        _MODEL = Clip()
    return _MODEL

def embed_image(path):
    e = np.asarray(model().embed_image(path), dtype=np.float32).ravel()
    return e / (np.linalg.norm(e) + 1e-9)

def embed_text(text):
    e = np.asarray(model().embed_text(text), dtype=np.float32).ravel()
    return e / (np.linalg.norm(e) + 1e-9)

def _cam_dir(camera_key):
    d = os.path.join(STORE, str(camera_key))
    os.makedirs(d, exist_ok=True)
    return d

def already_indexed(camera_key, date, arquivo):
    done = os.path.join(_cam_dir(camera_key), date + ".done")
    if not os.path.exists(done):
        return False
    return arquivo in set(x for x in open(done).read().split("\n") if x)

def index_segment(camera_key, date, arquivo, inicio, signed_url, step=3.0, max_frames=0):
    """Extrai quadros (ffmpeg via URL assinada), embeda e adiciona ao store. Idempotente por arquivo."""
    if already_indexed(camera_key, date, arquivo):
        return 0
    tmp = tempfile.mkdtemp(prefix="idx_")
    try:
        pat = os.path.join(tmp, "f_%05d.jpg")
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", signed_url,
                        "-vf", "fps=1/%g,scale=224:-2" % step, "-q:v", "4", pat],
                       timeout=900, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        frames = sorted(glob.glob(os.path.join(tmp, "f_*.jpg")))
        if max_frames and len(frames) > max_frames:
            frames = frames[:max_frames]
        if not frames:
            _mark_done(camera_key, date, arquivo)
            return 0
        try:
            bh, bm, bs = [int(x) for x in inicio.split(":")]
            base = bh * 3600 + bm * 60 + bs
        except Exception:
            base = 0
        vecs = []; meta = []
        for i, f in enumerate(frames):
            try:
                e = embed_image(f)
            except Exception:
                continue
            off = i * step
            tot = base + int(off)
            ts = "%02d:%02d:%02d" % ((tot // 3600) % 24, (tot % 3600) // 60, tot % 60)
            vecs.append(e); meta.append({"arquivo": arquivo, "offset": off, "ts": ts})
        if not vecs:
            _mark_done(camera_key, date, arquivo)
            return 0
        _append(camera_key, date, np.vstack(vecs).astype(np.float32), meta)
        _mark_done(camera_key, date, arquivo)
        return len(vecs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _append(camera_key, date, vecs, meta):
    d = _cam_dir(camera_key)
    npy = os.path.join(d, date + ".npy")
    mj = os.path.join(d, date + ".jsonl")
    if os.path.exists(npy):
        allv = np.vstack([np.load(npy), vecs]).astype(np.float32)
    else:
        allv = vecs
    np.save(npy, allv)
    with open(mj, "a") as f:
        for m in meta:
            f.write(json.dumps(m) + "\n")

def _mark_done(camera_key, date, arquivo):
    with open(os.path.join(_cam_dir(camera_key), date + ".done"), "a") as f:
        f.write(arquivo + "\n")

def query(text, camera_key, date, t0=None, t1=None, topk=30, min_score=0.0):
    d = _cam_dir(camera_key)
    npy = os.path.join(d, date + ".npy"); mj = os.path.join(d, date + ".jsonl")
    if not (os.path.exists(npy) and os.path.exists(mj)):
        return {"indexed": False, "results": []}
    V = np.load(npy)
    meta = [json.loads(l) for l in open(mj) if l.strip()]
    n = min(len(V), len(meta)); V = V[:n]; meta = meta[:n]
    q = embed_text(text)
    sims = V.dot(q)
    out = []
    for idx in np.argsort(-sims):
        m = meta[idx]
        if t0 and m["ts"] < t0:
            continue
        if t1 and m["ts"] > t1:
            continue
        sc = float(sims[idx])
        if sc < min_score:
            break
        out.append({"arquivo": m["arquivo"], "offset": m["offset"], "ts": m["ts"], "score": round(sc, 4)})
        if len(out) >= topk:
            break
    return {"indexed": True, "count": n, "results": out}
