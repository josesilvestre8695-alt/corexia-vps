import sqlite3, os, json
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corexia.db")
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

print("=== INTEGRIDADE ===")
print("  integrity_check :", c.execute("PRAGMA integrity_check").fetchone()[0])
print("  foreign_key_check:", c.execute("PRAGMA foreign_key_check").fetchall() or "OK (sem FK)")
print("  journal_mode    :", c.execute("PRAGMA journal_mode").fetchone()[0], "  (WAL = melhor p/ concorrencia)")
print("  page_size       :", c.execute("PRAGMA page_size").fetchone()[0])
print("  tamanho arquivo :", round(os.path.getsize(DB)/1024, 1), "KB")

print("\n=== TABELAS ===")
tabs = [r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
for t in tabs:
    print(f"  {t:12}: {c.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]} linhas")
print("  indices        :", [r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()] or "(so os implicitos das PKs)")

print("\n=== ENTIDADES POR TIPO ===")
for r in c.execute("SELECT entity, COUNT(*) n FROM entities GROUP BY entity ORDER BY n DESC").fetchall():
    print(f"  {r['entity']:16}: {r['n']}")

print("\n=== SANIDADE / ORFAOS ===")
us = c.execute("SELECT id,email,role,provedor_id,cliente_id,status FROM users").fetchall()
emails = [u["email"] for u in us]
print("  usuarios:", len(us), "| emails duplicados:", len(emails) - len(set(emails)))
print("  cliente s/ cliente_id :", [u["email"] for u in us if u["role"] == "cliente" and not u["cliente_id"]] or "nenhum")
print("  provedor s/ provedor_id:", [u["email"] for u in us if u["role"] == "provedor" and not u["provedor_id"]] or "nenhum")
cli_ids = {r["id"] for r in c.execute("SELECT id FROM entities WHERE entity='Cliente'").fetchall()}
prov_ids = {r["id"] for r in c.execute("SELECT id FROM entities WHERE entity='Provedor'").fetchall()}
al = c.execute("SELECT data FROM entities WHERE entity='Alerta'").fetchall()
orf_c = sum(1 for a in al for d in [json.loads(a["data"])] if d.get("cliente_id") and d["cliente_id"] not in cli_ids)
print(f"  alertas c/ cliente_id inexistente: {orf_c} de {len(al)}")
# camera -> provedor/cliente validos?
cams = c.execute("SELECT data FROM entities WHERE entity='Camera'").fetchall()
cam_bad = sum(1 for a in cams for d in [json.loads(a["data"])]
              if (d.get("provedor_id") and d["provedor_id"] not in prov_ids) or (d.get("cliente_id") and d["cliente_id"] not in cli_ids))
print(f"  cameras c/ dono inexistente: {cam_bad} de {len(cams)}")
# JSON invalido?
bad_json = 0
for r in c.execute("SELECT data FROM entities").fetchall():
    try: json.loads(r["data"])
    except Exception: bad_json += 1
print("  registros c/ JSON invalido:", bad_json)
print("  sessoes ativas:", c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
c.close()
