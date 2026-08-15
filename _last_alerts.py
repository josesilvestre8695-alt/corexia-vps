import sqlite3
c = sqlite3.connect("corexia.db"); c.row_factory = sqlite3.Row
cols = [r[1] for r in c.execute("PRAGMA table_info(alertas)")]
tcol = next((k for k in ("created", "data", "created_at", "timestamp", "ts", "criado") if k in cols), cols[-1])
for r in c.execute(f"select * from alertas order by id desc limit 6"):
    print(" ", str(r[tcol])[:19], "|", (r["tipo"] or "?").ljust(10), str(r["confianca"]) + "%", "|", r["camera_nome"], "| cliente:", r["cliente_nome"])
