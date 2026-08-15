#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> liga heatmap em 10 cameras online"
./venv/bin/python - <<'PY'
import os, requests, sqlite3, json, time, secrets
from dotenv import load_dotenv
load_dotenv(".env")
sec=os.getenv("WEBHOOK_SECRET","")
r=requests.post("http://localhost:8000/listarCamerasIA", json={"secret":sec,"validar":True,"decode_engine":"nvdec"}, timeout=120)
cams=[c for c in r.json().get("cameras",[]) if c.get("stream_valido")][:10]
c=sqlite3.connect("corexia.db"); now=time.strftime("%Y-%m-%dT%H:%M:%S")
for cam in cams:
    cid=cam["id"]
    d={"camera_id":cid,"camera_nome":cam["nome"],"ativo":True,"horarios":[],"analiticos_padrao":["heatmap"],"zonas_intrusao":[],"_poc":True}
    row=c.execute("SELECT id FROM entities WHERE entity='ConfigAnalitico' AND json_extract(data,'$.camera_id')=?", (cid,)).fetchone()
    if row: c.execute("UPDATE entities SET data=?,updated_date=? WHERE id=?", (json.dumps(d),now,row[0]))
    else: c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)", ("ConfigAnalitico",secrets.token_hex(12),json.dumps(d),now,now))
c.commit(); c.close()
print("   ligadas:", ", ".join(x["nome"] for x in cams))
PY
echo ">> aguardando acumulo + flush (~160s)"; sleep 160
echo ">> query por camera (hoje):"
./venv/bin/python - <<'PY'
import os, requests, time
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
import sqlite3, json
c=sqlite3.connect("corexia.db"); ids=[(json.loads(d).get("camera_id"),json.loads(d).get("camera_nome")) for _i,d in c.execute("SELECT id,data FROM entities WHERE entity='ConfigAnalitico'").fetchall() if json.loads(d).get("_poc")]; c.close()
day=time.strftime("%Y%m%d"); algum=False
for cid,nome in ids:
    q=requests.get(B+"/api/comercial/heatmap/grid?camera_id="+cid+"&de="+day+"00&ate="+day+"23",headers=H,timeout=10).json()
    if q.get("total"): algum=True; print("   %-22s total=%s pico=%s horas=%s" % (nome, q["total"], q["max"], q["buckets"]))
print("   >> PIPELINE COM DADO REAL:", "CONFIRMADO" if algum else "nenhuma captou pessoa na janela (cena parada)")
PY
echo ">> limpa PoC configs (dados de heatmap ficam)"
./venv/bin/python - <<'PY'
import sqlite3, json
c=sqlite3.connect("corexia.db")
n=0
for rid,data in c.execute("SELECT id,data FROM entities WHERE entity='ConfigAnalitico'").fetchall():
    if json.loads(data).get("_poc"): c.execute("DELETE FROM entities WHERE id=?", (rid,)); n+=1
c.commit(); c.close(); print("   removidas:", n)
PY
echo "(fim)"
