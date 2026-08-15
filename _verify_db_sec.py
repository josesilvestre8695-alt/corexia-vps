import requests, sqlite3, os
B = "http://localhost:8000"
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corexia.db")

c = sqlite3.connect(DB)
print("journal_mode :", c.execute("PRAGMA journal_mode").fetchone()[0], " (esperado: wal)")
print("sessoes antes:", c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
c.close()

r = requests.post(B + "/api/auth/login", json={"email": "admin@corexia.com", "password": "corexia123"})
print("login admin correto:", r.status_code, "(esperado 200)")

print("=== rate-limit: 10 logins ERRADOS do mesmo IP ===")
codes = []
for i in range(10):
    rr = requests.post(B + "/api/auth/login", json={"email": "admin@corexia.com", "password": f"errada{i}"})
    codes.append(rr.status_code)
print("codigos:", codes, "-> deve virar 429 (bloqueio) no fim")
print(">>>", "RATE-LIMIT OK" if 429 in codes else "SEM RATE-LIMIT!")
