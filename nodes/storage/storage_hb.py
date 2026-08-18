#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Heartbeat do storage (122) -> VPS /api/storage/heartbeat. Roda por cron a cada 2 min.
# O monitor_ops.py no VPS le _metrics.json e alerta o plantao se: sem heartbeat, disco>=90%, recorder/mediamtx inativo.
import os, json, subprocess, urllib.request
try:
    SEC = open("/opt/corexia/webhook_secret").read().strip()
except Exception:
    raise SystemExit("sem webhook_secret")

def active(svc):
    try:
        return subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"

st = os.statvfs("/gravacoes")
total = st.f_blocks * st.f_frsize
used = total - st.f_bfree * st.f_frsize
pct = round(100.0 * used / total, 1) if total else 0.0
mtx = active("mediamtx")
try:
    load = round(os.getloadavg()[0], 2)
except Exception:
    load = 0
body = {
    "secret": SEC, "agente": "storage-122",
    "load": load, "nucleos": os.cpu_count() or 0,
    "disco": {"total_tb": round(total / 1e12, 2), "usado_tb": round(used / 1e12, 2), "pct": pct},
    "mediamtx": mtx, "recorder": mtx,
}
req = urllib.request.Request("https://grupocorexia.com.br/api/storage/heartbeat",
                             data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req, timeout=15)
    print("hb ok", r.status, "disco", body["disco"]["pct"], "% mediamtx", mtx)
except Exception as e:
    print("hb falhou:", str(e)[:120])
