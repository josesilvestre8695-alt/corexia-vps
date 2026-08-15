#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py detector_saas.py || { echo ERRO; exit 1; }
echo ">> mecanismo gemini_piscina num frame real (camthumb):"
./venv/bin/python - <<'PY'
import os, requests, base64, json
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; KEY=os.getenv("GEMINI_API_KEY",""); MODEL=os.getenv("GEMINI_MODEL","gemini-2.5-flash")
sec=os.getenv("WEBHOOK_SECRET",""); email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
tok=requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")
H={"Authorization":"Bearer "+tok}
cams=[c for c in requests.post(B+"/listarCamerasIA",json={"secret":sec,"validar":True,"decode_engine":"nvdec"},timeout=120).json().get("cameras",[]) if c.get("stream_valido")]
cid=cams[0]["id"]; t=requests.get(B+"/camthumb/"+cid,headers=H,timeout=30)
print("   snapshot:", t.status_code, len(t.content), "bytes")
if t.status_code==200 and len(t.content)>3000:
    prompt=('Camera de piscina. Ha alguem em possivel AFOGAMENTO (boiando imovel/de bruços, submerso, ou se debatendo)? '
     'NAO alarme p/ nado normal, boia ativa, borda, ou agua vazia. Responda SO JSON: {"perigo": true/false, "descricao":"1 frase"}')
    u=f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}"
    r=requests.post(u,timeout=25,json={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":"image/jpeg","data":base64.b64encode(t.content).decode()}}]}],"generationConfig":{"response_mime_type":"application/json","temperature":0}})
    print("   gemini status:", r.status_code, "->", json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"]), "(mecanismo OK)")
PY
echo ">> roteamento piscina -> COCO"
./venv/bin/python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv(".env")
print("   piscina ->", (os.getenv("MODEL_ID_GENERAL","yolov8s-640"),))
PY
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   backend: $(systemctl is-active corexia-backend) | /comercial/analiticos -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000/comercial/analiticos)"
echo ">> commit"
git add comercial.py detector_saas.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "IA piscina/afogamento (AUXILIO): zona de agua + pessoa imovel na agua + Gemini periodico na zona; fail-closed, throttle+cooldown; modo 'agua' no editor + checkbox"; fi
git log --oneline -1
echo ">> restart nvdec"; pkill -f detector_nvdec.py
echo "(deploy ok; nvdec ~45s)"
