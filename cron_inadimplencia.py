#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Automacao diaria de inadimplencia (equivalente ao checkOverdueInvoices do Viggia).

Regras:
  - REGRA DE OURO: enquanto a promessa de pagamento estiver ativa e a data prometida
    ainda nao venceu (>= hoje), o cliente e PROTEGIDO (nao bloqueia).
  - Fatura vencida ha >= MIN_DIAS (10) dias, sem promessa valida -> BLOQUEIA (cascata de acesso).
  - Ao bloquear, marca bloqueio_auto=True e limpa a promessa.
  - AUTO-DESBLOQUEIO: cliente bloqueado AUTOMATICAMENTE (bloqueio_auto=True) que quitou tudo
    (sem faturas vencidas) e liberado. Substitui o webhook de pagamento do Asaas (opcao B).
  - NUNCA mexe em bloqueio MANUAL (bloqueio_auto ausente/False).

Uso:
  python cron_inadimplencia.py            # DRY-RUN (apenas relata o que faria)
  python cron_inadimplencia.py --apply    # efetiva bloqueios/desbloqueios
"""
import sys, json
from datetime import datetime, date
import comercial

APPLY = "--apply" in sys.argv
MIN_DIAS = 10
TODAY = date.today()


def _d(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return None


c = comercial._db()
clientes = [dict(json.loads(r["data"]), id=r["id"])
            for r in c.execute("SELECT id, data FROM entities WHERE entity='Cliente'").fetchall()]
fats = [json.loads(r["data"]) for r in c.execute("SELECT data FROM entities WHERE entity='Fatura'").fetchall()]
c.close()

# faturas vencidas por cliente (nivel 2 -> tem cliente_id)
venc = {}
for f in fats:
    if f.get("status") != "vencida":
        continue
    cid = f.get("cliente_id")
    if cid:
        venc.setdefault(cid, []).append(f)

bloquear, desbloquear = [], []
for cli in clientes:
    cid = cli["id"]
    status = cli.get("status") or "ativo"

    # --- auto-desbloqueio de quem foi bloqueado automaticamente e quitou tudo ---
    if status == "bloqueado":
        if cli.get("bloqueio_auto") and not venc.get(cid):
            desbloquear.append((cid, cli.get("nome", "")))
            if APPLY:
                comercial._update_ent("Cliente", cid, {
                    "status": "ativo", "block_reason": None, "bloqueio_auto": False,
                    "payment_promise_active": False, "payment_promise_date": None,
                    "payment_promise_set_by": None, "payment_promise_set_at": None})
                comercial._cliente_set_acesso(cid, True)
        continue

    # --- promessa valida protege ---
    pa = cli.get("payment_promise_active") is True and cli.get("payment_promise_date")
    pd = _d(cli.get("payment_promise_date")) if pa else None
    if pa and pd and pd >= TODAY:
        continue

    vs = venc.get(cid, [])
    if not vs:
        continue
    pior, dias = None, -1
    for f in vs:
        dv = _d(f.get("vencimento"))
        if dv is None:
            continue
        dd = (TODAY - dv).days
        if dd > dias:
            dias, pior = dd, f
    if pior is None or dias < MIN_DIAS:
        continue

    valor = float(pior.get("valor", 0) or 0)
    if pa and pd and pd <= TODAY:
        motivo = "Promessa de pagamento vencida em %s. Pagamento nao registrado. Valor: R$ %.2f." % (
            pd.strftime("%d/%m/%Y"), valor)
    else:
        dv = _d(pior.get("vencimento"))
        motivo = "Pagamento vencido ha %d dias. Valor: R$ %.2f. Vencimento: %s" % (
            dias, valor, dv.strftime("%d/%m/%Y") if dv else "-")
    bloquear.append((cid, cli.get("nome", ""), motivo))
    if APPLY:
        comercial._update_ent("Cliente", cid, {
            "status": "bloqueado", "block_reason": motivo, "bloqueio_auto": True,
            "payment_promise_active": False})
        comercial._cliente_set_acesso(cid, False)

print("[%s] %s | clientes=%d | com_vencida=%d | bloquear=%d | desbloquear=%d" % (
    datetime.now().isoformat(timespec="seconds"), "APPLY" if APPLY else "DRY-RUN",
    len(clientes), len(venc), len(bloquear), len(desbloquear)))
for cid, nome, m in bloquear:
    print("  BLOQUEAR    -", nome, "|", m)
for cid, nome in desbloquear:
    print("  DESBLOQUEAR -", nome)
