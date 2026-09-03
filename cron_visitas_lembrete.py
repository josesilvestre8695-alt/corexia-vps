# -*- coding: utf-8 -*-
# Lembrete diario das visitas do dia (cliente + vendedores marcados). Roda ~08:00 (cron).
import sys, os, json, sqlite3, datetime
os.chdir("/opt/corexia/app"); sys.path.insert(0, "/opt/corexia/app")
try:
    from dotenv import load_dotenv
    load_dotenv("/opt/corexia/app/.env")
except Exception:
    pass
import comercial
DB = "/opt/corexia/app/corexia.db"
hoje = datetime.datetime.now().strftime("%Y-%m-%d")
conn = sqlite3.connect(DB)
rows = conn.execute("SELECT id,data FROM entities WHERE entity='Visita'").fetchall()
conn.close()
n = 0
for vid, d in rows:
    try:
        v = json.loads(d)
    except Exception:
        continue
    if v.get("provedor_id"):          # so nivel Corexia
        continue
    if v.get("status") != "agendada":
        continue
    if (v.get("data") or "") != hoje:
        continue
    if v.get("lembrete_enviado"):
        continue
    v["id"] = vid
    try:
        res = comercial._visita_enviar(v, "lembrete")
        comercial._update_ent("Visita", vid, {"lembrete_enviado": True, "lembrete_em": comercial._now_iso()})
        n += 1
        print(hoje, "lembrete:", v.get("nome"), v.get("hora"), "cliente=", res.get("cliente"),
              "vend_ok=", sum(1 for x in res.get("vendedores", []) if x.get("ok")))
    except Exception as e:
        print(hoje, "ERRO", vid, str(e)[:120])
print(hoje, "total lembretes enviados:", n)
