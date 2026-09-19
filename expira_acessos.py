#!/usr/bin/env python3
# Expira acessos temporarios (Usuario Demonstrador + Provedor/Revenda Tester).
# Roda por cron 1x/dia (03h, crontab do corexia, venv python).
# - Demo: expira no dia seguinte a "expira".
# - Tester: no ULTIMO dia (vespera do bloqueio) manda WhatsApp de "ultimo dia";
#   bloqueia quando trial_ate <= hoje (= 15o dia; garante 14 dias completos de uso).
import sqlite3, json, datetime, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
try:
    import comercial as C   # p/ Z-API (aviso de ultimo dia)
except Exception as e:
    C = None
    print("[expira-acessos] aviso: comercial nao importou (%r) -> sem WhatsApp" % e)

DB = os.path.join(HERE, "corexia.db")
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
hoje = datetime.datetime.now().strftime("%Y-%m-%d")
amanha = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
now = datetime.datetime.now().isoformat()

MSG_ULTIMO_DIA = (
    "⏳ *%s, hoje e o ULTIMO dia do seu teste Corexia.*\n\n"
    "Nesses dias voce viu na pratica o que poucos entregam: cameras que *pensam* - "
    "detectam arma, invasao, fogo, aglomeracao e muito mais, e avisam na hora. "
    "Isso nao e so gravar pra ver depois. E *evitar antes*.\n\n"
    "A partir de amanha o seu acesso sera pausado. Mas manter tudo ligado e simples: "
    "e so ajustar o seu plano com a gente.\n\n"
    "\U0001F449 *Fale agora com o Grupo Corexia* e a gente libera a continuidade do seu acesso "
    "na hora, sem burocracia.\n\n"
    "Nao deixe suas cameras voltarem a ser so olhos que nao enxergam. "
    "Continue no mundo Corexia. \U0001F6E1\n\n"
    "_Equipe Corexia_"
)

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

# 2) provedores TESTER: aviso de ultimo dia (vespera) + bloqueio no 15o dia (trial_ate <= hoje)
nt = 0; nav = 0
for r in c.execute("SELECT id, data FROM entities WHERE entity='Provedor'").fetchall():
    d = json.loads(r["data"])
    if not (d.get("tester") and d.get("status") == "ativo"):
        continue
    ta = str(d.get("trial_ate") or "")
    if not ta:
        continue
    # 14o dia = vespera do bloqueio (trial_ate == amanha) -> WhatsApp de continuidade (1x)
    if ta == amanha and not d.get("aviso_ultimo_dia"):
        tel = (d.get("telefone") or "").strip()
        sent = False
        if tel and C is not None:
            try:
                primeiro = ((d.get("nome") or "").split(" ") or [""])[0] or "tudo bem"
                ok, _resp = C._zapi_send(tel, MSG_ULTIMO_DIA % primeiro)
                sent = bool(ok)
            except Exception as e:
                print("[expira-acessos] erro zapi tester %s: %r" % (r["id"], e))
        if sent:
            d["aviso_ultimo_dia"] = now
            c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='Provedor' AND id=?",
                      (json.dumps(d), now, r["id"]))
            nav += 1
        continue   # ainda ativo hoje; nao bloqueia
    # bloqueio no 15o dia
    if ta <= hoje:
        d["status"] = "bloqueado"; d["bloqueado_em"] = now
        c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='Provedor' AND id=?",
                  (json.dumps(d), now, r["id"]))
        c.execute("UPDATE users SET status='bloqueado' WHERE provedor_id=?", (r["id"],))
        for uu in c.execute("SELECT id FROM users WHERE provedor_id=?", (r["id"],)).fetchall():
            c.execute("DELETE FROM sessions WHERE user_id=?", (uu["id"],))
        nt += 1

c.commit(); c.close()
print("[expira-acessos]", now, "| demos expiradas:", nd, "| testers bloqueados:", nt, "| avisos ultimo dia:", nav)
