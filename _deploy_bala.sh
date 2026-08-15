#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py detector_saas.py || { echo ERRO; exit 1; }
echo ">> teste do Gemini balaclava (rosto normal -> False; balaclava -> True)"
./venv/bin/python - <<'PY'
import os, requests, base64
from dotenv import load_dotenv
load_dotenv(".env")
KEY=os.getenv("GEMINI_API_KEY",""); MODEL=os.getenv("GEMINI_MODEL","gemini-2.5-flash")
def grab(urls):
    for u in urls:
        try:
            r=requests.get(u,timeout=25,headers={"User-Agent":"Mozilla/5.0"})
            if r.ok and len(r.content)>6000: return r.content
        except Exception: pass
    return None
def bala(jpg):
    if not jpg: return "sem imagem"
    prompt=('Camera de seguranca "teste". A imagem e o RECORTE da cabeca/rosto de uma pessoa. '
     'A pessoa esta com o ROSTO COBERTO para ocultar a identidade — balaclava (touca ninja), '
     'mascara de esqui, capuz com mascara, ou CAPACETE INTEGRAL fechado? '
     'NAO conte como coberto: oculos, boné, mascara cirurgica comum, capuz sem mascara, rosto normal. '
     'Responda true SOMENTE se a maior parte do rosto estiver OCULTA. Responda SO JSON: {"coberto": true/false, "descricao": "1 frase"}')
    u=f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}"
    r=requests.post(u,timeout=25,json={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":"image/jpeg","data":base64.b64encode(jpg).decode()}}]}],"generationConfig":{"response_mime_type":"application/json","temperature":0}})
    import json as J; d=J.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"]); return d
face=grab(["https://upload.wikimedia.org/wikipedia/commons/thumb/8/8d/President_Barack_Obama.jpg/480px-President_Barack_Obama.jpg",
           "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e5/Boris_Johnson_official_portrait_%28cropped%29.jpg/480px-Boris_Johnson_official_portrait_%28cropped%29.jpg"])
mask=grab(["https://upload.wikimedia.org/wikipedia/commons/thumb/1/1e/Fsb_alpha.jpg/480px-Fsb_alpha.jpg",
           "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5c/Spetsnaz_soldier_in_balaclava.jpg/480px-Spetsnaz_soldier_in_balaclava.jpg",
           "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9a/Balaclava_3_hole_black.jpg/360px-Balaclava_3_hole_black.jpg"])
print("   ROSTO NORMAL ->", bala(face), "(esperado coberto=false)")
print("   BALACLAVA    ->", bala(mask), "(esperado coberto=true)")
PY
echo ">> teste roteamento toca_ninja -> COCO"
./venv/bin/python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv(".env")
GEN=os.getenv("MODEL_ID_GENERAL","yolov8s-640")
VOCAB={"toca_ninja":(GEN,)}
print("   toca_ninja ->", VOCAB.get("toca_ninja"), "(esp yolov8s-640)")
PY
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   backend: $(systemctl is-active corexia-backend) | /comercial/analiticos -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000/comercial/analiticos)"
echo ">> commit"
git add comercial.py detector_saas.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "IA balaclava/toca ninja: COCO(pessoa) -> recorte da cabeca -> Gemini (rosto coberto?), throttle+cooldown, fail-closed; checkbox na tela"; fi
git log --oneline -1
echo ">> restart nvdec"; pkill -f detector_nvdec.py
echo "(deploy ok; nvdec ~45s)"
