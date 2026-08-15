"""Confirma que os alertas de IA respeitam o escopo: cada cliente so ve os das cameras dele."""
import requests
B = "http://localhost:8000"

def login(e, p):
    r = requests.post(B + "/api/auth/login", json={"email": e, "password": p})
    return r.json()["token"] if r.ok else None

def alertas(tok):
    return requests.get(B + "/api/entities/Alerta", headers={"Authorization": "Bearer " + tok}).json()

for nome, email, senha in [("Joao (NetFibra)", "joao@teste.com", "joao123"),
                           ("Maria (NetFibra)", "maria@teste.com", "maria123"),
                           ("Padaria (VelozNet)", "padaria@teste.com", "pao123")]:
    tok = login(email, senha)
    al = alertas(tok) if tok else []
    cams = {}
    for a in al:
        cams[a.get("camera_nome", "?")] = cams.get(a.get("camera_nome", "?"), 0) + 1
    print(f"{nome}: {len(al)} alertas | cameras: {dict(cams)}")
