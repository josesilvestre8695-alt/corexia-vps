import sqlite3, json
c = sqlite3.connect("corexia.db"); c.row_factory = sqlite3.Row
print("=== TABELAS ===")
for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    print("  ", r["name"])
print("\n=== USERS ===")
for r in c.execute("SELECT email,role,provedor_id,cliente_id,status FROM users"):
    print("  %-30s role=%-8s prov=%s cli=%s %s" % (r["email"], r["role"], (r["provedor_id"] or "")[:8], (r["cliente_id"] or "")[:8], r["status"]))
print("\n=== ENTITIES (contagem) ===")
for r in c.execute("SELECT entity,COUNT(*) n FROM entities GROUP BY entity"):
    print("  %-20s %s" % (r["entity"], r["n"]))
print("\n=== CAMERAS ===")
for r in c.execute("SELECT id,data FROM entities WHERE entity='Camera'"):
    d = json.loads(r["data"])
    src = "rtsp/http" if (d.get("rtsp_url") or d.get("stream_url")) else ("youtube" if d.get("embed_url") else "?")
    print("  %s | %-24s | prov=%s cli=%s | fonte=%s placa=%s" % (r["id"][:10], d.get("nome","?"), d.get("provedor_nome",""), d.get("cliente_nome",""), src, d.get("ia_placa")))
print("\n=== PROVEDORES ===")
for r in c.execute("SELECT id,data FROM entities WHERE entity='Provedor'"):
    d = json.loads(r["data"]); print("  %s | %s" % (r["id"][:10], d.get("nome","?")))
print("\n=== CLIENTES ===")
for r in c.execute("SELECT id,data FROM entities WHERE entity='Cliente'"):
    d = json.loads(r["data"]); print("  %s | %s | tel=%s prov=%s status=%s" % (r["id"][:10], d.get("nome","?"), d.get("telefone",""), d.get("provedor_nome",""), d.get("status","")))
print("\n=== ALERTAS ===")
print("  total:", c.execute("SELECT COUNT(*) n FROM alertas").fetchone()["n"])
for r in c.execute("SELECT tipo,COUNT(*) n FROM alertas GROUP BY tipo ORDER BY n DESC"):
    print("    %-12s %s" % (r["tipo"], r["n"]))
print("  ultimos 5:")
for r in c.execute("SELECT id,camera_nome,tipo,confianca,whatsapp,criado FROM alertas ORDER BY id DESC LIMIT 5"):
    print("    #%s %s %-18s %-10s %s%% wa=%s" % (r["id"], r["criado"], (r["camera_nome"] or "")[:18], r["tipo"], r["confianca"], r["whatsapp"]))
c.close()
