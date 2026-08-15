import requests, base64, time, sys
key = sys.argv[1]
img = base64.b64encode(open("/home/tvlan/corexia-vision-ai/gun.jpg", "rb").read()).decode()

def test(model):
    for i in range(5):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                json={"contents": [{"parts": [
                    {"text": "Ha arma de fogo nesta imagem? Responda em uma frase curta."},
                    {"inline_data": {"mime_type": "image/jpeg", "data": img}}]}],
                    "generationConfig": {"temperature": 0}}, timeout=30)
            d = r.json()
            if r.status_code == 200:
                return 200, d["candidates"][0]["content"]["parts"][0]["text"]
            if r.status_code == 503:
                time.sleep(4); continue
            return r.status_code, d.get("error", {}).get("message", "")[:120]
        except Exception as e:
            time.sleep(3)
    return 503, "sobrecarga/timeout persistente"

for m in ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-flash-latest",
          "gemini-3-flash-preview", "gemini-2.0-flash-lite"]:
    st, msg = test(m)
    print(m, "->", st, "|", str(msg)[:130])
    if st == 200:
        print("USAR:", m)
        break
