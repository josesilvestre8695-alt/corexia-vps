#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> aguardando nvdec voltar..."; sleep 55
echo "   nvdec@0=$(systemctl is-active vigia_nvdec@0) nvdec@1=$(systemctl is-active vigia_nvdec@1)"
echo ">> seta zona cobrindo o quadro inteiro + intruso numa camera online"
CID=$(./venv/bin/python - <<'PY'
import os, requests, sqlite3, json, time, secrets
from dotenv import load_dotenv
load_dotenv(".env")
sec=os.getenv("WEBHOOK_SECRET","")
r=requests.post("http://localhost:8000/listarCamerasIA", json={"secret":sec,"validar":True,"decode_engine":"nvdec"}, timeout=120)
cams=[c for c in r.json().get("cameras",[]) if c.get("stream_valido")]
cid=cams[0]["id"]; nome=cams[0]["nome"]
d={"camera_id":cid,"camera_nome":nome,"ativo":True,"horarios":[],"analiticos_padrao":["intruso"],
   "zonas_intrusao":[{"tipo":"zona","nome":"PoC","pontos":[[0.02,0.02],[0.98,0.02],[0.98,0.98],[0.02,0.98]]}],"_poc":True}
c=sqlite3.connect("corexia.db"); now=time.strftime("%Y-%m-%dT%H:%M:%S")
row=c.execute("SELECT id FROM entities WHERE entity='ConfigAnalitico' AND json_extract(data,'$.camera_id')=?", (cid,)).fetchone()
if row: c.execute("UPDATE entities SET data=?,updated_date=? WHERE id=?", (json.dumps(d),now,row[0]))
else: c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)", ("ConfigAnalitico",secrets.token_hex(12),json.dumps(d),now,now))
c.commit(); c.close()
import sys; sys.stderr.write("   camera: %s (%s)\n"%(nome,cid))
print(cid)
PY
)
echo "   (config setada em $CID)"
echo ">> aguardando sync + inferencia (~150s)"; sleep 150
echo ">> logs (carga COCO on-demand + alertas de zona + erros):"
echo 'tvlantvlan' | sudo -S journalctl -u vigia_nvdec@0 -u vigia_nvdec@1 --since "5 min ago" --no-pager 2>/dev/null | grep -iE "on-demand|\[zona\]|\[ALERTA\].*intruso|Traceback|zona\] erro" | tail -25
echo ">> limpa PoC"
./venv/bin/python - <<PY
import sqlite3, json
c=sqlite3.connect("corexia.db")
for rid,data in c.execute("SELECT id,data FROM entities WHERE entity='ConfigAnalitico'").fetchall():
    if json.loads(data).get("_poc"): c.execute("DELETE FROM entities WHERE id=?", (rid,))
c.commit(); c.close(); print("   PoC removido")
PY
echo "(fim)"
