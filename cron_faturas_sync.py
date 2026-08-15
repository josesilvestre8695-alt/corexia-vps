#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sincroniza faturas do Asaas (SO LEITURA) para os clientes/provedores JA VINCULADOS na Corexia.
- Lê apenas os asaas_customer_id cadastrados na Corexia (Cliente/Provedor); ignora todo o resto
  da conta compartilhada (Viggia, Aldeia, etc.). NÃO cadastra webhook, NÃO escreve no Asaas.
- Roda por cron (tvlan) a cada 30 min. _upsert_fatura resolve cliente_id/provedor_id.
"""
import os, sys, json, sqlite3
from datetime import datetime
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE); sys.path.insert(0, HERE)

try:
    import comercial
except Exception as e:
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ERRO import comercial:", e); sys.exit(1)

asaas = getattr(comercial, "asaas", None)
if asaas is None:
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "asaas indisponivel"); sys.exit(1)

# customers vinculados na Corexia (Cliente + Provedor), fora os de teste
custs = set()
db = sqlite3.connect(os.path.join(HERE, "corexia.db")); db.row_factory = sqlite3.Row
for ent in ("Cliente", "Provedor"):
    for r in db.execute("SELECT data FROM entities WHERE entity=?", (ent,)).fetchall():
        cid = json.loads(r["data"]).get("asaas_customer_id", "")
        if cid and not str(cid).startswith("TESTE_"):
            custs.add(cid)
db.close()

if not custs:
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "nenhum cliente vinculado ao Asaas - nada a sincronizar")
    sys.exit(0)

n = 0; err = 0
for cid in custs:
    try:
        pays = asaas.list_payments(customer_id=cid)   # so leitura, chave da Corexia
    except Exception as e:
        err += 1; print("  erro list_payments", cid, str(e)[:120]); continue
    data = pays.get("data") if isinstance(pays, dict) else pays
    for p in (data or []):
        try:
            comercial._upsert_fatura(p, "")   # resolve cliente_id/provedor_id pelo asaas_customer_id
            n += 1
        except Exception as e:
            err += 1; print("  erro upsert", p.get("id"), str(e)[:120])

print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
      "faturas sincronizadas:", n, "| customers Corexia:", len(custs), "| erros:", err)
