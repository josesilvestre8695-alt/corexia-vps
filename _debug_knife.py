"""Replica o fluxo completo da verificacao de faca e SALVA as imagens pra inspecao:
frame -> knives-detection -> caixa + recorte -> Gemini (prompt novo) -> resposta."""
import os, sys, base64, json
os.environ["CUDA_VISIBLE_DEVICES"] = ""
import cv2, requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

API = os.environ["ROBOFLOW_API_KEY"]
KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

cap = cv2.VideoCapture("/home/tvlan/corexia-vision-ai/gravacoes_live/6313df722d27e27d7999fb78/index.m3u8")
ok, frame = cap.read(); cap.release()
if not ok:
    print("SEM FRAME"); sys.exit(1)

from inference import get_model
m = get_model(model_id="knives-detection/1", api_key=API)
res = m.infer(frame, confidence=0.25)[0]
if not res.predictions:
    print("SEM DETECCAO de faca neste frame (conf>=25%)")
    cv2.imwrite("/tmp/kn_frame.jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
    sys.exit(0)

p = max(res.predictions, key=lambda q: q.confidence)
print(f"deteccao: {p.class_name} {int(p.confidence*100)}% @ x={int(p.x)} y={int(p.y)} {int(p.width)}x{int(p.height)}")

anot = frame.copy()
x1, y1 = int(p.x - p.width/2), int(p.y - p.height/2)
cv2.rectangle(anot, (x1, y1), (x1+int(p.width), y1+int(p.height)), (0,0,255), 4)
cv2.putText(anot, f"{p.class_name} {int(p.confidence*100)}%", (x1, max(24, y1-8)),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,0,255), 3)
H, W = frame.shape[:2]
mg = max(p.width, p.height) * 1.3 + 80
cx1, cy1 = int(max(0, p.x-mg)), int(max(0, p.y-mg))
cx2, cy2 = int(min(W, p.x+mg)), int(min(H, p.y+mg))
crop = frame[cy1:cy2, cx1:cx2]
cv2.imwrite("/tmp/kn_anot.jpg", anot, [cv2.IMWRITE_JPEG_QUALITY, 78])
cv2.imwrite("/tmp/kn_crop.jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
print("salvos: /tmp/kn_anot.jpg /tmp/kn_crop.jpg")

# Gemini com o prompt NOVO
def b64(pth): return base64.b64encode(open(pth, "rb").read()).decode()
prompt = ('Camera de seguranca "Camera Escritorio". O detector de objetos marcou uma FACA ou outro '
          'objeto cortante (arma branca) na regiao da CAIXA VERMELHA da 1a imagem '
          '(a 2a imagem e o RECORTE AMPLIADO dessa regiao — examine-a com atencao). '
          'Julgue apenas a PRESENCA do objeto, NAO a intencao ou perigo: mesmo em cena calma ou '
          'aparentando teste, se o objeto estiver visivel, confirme. Responda SO JSON: '
          '{"confirmado": true/false, "descricao": "o que ve em 1 frase"}')
r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}",
    timeout=25, json={"contents":[{"parts":[
        {"text": prompt},
        {"inline_data": {"mime_type": "image/jpeg", "data": b64("/tmp/kn_anot.jpg")}},
        {"inline_data": {"mime_type": "image/jpeg", "data": b64("/tmp/kn_crop.jpg")}}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0}})
print("HTTP", r.status_code)
try:
    print("Gemini:", r.json()["candidates"][0]["content"]["parts"][0]["text"])
except Exception:
    print(r.text[:300])
