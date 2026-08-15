"""Cria a camera-demo de ARMA (stream local gun.jpg) atribuida ao NetFibra/Joao,
pra o alerta de IA cair no portal do cliente e provar o fluxo multi-tenant."""
import requests, sys
B = "http://localhost:8000"
r = requests.post(B + "/api/auth/login", json={"email": "admin@corexia.com", "password": "corexia123"})
r.raise_for_status()
H = {"Authorization": "Bearer " + r.json()["token"]}

provs = requests.get(B + "/api/entities/Provedor", headers=H).json()
clis = requests.get(B + "/api/entities/Cliente", headers=H).json()
cams = requests.get(B + "/api/entities/Camera", headers=H).json()
net = next((p for p in provs if p["nome"] == "NetFibra Telecom"), None)
joao = next((c for c in clis if "Joao" in c.get("nome", "") or "João" in c.get("nome", "")), None)
if not (net and joao):
    print("!! NetFibra ou Joao nao encontrados (rode _seed_saas.py antes)"); sys.exit(1)

nome = "Camera Demo IA (arma) - Loja"
if any(c.get("nome") == nome for c in cams):
    print("demo ja existe"); sys.exit(0)

cam = {"nome": nome, "rtsp_url": "http://localhost:8888/stream.m3u8", "status": "online", "tipo": "externa",
       "localizacao": "Loja (demonstracao)", "provedor_id": net["id"], "provedor_nome": net["nome"],
       "cliente_id": joao["id"], "cliente_nome": joao["nome"], "cliente_telefone": joao.get("telefone", "")}
o = requests.post(B + "/api/entities/Camera", json=cam, headers=H).json()
print("camera-demo criada:", o.get("nome"), "->", net["nome"], "/", joao["nome"], "| id:", o.get("id"))
