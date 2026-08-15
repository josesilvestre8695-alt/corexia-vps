"""Teste e2e de ISOLAMENTO multi-tenant. Roda na Xeon contra localhost:8000.
Sai com codigo 1 se qualquer checagem falhar."""
import requests, sys

B = "http://localhost:8000"
fails = []

def check(nome, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + nome + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(nome)

def login(e, p):
    r = requests.post(B + "/api/auth/login", json={"email": e, "password": p})
    return r.json() if r.ok else None

def H(s):
    return {"Authorization": "Bearer " + s["token"]}

def lista(s, n):
    r = requests.get(f"{B}/api/entities/{n}", headers=H(s))
    return r.json() if r.ok else []

adm = login("admin@corexia.com", "corexia123")
prov1 = login("netfibra@corexia.com", "prov123")
prov2 = login("veloznet@corexia.com", "prov123")
joao = login("joao@teste.com", "joao123")
check("login admin", bool(adm))
check("login provedor1 (NetFibra)", bool(prov1))
check("login provedor2 (VelozNet)", bool(prov2))
check("login cliente final (Joao)", bool(joao))
check("senha errada rejeitada", login("admin@corexia.com", "senha-errada") is None)
if not all([adm, prov1, prov2, joao]):
    print("logins basicos falharam, abortando"); sys.exit(1)

p1id = prov1["user"]["provedor_id"]; p2id = prov2["user"]["provedor_id"]
cjid = joao["user"]["cliente_id"]

# --- sem autenticacao ---
check("sem login -> 401 nas entidades", requests.get(B + "/api/entities/Camera").status_code == 401)
check("sem login -> 401 no /api/users", requests.get(B + "/api/users").status_code == 401)
check("sem login -> 401 nas gravacoes", requests.get(B + "/api/gravacoes/cameras").status_code == 401)

# --- visibilidade de cameras ---
cam_adm = lista(adm, "Camera"); cam_p1 = lista(prov1, "Camera")
cam_p2 = lista(prov2, "Camera"); cam_cj = lista(joao, "Camera")
check("admin ve TODAS as cameras (3)", len(cam_adm) == 3, f"viu {len(cam_adm)}")
check("prov1 ve SO as 2 dele", len(cam_p1) == 2 and all(c.get("provedor_id") == p1id for c in cam_p1), f"viu {len(cam_p1)}")
check("prov2 ve SO a 1 dele", len(cam_p2) == 1 and all(c.get("provedor_id") == p2id for c in cam_p2), f"viu {len(cam_p2)}")
check("cliente Joao ve SO a camera dele", len(cam_cj) == 1 and all(c.get("cliente_id") == cjid for c in cam_cj), f"viu {len(cam_cj)}")

# --- visibilidade de clientes ---
cli_p1 = lista(prov1, "Cliente"); cli_p2 = lista(prov2, "Cliente"); cli_cj = lista(joao, "Cliente")
check("prov1 ve SO os 2 clientes dele", len(cli_p1) == 2 and all(c.get("provedor_id") == p1id for c in cli_p1), f"viu {len(cli_p1)}")
check("prov2 ve SO o 1 cliente dele", len(cli_p2) == 1, f"viu {len(cli_p2)}")
check("Joao ve SO o proprio cadastro", len(cli_cj) == 1 and cli_cj[0]["id"] == cjid, f"viu {len(cli_cj)}")

# --- ataques cruzados ---
if cam_p2:
    alvo = cam_p2[0]["id"]
    r = requests.get(f"{B}/api/entities/Camera/{alvo}", headers=H(prov1))
    check("prov1 NAO le camera do prov2 (403)", r.status_code == 403, str(r.status_code))
    r = requests.put(f"{B}/api/entities/Camera/{alvo}", json={"cliente_id": "hack"}, headers=H(prov1))
    check("prov1 NAO edita camera do prov2 (403)", r.status_code == 403, str(r.status_code))
r = requests.post(f"{B}/api/entities/Camera", json={"nome": "cam-hack"}, headers=H(prov1))
check("provedor NAO cria camera (403)", r.status_code == 403, str(r.status_code))
if cli_p2 and cam_p1:
    r = requests.put(f"{B}/api/entities/Camera/{cam_p1[0]['id']}",
                     json={"cliente_id": cli_p2[0]["id"]}, headers=H(prov1))
    check("prov1 NAO atribui camera a cliente do prov2 (403)", r.status_code == 403, str(r.status_code))
r = requests.post(f"{B}/api/entities/Cliente", json={"nome": "hack"}, headers=H(joao))
check("cliente final NAO cria Cliente (403)", r.status_code == 403, str(r.status_code))
if cam_p1:
    r = requests.delete(f"{B}/api/entities/Camera/{cam_p1[0]['id']}", headers=H(prov1))
    check("provedor NAO exclui camera (403)", r.status_code == 403, str(r.status_code))
# provedor tentando roubar camera: mudar provedor_id de camera dele (deve ser ignorado)
if cam_p1:
    requests.put(f"{B}/api/entities/Camera/{cam_p1[0]['id']}", json={"provedor_id": "outro"}, headers=H(prov1))
    depois = requests.get(f"{B}/api/entities/Camera/{cam_p1[0]['id']}", headers=H(adm)).json()
    check("provedor_id nao muda por nao-admin", depois.get("provedor_id") == p1id, depois.get("provedor_id", ""))

# --- /api/users escopado ---
us_p1 = requests.get(B + "/api/users", headers=H(prov1)).json()
check("prov1 ve SO logins de clientes dele",
      all(u["provedor_id"] == p1id and u["role"] == "cliente" for u in us_p1), f"{len(us_p1)} logins")
r = requests.post(B + "/api/users", json={"email": "h4ck@x.com", "password": "1234", "role": "admin"}, headers=H(prov1))
check("prov1 NAO cria login admin (403)", r.status_code == 403, str(r.status_code))
r = requests.post(B + "/api/users", json={"email": "h4ck2@x.com", "password": "1234", "role": "cliente",
                                          "cliente_id": (cli_p2[0]["id"] if cli_p2 else "x")}, headers=H(prov1))
check("prov1 NAO cria login p/ cliente do prov2 (403)", r.status_code == 403, str(r.status_code))

# --- alertas escopados ---
al_cj = lista(joao, "Alerta")
check("alertas do Joao todos dele", all(a.get("cliente_id") == cjid for a in al_cj), f"{len(al_cj)} alertas")
al_p1 = lista(prov1, "Alerta")
check("alertas do prov1 todos dele", all(a.get("provedor_id") == p1id for a in al_p1), f"{len(al_p1)} alertas")

# --- gravacoes escopadas ---
g_adm = requests.get(B + "/api/gravacoes/cameras", headers=H(adm)).json()
g_cj = requests.get(B + "/api/gravacoes/cameras", headers=H(joao)).json()
check("gravacoes: cliente ve no maximo as cams dele",
      all(g["camera_id"] in {c["id"] for c in cam_cj} for c in [g_cj] for g in g_cj), f"{len(g_cj)} cams c/ gravacao")
if g_adm:
    fora = [g for g in g_adm if g["camera_id"] not in {c["id"] for c in cam_cj}]
    if fora:
        r = requests.get(f"{B}/api/gravacoes?camera_id={fora[0]['camera_id']}&data=2099-01-01", headers=H(joao))
        check("cliente NAO lista gravacao de camera alheia (403)", r.status_code == 403, str(r.status_code))

print(f"\n{'TODOS OS TESTES PASSARAM' if not fails else str(len(fails)) + ' FALHAS: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
