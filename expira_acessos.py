#!/usr/bin/env python3
# Expira acessos temporarios (Usuario Demonstrador + Provedor/Revenda Tester).
# Roda por cron 1x/dia (03h). A demo ja expira em tempo real no backend (checa data); aqui formaliza.
# O tester e bloqueado por este cron (users.status=bloqueado + derruba sessoes + Provedor.status=bloqueado).
import sqlite3, json, datetime
import os
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corexia.db")
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
hoje = datetime.datetime.now().strftime("%Y-%m-%d"); now = datetime.datetime.now().isoformat()

# 1) demonstradores vencidos
nd = 0
for r in c.execute("SELECT id, data FROM entities WHERE entity='AcessoDemo'").fetchall():
    d = json.loads(r["data"])
    if d.get("status") == "ativo" and str(d.get("expira") or "") and str(d.get("expira")) < hoje:
        d["status"] = "expirado"; d["expirado_em"] = now
        c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='AcessoDemo' AND id=?",
                  (json.dumps(d), now, r["id"]))
        c.execute("DELETE FROM sessions WHERE user_id=?", (d.get("user_id"),))
        nd += 1

# 2) provedores TESTER com trial vencido -> bloqueia painel
nt = 0
for r in c.execute("SELECT id, data FROM entities WHERE entity='Provedor'").fetchall():
    d = json.loads(r["data"])
    if d.get("tester") and d.get("status") == "ativo" and str(d.get("trial_ate") or "") and str(d.get("trial_ate")) < hoje:
        d["status"] = "bloqueado"; d["bloqueado_em"] = now
        c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='Provedor' AND id=?",
                  (json.dumps(d), now, r["id"]))
        c.execute("UPDATE users SET status='bloqueado' WHERE provedor_id=?", (r["id"],))
        for uu in c.execute("SELECT id FROM users WHERE provedor_id=?", (r["id"],)).fetchall():
            c.execute("DELETE FROM sessions WHERE user_id=?", (uu["id"],))
        nt += 1

c.commit(); c.close()
print("[expira-acessos]", now, "| demos expiradas:", nd, "| testers bloqueados:", nt)
