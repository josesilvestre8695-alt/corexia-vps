import os, requests
from dotenv import load_dotenv
load_dotenv()
KEY = os.getenv("GEMINI_API_KEY"); MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}"
try:
    r = requests.post(url, timeout=20, json={"contents": [{"parts": [{"text": "responda so: ok"}]}]})
    print("modelo:", MODEL, "| status:", r.status_code)
    print("resp:", str(r.json())[:400])
except Exception as e:
    print("erro:", e)
