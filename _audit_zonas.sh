#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo "== chaves da entidade Camera + procura geometria em qualquer entidade =="
./venv/bin/python - <<'PY'
import sqlite3, json
c=sqlite3.connect("corexia.db")
ks=set()
for (d,) in c.execute("SELECT data FROM entities WHERE entity='Camera' LIMIT 80").fetchall():
    ks|=set(json.loads(d).keys())
print("Camera keys:", sorted(ks))
print("---- entidades com possivel geometria (poligono/linha/zona/points/roi/intrus) ----")
for (ent,) in c.execute("SELECT DISTINCT entity FROM entities").fetchall():
    hit=None
    for (d,) in c.execute("SELECT data FROM entities WHERE entity=? LIMIT 60",(ent,)).fetchall():
        s=d.lower()
        if any(k in s for k in ['poligon','linha','zona','virtual','"roi"','points','pontos','"area"','intrus','polygon']):
            hit=[k for k in json.loads(d).keys()]; break
    if hit: print("  ", ent, "->", hit)
c.close()
PY
echo "== server.py: rotas/menções de zona/linha/thumb =="
grep -niE 'zona|poligon|linha|polygon|virtual_line|/roi|pontos|intrus|camthumb|def _gen_thumb' server.py | head -25
echo "== /camthumb funciona? (pega uma camera online) =="
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
sec=os.getenv("WEBHOOK_SECRET","")
r=requests.post("http://localhost:8000/listarCamerasIA", json={"secret":sec,"validar":True,"decode_engine":"nvdec"}, timeout=120)
cams=[c for c in r.json().get("cameras",[]) if c.get("stream_valido")]
if cams:
    cid=cams[0]["id"]
    t=requests.get("http://localhost:8000/camthumb/"+cid, timeout=30)
    print("   camthumb %s -> HTTP %s | %s bytes | ct=%s" % (cams[0]["nome"], t.status_code, len(t.content), t.headers.get("content-type")))
else:
    print("   nenhuma online p/ testar thumb")
PY
