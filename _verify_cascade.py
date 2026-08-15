import requests
B = "http://localhost:8000"
adm = requests.post(B + "/api/auth/login", json={"email": "admin@corexia.com", "password": "corexia123"}).json()
H = {"Authorization": "Bearer " + adm["token"]}
clis = requests.get(B + "/api/entities/Cliente", headers=H).json()
joao = next(c for c in clis if "Joao" in c.get("nome", "") or "João" in c.get("nome", ""))

def login_joao():
    return requests.post(B + "/api/auth/login", json={"email": "joao@teste.com", "password": "joao123"}).status_code

print("login Joao ANTES (espera 200):", login_joao())
requests.put(f"{B}/api/entities/Cliente/{joao['id']}", json={"status": "suspenso"}, headers=H)
print("login Joao BLOQUEADO (espera 403):", login_joao())
requests.put(f"{B}/api/entities/Cliente/{joao['id']}", json={"status": "ativo"}, headers=H)
print("login Joao RESTAURADO (espera 200):", login_joao())
