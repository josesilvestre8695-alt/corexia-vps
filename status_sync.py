#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Sincroniza o campo status das cameras (online/offline) a partir do mediamtx_ready.json (quem publica).
import sqlite3, json, os, time
HERE = os.path.dirname(os.path.abspath(__file__))
try:
    d = json.load(open(os.path.join(HERE, "mediamtx_ready.json")))
except Exception:
    print(time.strftime("%FT%T"), "sem mediamtx_ready - nao mexo"); raise SystemExit(0)
if time.time() - float(d.get("ts", 0)) > 360:
    print(time.strftime("%FT%T"), "ready velho - nao mexo (evita falso offline)"); raise SystemExit(0)
ready = set(str(x) for x in d.get("ready", []))
c = sqlite3.connect(os.path.join(HERE, "corexia.db"), timeout=15)
n = 0
for (i, data) in c.execute("SELECT id,data FROM entities WHERE entity='Camera'").fetchall():
    try:
        o = json.loads(data)
    except Exception:
        continue
    sk = o.get("stream_key")
    new = "online" if (sk and sk in ready) else "offline"
    if o.get("status") != new:
        o["status"] = new
        c.execute("UPDATE entities SET data=? WHERE entity='Camera' AND id=?", (json.dumps(o, ensure_ascii=False), i))
        n += 1
c.commit(); c.close()
print(time.strftime("%FT%T"), "status sync:", n, "atualizadas | online agora:", len(ready))
