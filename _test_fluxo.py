"""Teste E2E do fluxo de proposta em MODO TESTE (ASAAS_LIVE=0): nao cria nada no Asaas.
Envia 1 codigo real por WhatsApp (prova do 2FA) e limpa os registros de teste no fim."""
import os, json, sqlite3, requests
from dotenv import load_dotenv
load_dotenv(".env")
B = "http://localhost:8000"
email = (os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw = os.getenv("ADMIN_PASSWORD", "")
tok = requests.post(B + "/api/auth/login", json={"email": email, "password": pw}, timeout=10).json().get("token", "")
if not tok:
    print("login admin falhou"); raise SystemExit
H = {"Authorization": "Bearer " + tok}

planos = requests.get(B + "/api/entities/Plano", headers=H, timeout=10).json()
plano = planos[0] if planos else {}
print("plano usado:", plano.get("nome"), "R$", plano.get("valor_mensal"))

prop = {"cliente_nome": "TESTE FLUXO - APAGAR", "document_type": "cnpj", "document_number": "11444777000161",
        "email": "teste@corexia.local", "whatsapp": "81997335544",
        "plano_id": plano.get("id", ""), "plano_nome": plano.get("nome", ""), "tipo_plano": plano.get("tipo", ""),
        "contrato_meses": 36, "qtd_cameras": 10, "valor_mensal": plano.get("valor_mensal", 0),
        "consultor": "selftest", "status": "pendente"}
pid = requests.post(B + "/api/entities/Proposta", headers=H, json=prop, timeout=10).json().get("id")
print("1) proposta criada:", pid)

requests.put(B + "/api/entities/Proposta/" + pid, headers=H, json={"status": "aprovada"}, timeout=10)
print("2) aprovada")

r = requests.post(B + "/api/comercial/propostas/%s/enviar-codigo" % pid, headers=H, timeout=35).json()
print("3) enviar-codigo ->", r)

c = sqlite3.connect("corexia.db")
d = json.loads(c.execute("SELECT data FROM entities WHERE entity='Proposta' AND id=?", (pid,)).fetchone()[0]); c.close()
code = d.get("_sig_code")
print("   codigo gravado:", code, "| status:", d.get("status"))

r = requests.post(B + "/api/comercial/propostas/%s/assinar" % pid, headers=H, json={"codigo": code}, timeout=35).json()
print("4) assinar ->", r)

c = sqlite3.connect("corexia.db")
d = json.loads(c.execute("SELECT data FROM entities WHERE entity='Proposta' AND id=?", (pid,)).fetchone()[0]); c.close()
print("   proposta agora: status=%s modo=%s asaas_customer=%s" % (d.get("status"), d.get("signature_modo"), d.get("asaas_customer_id")))

alvo = r.get("alvo"); alvo_id = r.get("alvo_id")
c = sqlite3.connect("corexia.db")
c.execute("DELETE FROM entities WHERE entity='Proposta' AND id=?", (pid,))
if alvo and alvo_id:
    c.execute("DELETE FROM entities WHERE entity=? AND id=?", (alvo, alvo_id))
c.commit(); c.close()
print("5) cleanup: proposta + %s de teste apagados. OK." % alvo)
