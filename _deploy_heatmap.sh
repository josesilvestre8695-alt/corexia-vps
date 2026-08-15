#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py detector_saas.py || { echo ERRO; exit 1; }
echo ">> CAMERAS_URL (base do HEATMAP_URL):"; grep '^CAMERAS_URL=' .env | sed 's/^/   /'
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend) | /comercial/heatmap -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000/comercial/heatmap)"
echo ">> menu tem Mapa de Calor?"; curl -s -m6 http://localhost:8000/comercial/heatmap | grep -oE 'href="/comercial/heatmap">[^<]*' | head -1 | sed 's/^/   /'
echo ">> TESTE pipeline: ingest 2x (soma) + query + cleanup"
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; sec=os.getenv("WEBHOOK_SECRET","")
email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
cam="HEATTEST"; bk="2026073115"; g=[1,2,3,4,5,6,7,8]
r1=requests.post(B+"/api/comercial/heatmap/ingest",json={"secret":sec,"camera_id":cam,"bucket":bk,"gw":4,"gh":2,"grid":g},timeout=10)
r2=requests.post(B+"/api/comercial/heatmap/ingest",json={"secret":sec,"camera_id":cam,"bucket":bk,"gw":4,"gh":2,"grid":g},timeout=10)
print("   ingest1:", r1.status_code, "| ingest2:", r2.status_code)
q=requests.get(B+"/api/comercial/heatmap/grid?camera_id="+cam+"&de=2026073100&ate=2026073123",headers=H,timeout=10).json()
print("   query -> gw/gh:", q.get("gw"), q.get("gh"), "| grid:", q.get("grid"), "| max:", q.get("max"), "| total:", q.get("total"), "| buckets:", q.get("buckets"))
print("   soma correta (esperado [2,4,6,8,10,12,14,16], total 72, max 16):", q.get("grid")==[2,4,6,8,10,12,14,16] and q.get("total")==72 and q.get("max")==16)
# auth negado sem secret?
rn=requests.post(B+"/api/comercial/heatmap/ingest",json={"camera_id":cam,"bucket":bk,"gw":4,"gh":2,"grid":g},timeout=10)
print("   ingest sem secret ->", rn.status_code, "(esp 401)")
import sqlite3
c=sqlite3.connect("corexia.db"); c.execute("DELETE FROM entities WHERE entity='Heatmap' AND json_extract(data,'$.camera_id')=?", (cam,)); c.commit(); c.close()
print("   cleanup ok")
PY
echo ">> commit"
git add comercial.py detector_saas.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "IA mapa de calor: detector acumula posicoes de pessoa por grade/hora e envia ao server; endpoints ingest+query; tela viewer (colormap sobre snapshot, filtros hora/dia) + area no editor de zonas + checkbox habilitar"; fi
git log --oneline -1
echo ">> restart nvdec"; pkill -f detector_nvdec.py
echo "(deploy ok; nvdec reiniciando ~45s)"
