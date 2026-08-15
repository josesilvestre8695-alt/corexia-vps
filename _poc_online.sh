#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> procurando camera ONLINE que o nvdec processa (engine=nvdec, stream_valido)"
./venv/bin/python - <<'PY'
import os, requests, json, sqlite3, time
from dotenv import load_dotenv
load_dotenv(".env")
sec=os.getenv("WEBHOOK_SECRET","")
r=requests.post("http://localhost:8000/listarCamerasIA", json={"secret":sec,"validar":True,"decode_engine":"nvdec"}, timeout=120)
cams=[c for c in r.json().get("cameras",[]) if c.get("stream_valido")]
print("   online (nvdec):", len(cams))
if not cams:
    print("   NENHUMA online agora — nao da p/ PoC live neste momento."); raise SystemExit
cam=cams[0]; cid=cam["id"]; nome=cam.get("nome","")
print("   escolhida: %s (id=%s)" % (nome, cid))
# upsert ConfigAnalitico com pessoa+epi 24/7 (padrao, sem horario)
c=sqlite3.connect("corexia.db")
row=c.execute("SELECT id FROM entities WHERE entity='ConfigAnalitico' AND json_extract(data,'$.camera_id')=?", (cid,)).fetchone()
d={"camera_id":cid,"camera_nome":nome,"ativo":True,"horarios":[],"analiticos_padrao":["pessoa","epi"],"zonas_intrusao":[],"_poc":True}
now=time.strftime("%Y-%m-%dT%H:%M:%S")
if row:
    c.execute("UPDATE entities SET data=?, updated_date=? WHERE id=?", (json.dumps(d), now, row[0]))
    print("   config ATUALIZADA")
else:
    import secrets
    c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
              ("ConfigAnalitico", secrets.token_hex(12), json.dumps(d), now, now))
    print("   config CRIADA")
c.commit(); c.close()
open("/tmp/poc_cam.txt","w").write(nome)
print("   -> pessoa+epi 24/7 nesta camera. Detector pega no proximo sync.")
PY
echo "(fim step online-setup)"
