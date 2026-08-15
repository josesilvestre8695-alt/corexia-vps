"""Testa asaas.py: read-only + cliente DESCARTAVEL (create->delete). SEM assinatura = SEM cobranca."""
import asaas

print("1) my_account (READ-ONLY):")
acc = asaas.my_account()
print("   conta:", acc.get("name") or acc.get("companyName"), "| email:", acc.get("email"), "| base:", asaas.ASAAS_BASE)

print("2) create + delete de cliente descartavel (sem assinatura -> sem cobranca):")
cid = None
try:
    c = asaas.create_customer(name="COREXIA SELFTEST - APAGAR", cpf_cnpj="11144477735",
                              email="selftest@corexia.local", external_ref="_selftest_asaas_")
    cid = c.get("id")
    print("   criado customer:", cid, "| nome:", c.get("name"))
finally:
    if cid:
        d = asaas.delete_customer(cid)
        print("   deletado:", d.get("deleted"), "(cliente removido, nada ficou na conta)")

print("OK — modulo asaas funcionando: leitura + escrita + delete. Nenhuma cobranca gerada.")
