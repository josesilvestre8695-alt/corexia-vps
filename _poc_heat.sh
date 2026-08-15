#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> aguardando nvdec..."; sleep 50
echo "   nvdec@0=$(systemctl is-active vigia_nvdec@0) nvdec@1=$(systemctl is-active vigia_nvdec@1)"
CID=$(./venv/bin/python - <<'PY'
import os, requests, sqlite3, json, time, secrets
from dotenv import load_dotenv
load_dotenv(".env")
sec=os.getenv("WEBHOOK_SECRET","")
r=requests.post("http://localhost:8000/listarCamerasIA", json={"secret":sec,"validar":True,"decode_engine":"nvdec"}, timeout=120)
cams=[c for c in r.json().get("cameras",[]) if c.get("stream_valido")]
cid=cams[0]["id"]; nome=cams[0]["nome"]
d={"camera_id":cid,"camera_nome":nome,"ativo":True,"horarios":[],"analiticos_padrao":["heatmap"],"zonas_intrusao":[],"_poc":True}
c=sqlite3.connect("corexia.db"); now=time.strftime("%Y-%m-%dT%H:%M:%S")
row=c.execute("SELECT id FROM entities WHERE entity='ConfigAnalitico' AND json_extract(data,'$.camera_id')=?", (cid,)).fetchone()
if row: c.execute("UPDATE entities SET data=?,updated_date=? WHERE id=?", (json.dumps(d),now,row[0]))
else: c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)", ("ConfigAnalitico",secrets.token_hex(12),json.dumps(d),now,now))
c.commit(); c.close()
import sys; sys.stderr.write("   heatmap ON em %s (%s)\n"%(nome,cid))
print(cid)
PY
)
echo ">> aguardando sync + acumulo + flush (~140s)"; sleep 140
echo ">> logs do detector (heat/on-demand/erros):"
echo 'tvlantvlan' | sudo -S journalctl -u vigia_nvdec@0 -u vigia_nvdec@1 --since "4 min ago" --no-pager 2>/dev/null | grep -iE "on-demand|\[heat\]|heat\] erro" | tail -12
echo ">> query do heatmap desse periodo (hoje):"
./venv/bin/python - <<PY
import os, requests, time
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
d=time.strftime("%Y%m%d")
q=requests.get(B+"/api/comercial/heatmap/grid?camera_id=$CID&de="+d+"00&ate="+d+"23",headers=H,timeout=10).json()
print("   total passagens:", q.get("total"), "| pico:", q.get("max"), "| horas c/ dado:", q.get("buckets"))
print("   -> pipeline detector->server:", "OK (chegou dado)" if q.get("buckets") else "sem dado ainda (ninguem passou na cena OU aguardando flush)")
PY
echo ">> limpa PoC"
./venv/bin/python - <<'PY'
import sqlite3, json
c=sqlite3.connect("corexia.db")
for rid,data in c.execute("SELECT id,data FROM entities WHERE entity='ConfigAnalitico'").fetchall():
    if json.loads(data).get("_poc"): c.execute("DELETE FROM entities WHERE id=?", (rid,))
c.commit(); c.close(); print("   PoC config removida (dados de heatmap ficam)")
PY
echo "(fim)"
