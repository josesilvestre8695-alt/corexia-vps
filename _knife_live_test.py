"""Teste ao vivo (3 min): captura frames, roda o modelo de faca, salva anotado+recorte
de cada deteccao e pergunta ao Gemini nos 2 melhores momentos. Tudo salvo em /tmp/klt/"""
import os, time, base64, json, shutil
os.environ["CUDA_VISIBLE_DEVICES"] = ""
import cv2, requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

API = os.environ["ROBOFLOW_API_KEY"]
KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
SRC = "/home/tvlan/corexia-vision-ai/gravacoes_live/6313df722d27e27d7999fb78/index.m3u8"
OUT = "/tmp/klt"
shutil.rmtree(OUT, ignore_errors=True); os.makedirs(OUT)

from inference import get_model
m = get_model(model_id="knives-detection/1", api_key=API)

dets = []
fim = time.time() + 180
n = 0
while time.time() < fim:
    cap = cv2.VideoCapture(SRC)
    ok, frame = cap.read(); cap.release()
    if not ok:
        time.sleep(2); continue
    n += 1
    res = m.infer(frame, confidence=0.25)[0]
    for p in res.predictions:
        ts = time.strftime("%H%M%S")
        conf = int(p.confidence * 100)
        anot = frame.copy()
        x1, y1 = int(p.x - p.width/2), int(p.y - p.height/2)
        cv2.rectangle(anot, (x1, y1), (x1+int(p.width), y1+int(p.height)), (0,0,255), 4)
        H, W = frame.shape[:2]
        mg = max(p.width, p.height) * 1.3 + 80
        cx1, cy1 = int(max(0, p.x-mg)), int(max(0, p.y-mg))
        cx2, cy2 = int(min(W, p.x+mg)), int(min(H, p.y+mg))
        base = f"{OUT}/{ts}_{conf}"
        cv2.imwrite(base + "_anot.jpg", anot, [cv2.IMWRITE_JPEG_QUALITY, 75])
        cv2.imwrite(base + "_crop.jpg", frame[cy1:cy2, cx1:cx2], [cv2.IMWRITE_JPEG_QUALITY, 85])
        dets.append((conf, base))
        print(f"[{ts}] Knives {conf}% @ ({int(p.x)},{int(p.y)})", flush=True)
    time.sleep(1.5)

print(f"\nframes analisados: {n} | deteccoes: {len(dets)}")
if not dets:
    print("NENHUMA deteccao no periodo."); raise SystemExit

def b64(pth): return base64.b64encode(open(pth, "rb").read()).decode()
prompt = ('Camera de seguranca. O detector marcou uma FACA ou outro objeto cortante (arma branca) '
          'na CAIXA VERMELHA da 1a imagem (a 2a e o RECORTE AMPLIADO). Julgue apenas a PRESENCA '
          'do objeto, nao a intencao: se visivel, confirme. Responda SO JSON: '
          '{"confirmado": true/false, "descricao": "1 frase"}')
for conf, base in sorted(dets, reverse=True)[:2]:
    try:
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}",
            timeout=25, json={"contents":[{"parts":[
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64(base+"_anot.jpg")}},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64(base+"_crop.jpg")}}]}],
                "generationConfig": {"response_mime_type": "application/json", "temperature": 0}})
        print(f"GEMINI p/ {base} ({conf}%):", r.json()["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as e:
        print("gemini erro:", str(e)[:80])
