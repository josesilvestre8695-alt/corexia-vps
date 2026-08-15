import requests
B = "http://localhost:8000"
OLD_SECRET = "corexia-webhook-2024"
fails = []
def check(n, c, extra=""):
    print(("PASS  " if c else "FAIL  ") + n + (f"  [{extra}]" if extra else ""))
    if not c: fails.append(n)
def login(e, p):
    r = requests.post(B + "/api/auth/login", json={"email": e, "password": p}); return r.json() if r.ok else None
def H(s): return {"Authorization": "Bearer " + s["token"]}

adm = login("admin@corexia.com", "corexia123")
p1 = login("netfibra@corexia.com", "prov123")
p2 = login("veloznet@corexia.com", "prov123")
joao = login("joao@teste.com", "joao123")
check("logins dos 4 papeis ok", all([adm, p1, p2, joao]))

# secret rotacionado: o default publico NAO funciona mais
r = requests.post(B + "/listarCamerasIA", json={"secret": OLD_SECRET, "validar": False})
check("secret default publico REJEITADO (rotacionado)", r.status_code == 401, str(r.status_code))
check("sem login -> 401", requests.get(B + "/api/entities/Camera").status_code == 401)

# isolamento intacto
cam_p1 = requests.get(B + "/api/entities/Camera", headers=H(p1)).json()
cam_p2 = requests.get(B + "/api/entities/Camera", headers=H(p2)).json()
check("prov1 so ve cameras dele", all(c.get("provedor_id") == p1["user"]["provedor_id"] for c in cam_p1), f"{len(cam_p1)} cams")
check("prov2 so ve cameras dele", all(c.get("provedor_id") == p2["user"]["provedor_id"] for c in cam_p2), f"{len(cam_p2)} cams")
if cam_p2:
    check("prov1 NAO le camera do prov2 (403)", requests.get(f"{B}/api/entities/Camera/{cam_p2[0]['id']}", headers=H(p1)).status_code == 403)

# NOVO fix: provedor NAO edita a propria entidade Provedor (plano/cobranca)
pe = requests.get(B + "/api/entities/Provedor", headers=H(p1)).json()
if pe:
    r = requests.put(f"{B}/api/entities/Provedor/{pe[0]['id']}", json={"plano": "enterprise", "mensalidade": 0}, headers=H(p1))
    check("provedor NAO edita propria entidade Provedor (403)", r.status_code == 403, str(r.status_code))
# NOVO fix: provedor NAO cria login sem cliente_id
r = requests.post(B + "/api/users", json={"email": "z@z.com", "password": "1234", "role": "cliente"}, headers=H(p1))
check("provedor NAO cria login cliente sem dono (400/403)", r.status_code in (400, 403), str(r.status_code))
# NOVO fix: provedor NAO forja Alerta p/ cliente de outro provedor
cli_p2 = requests.get(B + "/api/entities/Cliente", headers=H(p2)).json()
if cli_p2:
    r = requests.post(B + "/api/entities/Alerta", json={"tipo": "arma_fogo", "cliente_id": cli_p2[0]["id"], "descricao": "forjado"}, headers=H(p1))
    check("provedor NAO forja Alerta p/ cliente de outro (403)", r.status_code == 403, str(r.status_code))

# cliente so ve o dele
cam_j = requests.get(B + "/api/entities/Camera", headers=H(joao)).json()
check("cliente Joao so ve cameras dele", all(c.get("cliente_id") == joao["user"]["cliente_id"] for c in cam_j), f"{len(cam_j)} cams")

print(f"\n{'>>> TUDO PASSOU' if not fails else '>>> ' + str(len(fails)) + ' FALHAS: ' + ', '.join(fails)}")
