#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> nvdec: $(systemctl is-active vigia_nvdec@0)/$(systemctl is-active vigia_nvdec@1)"
echo ">> mecanismo gemini_balaclava (chamada+parse) em imagens reais:"
./venv/bin/python - <<'PY'
import os, requests, base64, json
from dotenv import load_dotenv
load_dotenv(".env")
KEY=os.getenv("GEMINI_API_KEY",""); MODEL=os.getenv("GEMINI_MODEL","gemini-2.5-flash")
def grab(urls):
    for u in urls:
        try:
            r=requests.get(u,timeout=25,headers={"User-Agent":"Mozilla/5.0"})
            if r.ok and len(r.content)>6000: return r.content, u
        except Exception: pass
    return None, None
def bala(jpg):
    prompt=('A imagem e o recorte da cabeca/rosto de uma pessoa. A pessoa esta com o ROSTO COBERTO '
     '(balaclava/touca ninja/mascara de esqui/capacete integral fechado)? NAO conte oculos, boné, '
     'mascara cirurgica, capuz sem mascara ou rosto normal. Responda SO JSON: {"coberto": true/false, "descricao":"1 frase"}')
    u=f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}"
    r=requests.post(u,timeout=25,json={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":"image/jpeg","data":base64.b64encode(jpg).decode()}}]}],"generationConfig":{"response_mime_type":"application/json","temperature":0}})
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
# imagem que sabidamente baixa (fogo) -> confirma chamada+parse, coberto deve ser false
fire,uf=grab(["https://upload.wikimedia.org/wikipedia/commons/0/06/Fire.JPG"])
print("   fogo baixou:", bool(fire))
if fire: print("   gemini_balaclava(fogo) ->", bala(fire), "(esperado coberto=false; valida mecanismo)")
# tenta uma balaclava (varias fontes simples)
mask,um=grab(["https://upload.wikimedia.org/wikipedia/commons/6/6b/Balaclava.jpg",
              "https://upload.wikimedia.org/wikipedia/commons/2/2e/Balaclava_mask.jpg",
              "https://upload.wikimedia.org/wikipedia/commons/c/c8/Ski_mask.jpg"])
print("   balaclava baixou:", bool(mask), um or "")
if mask: print("   gemini_balaclava(balaclava) ->", bala(mask), "(esperado coberto=true)")
PY
echo "(fim)"
