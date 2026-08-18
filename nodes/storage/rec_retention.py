#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Retencao: apaga .mp4 mais antigos que os dias de gravacao de cada camera (do cam_map.json).
# Seguro: se nao souber a camera, usa DEFAULT (mantem mais tempo, nunca apaga cedo demais).
import os, json, datetime
BASE = "/gravacoes"
MAP = "/opt/corexia/cam_map.json"
DEFAULT = int(os.getenv("REC_RETENCAO_DEFAULT", "30"))

def safe(s):
    s = "".join(c for c in str(s) if c.isalnum() or c in " -_.()").strip()
    return s or "SEM_NOME"

try:
    m = json.load(open(MAP))
except Exception:
    m = {}
by_cli = {}
for k, v in m.items():
    cli = safe(v.get("cliente") or "SEM_CLIENTE")
    cam = safe(v.get("camera") or k)
    dias = int(v.get("dias", 0) or 0)
    by_cli.setdefault(cli, []).append((cam, dias))

today = datetime.date.today()
deleted = 0; freed = 0
for cliente in list(os.listdir(BASE)):
    if cliente.startswith("_"):
        continue
    cpath = os.path.join(BASE, cliente)
    if not os.path.isdir(cpath):
        continue
    cams = by_cli.get(cliente, [])
    for week in list(os.listdir(cpath)):
        wpath = os.path.join(cpath, week)
        if not os.path.isdir(wpath):
            continue
        for day in list(os.listdir(wpath)):
            dpath = os.path.join(wpath, day)
            if not os.path.isdir(dpath):
                continue
            try:
                dday = datetime.datetime.strptime(day[:10], "%Y-%m-%d").date()
            except Exception:
                continue
            age = (today - dday).days
            for f in list(os.listdir(dpath)):
                if not f.endswith(".mp4"):
                    continue
                dias = None
                for (cam, dd) in cams:
                    if f.startswith(cam + "_"):
                        dias = dd; break
                if not dias or dias <= 0:
                    dias = DEFAULT
                if age > dias:
                    fp = os.path.join(dpath, f)
                    try:
                        sz = os.path.getsize(fp); os.remove(fp); deleted += 1; freed += sz
                    except Exception:
                        pass
            try:
                if not os.listdir(dpath):
                    os.rmdir(dpath)
            except Exception:
                pass
        try:
            if not os.listdir(wpath):
                os.rmdir(wpath)
        except Exception:
            pass
print(datetime.datetime.now().isoformat(timespec="seconds"), "retencao: apagados", deleted, "arquivos |", round(freed / 1e9, 2), "GB")
