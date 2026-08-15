"""Diagnostico: o detector de IA esta vendo/analisando as cameras?"""
import requests
B = "http://localhost:8000"
SECRET = "corexia-webhook-2024"
r = requests.post(B + "/listarCamerasIA", json={"secret": SECRET, "validar": True}, timeout=60)
d = r.json()
print("total cameras:", d.get("total"), "| streams validas p/ IA:", d.get("online_validas"))
for c in d.get("cameras", []):
    print(f"  - {c.get('nome'):28} stream_url={'SIM' if c.get('stream_url') else 'NAO':4} valido={c.get('stream_valido')}")
if d.get("online_validas", 0) == 0:
    print("\n>>> A IA NAO tem nenhuma camera com stream direto pra analisar (YouTube e' embed).")
