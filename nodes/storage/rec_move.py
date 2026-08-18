#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Organiza um segmento de gravacao do MediaMTX em /gravacoes/<Cliente>/<semana>/<dia>/<Camera>_<HH-MM-SS>.mp4
# Chamado pelo hook rec_organize.sh (runOnRecordSegmentComplete). argv: <arquivo_segmento> <mtx_path (cam/<key>)>
import sys, os, json, shutil, datetime, subprocess

BASE = "/gravacoes"
MAP = "/opt/corexia/cam_map.json"


def safe(s):
    s = "".join(c for c in str(s) if c.isalnum() or c in " -_.()").strip()
    return s or "SEM_NOME"


def main():
    if len(sys.argv) < 3:
        return
    seg, mpath = sys.argv[1], sys.argv[2]
    if not seg or not os.path.isfile(seg):
        return
    key = mpath.split("cam/", 1)[1] if "cam/" in mpath else mpath.strip("/")
    m = {}
    try:
        m = json.load(open(MAP))
    except Exception:
        pass
    info = m.get(key) or {}
    if info.get("grava") is False:
        # camera sem gravacao contratada -> descarta o segmento (nao armazena no 54TB)
        try:
            os.remove(seg)
        except Exception:
            pass
        print(datetime.datetime.now().isoformat(timespec="seconds"), key, "-> DESCARTADO (sem gravacao)", flush=True)
        return
    cliente = safe(info.get("cliente") or "SEM_CLIENTE")
    camera = safe(info.get("camera") or key)
    fname = os.path.basename(seg)
    try:
        dt = datetime.datetime.strptime(fname[:19], "%Y-%m-%d_%H-%M-%S")
    except Exception:
        dt = datetime.datetime.fromtimestamp(os.path.getmtime(seg))
    monday = dt - datetime.timedelta(days=dt.weekday())
    sunday = monday + datetime.timedelta(days=6)
    week = "%s_a_%s" % (monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"))
    day = dt.strftime("%Y-%m-%d")
    dest_dir = os.path.join(BASE, cliente, week, day)
    os.makedirs(dest_dir, exist_ok=True)
    ext = os.path.splitext(seg)[1] or ".mp4"
    dest = os.path.join(dest_dir, "%s_%s%s" % (camera, dt.strftime("%H-%M-%S"), ext))
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, "%s_%s_%d%s" % (camera, dt.strftime("%H-%M-%S"), n, ext)); n += 1
    # remuxa fmp4 (do MediaMTX) -> mp4 padrao com faststart, pra tocar no navegador. fallback: move puro.
    ok = False
    try:
        r = subprocess.run(["/usr/bin/ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                            "-i", seg, "-c", "copy", "-movflags", "+faststart", dest],
                           stdin=subprocess.DEVNULL, timeout=180)
        ok = (r.returncode == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 0)
    except Exception as e:
        print("rec_move remux erro:", e, flush=True)
    if ok:
        try:
            os.remove(seg)
        except Exception:
            pass
    else:
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except Exception:
            pass
        shutil.move(seg, dest)
    print(datetime.datetime.now().isoformat(timespec="seconds"), key, "->", dest, "(remux)" if ok else "(move)", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("rec_move erro:", e, flush=True)
