"""Corexia — cliente Asaas (cobranca em 2 niveis).

Nivel 1 (Corexia -> Provedor): usa a chave da Corexia (ASAAS_API_KEY do .env).
Nivel 2 (Provedor -> Cliente final): passa a chave DO provedor (prov['asaas_api_key']).
    -> quem chama decide a chave via o parametro api_key.

Base trocavel por ASAAS_BASE_URL (sandbox <-> producao numa linha).
Docs: https://docs.asaas.com/  (auth: header access_token)
"""
import os, requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

ASAAS_BASE = os.getenv("ASAAS_BASE_URL", "https://api.asaas.com/v3").rstrip("/")
ASAAS_KEY_COREXIA = os.getenv("ASAAS_API_KEY", "")   # chave da Corexia (Nivel 1)


class AsaasError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"Asaas HTTP {status}: {str(body)[:300]}")


def _only_digits(s):
    return "".join(c for c in str(s or "") if c.isdigit())


def _headers(api_key):
    return {"access_token": api_key or ASAAS_KEY_COREXIA,
            "Content-Type": "application/json",
            "User-Agent": "Corexia/1.0"}


def _req(method, path, api_key=None, **kw):
    r = requests.request(method, f"{ASAAS_BASE}{path}", headers=_headers(api_key), timeout=30, **kw)
    if r.status_code >= 300:
        try:
            body = r.json()
        except Exception:
            body = r.text
        raise AsaasError(r.status_code, body)
    return r.json() if r.text else {}


# ---------- leitura ----------
def my_account(api_key=None):
    """READ-ONLY: dados da conta (valida a chave sem criar nada)."""
    return _req("GET", "/myAccount", api_key)


def get_subscription(sub_id, api_key=None):
    return _req("GET", f"/subscriptions/{sub_id}", api_key)


def list_payments(customer_id=None, subscription_id=None, status=None, api_key=None):
    params = {}
    if customer_id:     params["customer"] = customer_id
    if subscription_id: params["subscription"] = subscription_id
    if status:          params["status"] = status
    return _req("GET", "/payments", api_key, params=params)


def list_payments_page(offset=0, limit=100, api_key=None, extra=None):
    """Pagina de cobrancas (READ-ONLY). extra p/ filtros ex.: {'dueDate[ge]': '2026-01-01'}."""
    params = {"offset": offset, "limit": limit, "order": "desc"}
    if extra:
        params.update(extra)
    return _req("GET", "/payments", api_key, params=params)


def list_customers_page(offset=0, limit=100, api_key=None):
    return _req("GET", "/customers", api_key, params={"offset": offset, "limit": limit})


# ---------- escrita ----------
def create_customer(*, name, cpf_cnpj, email="", phone="", external_ref="", api_key=None, **extra):
    body = {"name": name, "cpfCnpj": _only_digits(cpf_cnpj)}
    if email:        body["email"] = email
    if phone:        body["mobilePhone"] = _only_digits(phone)
    if external_ref: body["externalReference"] = str(external_ref)
    body.update(extra)
    return _req("POST", "/customers", api_key, json=body)


def create_subscription(*, customer, value, next_due_date, cycle="MONTHLY",
                        billing_type="BOLETO", max_payments=None, description="",
                        external_ref="", fine_pct=2.0, interest_pct=1.0, api_key=None, **extra):
    """Assinatura recorrente. ATENCAO: gera cobrancas REAIS na conta da chave usada."""
    body = {
        "customer": customer,
        "value": round(float(value), 2),
        "nextDueDate": next_due_date,          # 'YYYY-MM-DD'
        "cycle": cycle,                        # MONTHLY
        "billingType": billing_type,           # BOLETO | PIX | CREDIT_CARD | UNDEFINED
        "fine": {"value": fine_pct, "type": "PERCENTAGE"},
        "interest": {"value": interest_pct, "type": "PERCENTAGE"},
    }
    if max_payments: body["maxPayments"] = int(max_payments)
    if description:  body["description"] = description
    if external_ref: body["externalReference"] = str(external_ref)
    body.update(extra)
    return _req("POST", "/subscriptions", api_key, json=body)


def create_payment(*, customer, value, due_date, billing_type="UNDEFINED",
                   description="", external_ref="", fine_pct=2.0, interest_pct=1.0,
                   api_key=None, **extra):
    """Cobranca AVULSA (uma unica parcela). Resposta traz invoiceUrl (pagina de pagamento).
    ATENCAO: gera cobranca REAL na conta da chave usada."""
    body = {
        "customer": customer,
        "value": round(float(value), 2),
        "dueDate": due_date,                   # 'YYYY-MM-DD'
        "billingType": billing_type,           # BOLETO | PIX | CREDIT_CARD | UNDEFINED
        "fine": {"value": fine_pct, "type": "PERCENTAGE"},
        "interest": {"value": interest_pct, "type": "PERCENTAGE"},
    }
    if description:  body["description"] = description
    if external_ref: body["externalReference"] = str(external_ref)
    body.update(extra)
    return _req("POST", "/payments", api_key, json=body)


# ---------- limpeza (testes) ----------
def delete_customer(cid, api_key=None):
    return _req("DELETE", f"/customers/{cid}", api_key)


def delete_subscription(sid, api_key=None):
    return _req("DELETE", f"/subscriptions/{sid}", api_key)


def pix_qrcode(payment_id, api_key=None):
    """QR / copia-e-cola do PIX de uma cobranca avulsa (READ-ONLY)."""
    return _req("GET", "/payments/%s/pixQrCode" % payment_id, api_key)
