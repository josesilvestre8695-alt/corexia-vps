import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
BASE = os.getenv("ASAAS_BASE_URL", "https://api.asaas.com/v3").rstrip("/")
KEY = os.getenv("ASAAS_API_KEY", "")
H = {"access_token": KEY, "User-Agent": "Corexia/1.0"}
r = requests.get(BASE + "/webhooks", headers=H, timeout=30)
print("HTTP", r.status_code)
data = r.json().get("data", []) if r.ok else []
print("total webhooks:", len(data))
for i, w in enumerate(data, 1):
    print("%2d) id=%s | enabled=%s | interrupted=%s" % (i, w.get("id"), w.get("enabled"), w.get("interrupted")))
    print("      nome: %s" % w.get("name"))
    print("      url : %s" % w.get("url"))
    print("      eventos: %s" % ",".join(w.get("events") or []))
