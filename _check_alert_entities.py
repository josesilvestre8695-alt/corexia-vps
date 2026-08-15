import sqlite3, json
c = sqlite3.connect("corexia.db"); c.row_factory = sqlite3.Row

print("=== entidades Alerta mais recentes (o que o PAINEL le) ===")
rows = c.execute("SELECT id, data, created_date FROM entities WHERE entity='Alerta' ORDER BY created_date DESC LIMIT 8").fetchall()
print("total de entidades Alerta:", c.execute("SELECT count(*) FROM entities WHERE entity='Alerta'").fetchone()[0])
for r in rows:
    d = json.loads(r["data"])
    print(" ", r["created_date"][:19], "|", (d.get("tipo") or "?").ljust(10),
          "|", d.get("camera_nome"), "| cliente:", d.get("cliente_nome"),
          "| img:", (d.get("imagem_url") or "(sem)")[:30], "| status:", d.get("status"))

print()
print("=== alertas da Camera Escritorio nas ENTIDADES ===")
n = 0
for r in c.execute("SELECT data, created_date FROM entities WHERE entity='Alerta'"):
    d = json.loads(r["data"])
    if "Escritorio" in (d.get("camera_nome") or ""):
        n += 1
        print(" ", r["created_date"][:19], d.get("tipo"), d.get("confianca"), "%", "| img:", d.get("imagem_url"))
print("total escritorio:", n)
