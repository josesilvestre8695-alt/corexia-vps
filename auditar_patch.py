"""Audita as premissas do patch contra o banco real (somente leitura)."""
import json
import sqlite3
import time

con = sqlite3.connect("file:corexia.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
agora = time.time()


def ents(nome):
    out = []
    for r in con.execute("SELECT data FROM entities WHERE entity=?", (nome,)):
        try:
            out.append(json.loads(r["data"]))
        except Exception:
            pass
    return out


print("=== VOLUME DE ALERTAS ===")
for rot, seg in (("ultima 1h", 3600), ("ultimas 6h", 6 * 3600), ("ultimas 24h", 86400)):
    n = con.execute(
        "SELECT COUNT(*) FROM entities WHERE entity='Alerta' AND created_date >= datetime('now', ?)",
        ("-%d seconds" % seg,),
    ).fetchone()[0]
    print("  %-12s %6d alertas  (%.0f/hora)" % (rot, n, n / (seg / 3600)))

print("\n=== ENTREGA DE WHATSAPP (ultimas 24h) ===")
alertas = [
    json.loads(r["data"])
    for r in con.execute(
        "SELECT data FROM entities WHERE entity='Alerta' AND created_date >= datetime('now','-1 day')"
    )
]
env = sum(1 for a in alertas if a.get("whatsapp_enviado"))
com_cli = sum(1 for a in alertas if a.get("cliente_id"))
print("  total de alertas:      %d" % len(alertas))
print("  com whatsapp enviado:  %d" % env)
print("  com cliente atribuido: %d" % com_cli)
por_tipo = {}
for a in alertas:
    por_tipo[a.get("tipo", "?")] = por_tipo.get(a.get("tipo", "?"), 0) + 1
print("  por tipo: %s" % dict(sorted(por_tipo.items(), key=lambda x: -x[1])))

print("\n=== IMPACTO DO ITEM 2 (WhatsApp so p/ camera COM cliente) ===")
cams = ents("Camera")
print("  cameras cadastradas:        %d" % len(cams))
com_cliente = [c for c in cams if c.get("cliente_id")]
print("  com cliente_id atribuido:   %d" % len(com_cliente))
print("  SEM cliente_id:             %d" % (len(cams) - len(com_cliente)))
ativas = [c for c in cams if c.get("ativa") or c.get("status") == "ativa"]
print("  marcadas como ativas:       %d" % len(ativas))
ativas_sem_cli = [c for c in ativas if not c.get("cliente_id")]
print("  ATIVAS e SEM cliente:       %d  <-- deixariam de notificar" % len(ativas_sem_cli))
for c in ativas_sem_cli[:8]:
    print("      - %s" % c.get("nome", "?"))

print("\n=== CLIENTES COM TELEFONE ===")
clientes = ents("Cliente")
print("  clientes: %d | com telefone: %d" % (
    len(clientes), sum(1 for c in clientes if c.get("telefone"))))

print("\n=== ALERTAS SEM CLIENTE QUE TERIAM SIDO NOTIFICADOS (24h) ===")
sem_cli_com_wpp = sum(1 for a in alertas if a.get("whatsapp_enviado") and not a.get("cliente_id"))
print("  %d alerta(s) foram enviados por WhatsApp sem cliente atribuido" % sem_cli_com_wpp)
print("  (esses parariam de ser enviados com o item 2)")
con.close()
