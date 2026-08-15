"""Cadastra cameras REAIS (YouTube live 24/7) via embed pra testar o painel."""
import requests
B = "http://localhost:8000"
cams = [
    {"nome": "NYC — Times Square", "embed_url": "https://www.youtube.com/embed/VGnFLdQW39A?autoplay=1&mute=1",
     "status": "online", "tipo": "externa", "localizacao": "Nova York, EUA"},
    {"nome": "Times Square — EarthCam", "embed_url": "https://www.youtube.com/embed/z-jYdOIKcTQ?autoplay=1&mute=1",
     "status": "online", "tipo": "externa", "localizacao": "Nova York, EUA"},
    {"nome": "Houston — Downtown", "embed_url": "https://www.youtube.com/embed/SDK_m1_BVJ4?autoplay=1&mute=1",
     "status": "online", "tipo": "externa", "localizacao": "Houston, EUA"},
]
existing = {c.get("nome") for c in requests.get(B + "/api/entities/Camera").json()}
for c in cams:
    if c["nome"] in existing:
        print("ja existe:", c["nome"]); continue
    r = requests.post(B + "/api/entities/Camera", json=c)
    print("cadastrada:", r.json().get("nome"), "| id:", r.json().get("id"))
