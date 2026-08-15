"""Testa a chave do Gemini + o 2o estagio: manda a imagem e pede confirmacao."""
import os, sys, base64, json, requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
img = sys.argv[1]
tipo = sys.argv[2] if len(sys.argv) > 2 else "arma_fogo"

b64 = base64.b64encode(open(img, "rb").read()).decode()
prompt = (f'Camera de seguranca. Um detector marcou possivel "{tipo}". '
          'Olhe a imagem e confirme. Responda SO JSON: '
          '{"confirmado": true/false, "descricao": "o que ve em 1 frase"}')
url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}"

r = requests.post(url, timeout=25, json={
    "contents": [{"parts": [
        {"text": prompt},
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
    ]}],
    "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
})
print("HTTP", r.status_code)
try:
    data = r.json()
    txt = data["candidates"][0]["content"]["parts"][0]["text"]
    print("Resposta do Gemini:", txt)
except Exception:
    print(r.text[:600])
