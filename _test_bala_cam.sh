#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
./venv/bin/python - <<'PY'
import os, requests, base64, json
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; KEY=os.getenv("GEMINI_API_KEY",""); MODEL=os.getenv("GEMINI_MODEL","gemini-2.5-flash")
sec=os.getenv("WEBHOOK_SECRET",""); email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
tok=requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")
H={"Authorization":"Bearer "+tok}
cams=[c for c in requests.post(B+"/listarCamerasIA",json={"secret":sec,"validar":True,"decode_engine":"nvdec"},timeout=120).json().get("cameras",[]) if c.get("stream_valido")]
cid=cams[0]["id"]; nome=cams[0]["nome"]
t=requests.get(B+"/camthumb/"+cid,headers=H,timeout=30)
print("   snapshot de", nome, "->", t.status_code, len(t.content), "bytes")
if t.status_code==200 and len(t.content)>3000:
    prompt=('A imagem e de uma camera de seguranca. Ha alguma pessoa com o ROSTO COBERTO '
     '(balaclava/touca ninja/mascara de esqui/capacete integral)? Responda SO JSON: {"coberto": true/false, "descricao":"1 frase"}')
    u=f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}"
    r=requests.post(u,timeout=25,json={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":"image/jpeg","data":base64.b64encode(t.content).decode()}}]}],"generationConfig":{"response_mime_type":"application/json","temperature":0}})
    print("   gemini status:", r.status_code)
    d=json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    print("   gemini_balaclava(frame real) ->", d, "(mecanismo OK: chamada+parse retornaram bool)")
PY
echo "(fim)"
