import sqlite3, json
c = sqlite3.connect("corexia.db"); c.row_factory = sqlite3.Row
cols = [r[1] for r in c.execute("PRAGMA table_info(entities)")]
print("cols entities:", cols)

rows = [dict(r) for r in c.execute("select * from entities")]
print("total entities:", len(rows))

# descobre a coluna de tipo e a de dados
tipo_col = next((k for k in ("tipo","kind","entity","entity_type","type","collection") if k in cols), None)
data_col = next((k for k in ("data","json","payload","body","doc") if k in cols), None)
print("tipo_col=", tipo_col, "| data_col=", data_col)

def parse(r):
    raw = r.get(data_col) if data_col else None
    if isinstance(raw, str):
        try: return json.loads(raw)
        except: return {}
    return raw or {}

# conta por tipo
from collections import Counter
tipos = Counter(r.get(tipo_col) for r in rows) if tipo_col else Counter()
print("tipos:", dict(tipos))

print("\n=== CAMERAS ===")
for r in rows:
    if tipo_col and str(r.get(tipo_col)).lower() != "camera":
        continue
    d = parse(r)
    print("  id=", r.get("id"), "| nome=", d.get("nome"), "| stream=", (d.get("stream_url") or d.get("rtsp_url") or "")[:40],
          "| cliente=", d.get("cliente_id"), "| deleted=", d.get("deleted"), "| status=", d.get("status"))
