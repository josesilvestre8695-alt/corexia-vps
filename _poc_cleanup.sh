#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
./venv/bin/python - <<'PY'
import sqlite3, json
c=sqlite3.connect("corexia.db")
n=0
for rid,data in c.execute("SELECT id,data FROM entities WHERE entity='ConfigAnalitico'").fetchall():
    d=json.loads(data)
    if d.get("_poc"):
        c.execute("DELETE FROM entities WHERE id=?", (rid,)); n+=1
    else:
        changed=False
        for h in d.get("horarios",[]):
            if "epi" in (h.get("analiticos") or []):
                h["analiticos"]=[a for a in h["analiticos"] if a!="epi"]; changed=True
        if "epi" in (d.get("analiticos_padrao") or []):
            d["analiticos_padrao"]=[a for a in d["analiticos_padrao"] if a!="epi"]; changed=True
        if changed:
            c.execute("UPDATE entities SET data=? WHERE id=?", (json.dumps(d), rid))
c.commit()
print("PoC configs removidas:", n)
for rid,data in c.execute("SELECT id,data FROM entities WHERE entity='ConfigAnalitico'").fetchall():
    d=json.loads(data); print(" resta config:", d.get("camera_nome"), "| padrao:", d.get("analiticos_padrao"))
c.close()
PY
echo "(cleanup ok)"
