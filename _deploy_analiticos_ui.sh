#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend) | /comercial/analiticos -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000/comercial/analiticos)"
echo ">> menu tem Analiticos por Camera -> /comercial/analiticos?"
curl -s -m6 http://localhost:8000/comercial/analiticos | grep -oE 'href="/comercial/analiticos">[^<]*' | head -1 | sed 's/^/   /'
echo ">> TESTE endpoints (lista cameras, salva config numa, confere, limpa)"
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
cams=requests.get(B+"/api/comercial/analiticos/cameras",headers=H,timeout=30).json()
print("   cameras:", len(cams), "| ja configuradas:", sum(1 for c in cams if c.get("config")))
cam=next((c for c in cams if not c.get("config")), cams[0])
cid=cam["id"]; print("   teste na camera:", cam["nome"], cid)
requests.post(B+"/api/comercial/analiticos/salvar",headers=H,json={"camera_id":cid,"camera_nome":cam["nome"],"ativo":True,"horarios":[],"analiticos_padrao":["pessoa","epi"]},timeout=10)
back=[c for c in requests.get(B+"/api/comercial/analiticos/cameras",headers=H,timeout=30).json() if c["id"]==cid][0]
print("   apos salvar -> config:", back.get("config",{}).get("analiticos_padrao") if back.get("config") else None)
requests.post(B+"/api/comercial/analiticos/limpar",headers=H,json={"camera_id":cid},timeout=10)
back2=[c for c in requests.get(B+"/api/comercial/analiticos/cameras",headers=H,timeout=30).json() if c["id"]==cid][0]
print("   apos limpar -> config:", back2.get("config"), "(esp None)")
PY
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "comercial: tela Analiticos por Camera (config por camera+horario do vocab completo: fogo/arma/placa/pessoa/veiculo/animal/epi) escrevendo ConfigAnalitico; ponte do SPA aponta pra ela"; fi
git log --oneline -1
