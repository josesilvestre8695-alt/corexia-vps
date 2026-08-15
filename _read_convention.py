import sqlite3, json
c = sqlite3.connect("corexia.db")
for ent in ("Contrato", "Fatura", "Vendedor", "Plano", "Cliente"):
    rows = c.execute("SELECT id,data FROM entities WHERE entity=? LIMIT 2", (ent,)).fetchall()
    print(f"\n===== {ent} ({len(rows)} amostra) =====")
    for (eid, d) in rows:
        o = json.loads(d)
        print("  keys:", sorted(o.keys()))
        # mostra campos que parecem valor/preco/camera/contrato
        interesse = {k: v for k, v in o.items() if any(t in k.lower() for t in
                     ("valor","preco","price","value","mensal","camera","contrato","meses","doc","tipo","nome","status"))}
        print("  interesse:", json.dumps(interesse, ensure_ascii=False)[:400])
c.close()
