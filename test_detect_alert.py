"""Prova ponta-a-ponta: modelo REAL detecta arma numa imagem e dispara o alerta no Base44."""
import os, sys, requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from inference import get_model

API      = os.environ["ROBOFLOW_API_KEY"]
MODEL_ID = os.environ["MODEL_ID"]
WEBHOOK  = os.environ["WEBHOOK_URL"]
SECRET   = os.environ.get("WEBHOOK_SECRET", "corexia-webhook-2024")
CONF_MIN = float(os.environ.get("CONF_MIN", "0.40"))

CLASS_MAP = {"gun":"arma_fogo","pistol":"arma_fogo","rifle":"arma_fogo","weapon":"arma_fogo",
             "handgun":"arma_fogo","firearm":"arma_fogo","knife":"arma_branca","faca":"arma_branca"}

img = sys.argv[1]
res = get_model(model_id=MODEL_ID, api_key=API).infer(img)[0]

best = None
for p in res.predictions:
    tipo = CLASS_MAP.get(p.class_name.lower())
    if tipo and p.confidence >= CONF_MIN and (best is None or p.confidence > best[1]):
        best = (tipo, p.confidence, p.class_name)

if not best:
    print("Nenhuma arma acima do limiar."); sys.exit()

tipo, conf, cls = best
print(f"Detectado: {cls} {conf:.2f} -> tipo={tipo}")
r = requests.post(WEBHOOK, timeout=15, json={
    "secret": SECRET,
    "camera_nome": "TESTE MODELO REAL",
    "tipo": tipo,
    "descricao": f"[IA local] {cls} detectado pelo modelo Roboflow ({int(conf*100)}%)",
    "confianca": int(conf * 100),
})
print("Webhook:", r.status_code, r.text[:200])
