"""Seed de demonstracao do SaaS multi-tenant (idempotente):
2 provedores + 3 clientes finais + logins + cameras YouTube atribuidas."""
import requests

B = "http://localhost:8000"

r = requests.post(B + "/api/auth/login", json={"email": "admin@corexia.com", "password": "corexia123"})
r.raise_for_status()
H = {"Authorization": "Bearer " + r.json()["token"]}

def ent_list(n):
    return requests.get(f"{B}/api/entities/{n}", headers=H).json()

def ent_create(n, d):
    return requests.post(f"{B}/api/entities/{n}", json=d, headers=H).json()

def ent_update(n, i, d):
    return requests.put(f"{B}/api/entities/{n}/{i}", json=d, headers=H).json()

# ---- provedores ----
provs = ent_list("Provedor")
def prov(nome, email, tel):
    p = next((x for x in provs if x.get("nome") == nome), None)
    if p:
        print("provedor ja existe:", nome); return p
    p = ent_create("Provedor", {"nome": nome, "email": email, "telefone": tel, "status": "ativo"})
    print("provedor criado:", nome); return p

p1 = prov("NetFibra Telecom", "contato@netfibra.com", "81999990001")
p2 = prov("VelozNet Internet", "contato@veloznet.com", "81999990002")

# ---- clientes finais ----
clis = ent_list("Cliente")
def cli(nome, email, tel, p):
    c = next((x for x in clis if x.get("nome") == nome), None)
    if c:
        print("cliente ja existe:", nome); return c
    c = ent_create("Cliente", {"nome": nome, "email": email, "telefone": tel, "status": "ativo",
                               "provedor_id": p["id"], "provedor_nome": p["nome"]})
    print("cliente criado:", nome, "->", p["nome"]); return c

c1 = cli("Mercadinho do Joao", "joao@teste.com", "81997335544", p1)
c2 = cli("Boutique da Maria", "maria@teste.com", "81997335544", p1)
c3 = cli("Padaria Central", "padaria@teste.com", "81997335544", p2)

# ---- atribui as cameras YouTube ----
cams = ent_list("Camera")
def atribui(nome_cam, p, c):
    cam = next((x for x in cams if x.get("nome") == nome_cam), None)
    if not cam:
        print("!! camera nao encontrada:", nome_cam); return
    ent_update("Camera", cam["id"], {
        "provedor_id": p["id"], "provedor_nome": p["nome"],
        "cliente_id": c["id"], "cliente_nome": c["nome"],
        "cliente_telefone": c.get("telefone", "")})
    print(f"camera '{nome_cam}' -> {p['nome']} / {c['nome']}")

atribui("NYC — Times Square", p1, c1)
atribui("Times Square — EarthCam", p1, c2)
atribui("Houston — Downtown", p2, c3)

# ---- logins ----
logins = [
    {"email": "netfibra@corexia.com", "password": "prov123", "full_name": "NetFibra Telecom",
     "role": "provedor", "provedor_id": p1["id"]},
    {"email": "veloznet@corexia.com", "password": "prov123", "full_name": "VelozNet Internet",
     "role": "provedor", "provedor_id": p2["id"]},
    {"email": "joao@teste.com", "password": "joao123", "full_name": "Joao (Mercadinho)",
     "role": "cliente", "cliente_id": c1["id"], "provedor_id": p1["id"]},
    {"email": "maria@teste.com", "password": "maria123", "full_name": "Maria (Boutique)",
     "role": "cliente", "cliente_id": c2["id"], "provedor_id": p1["id"]},
    {"email": "padaria@teste.com", "password": "pao123", "full_name": "Padaria Central",
     "role": "cliente", "cliente_id": c3["id"], "provedor_id": p2["id"]},
]
for l in logins:
    r = requests.post(B + "/api/users", json=l, headers=H)
    if r.status_code == 409:
        print("login ja existe:", l["email"])
    elif r.ok:
        print("login criado:", l["email"], f"({l['role']})")
    else:
        print("!! erro criando", l["email"], r.status_code, r.text[:100])

print("\nSEED OK")
