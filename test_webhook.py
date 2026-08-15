"""
Teste rapido do webhookAlertas do Base44.
Dispara um alerta FAKE so pra confirmar que a URL + secret estao certas
e que o alerta aparece no painel Corexia -- SEM precisar de GPU, camera ou modelo.

Uso:
  python test_webhook.py                                  (le WEBHOOK_URL/SECRET do .env)
  python test_webhook.py https://.../webhookAlertas       (passa a URL direto)
  python test_webhook.py https://.../webhookAlertas SEGREDO
"""
import os, sys, json, urllib.request

url = None
secret = "corexia-webhook-2024"

# 1) tenta ler do .env se existir
if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if line.startswith("WEBHOOK_URL="):
            url = line.split("=", 1)[1].strip()
        elif line.startswith("WEBHOOK_SECRET="):
            secret = line.split("=", 1)[1].strip()

# 2) argumentos sobrescrevem
if len(sys.argv) > 1:
    url = sys.argv[1]
if len(sys.argv) > 2:
    secret = sys.argv[2]

if not url or "SEU-APP" in url:
    print("Falta a URL. Copie da aba Deploy da funcao webhookAlertas no Base44 e rode:")
    print("  python test_webhook.py https://SEU-APP.base44.app/functions/webhookAlertas")
    sys.exit(1)

payload = json.dumps({
    "secret": secret,
    "camera_nome": "CAMERA DE TESTE",
    "tipo": "arma_fogo",
    "descricao": "[TESTE] alerta fake de validacao -- pode resolver/ignorar",
    "confianca": 99,
}).encode()

print(f"POST -> {url}")
req = urllib.request.Request(url, data=payload,
                            headers={"Content-Type": "application/json",
                                     "User-Agent": "Mozilla/5.0"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print("HTTP", resp.status)
        print(resp.read().decode()[:500])
        print("\n[OK] Enviado! Abra a pagina de Alertas no painel Corexia")
        print("     e veja o alerta da 'CAMERA DE TESTE'.")
except Exception as e:
    print("[FALHOU]", repr(e))
    print("\nConfira:")
    print(" - WEBHOOK_URL: copie a URL publica exata da funcao webhookAlertas (aba Deploy no Base44)")
    print(" - WEBHOOK_SECRET: deve bater com a env WEBHOOK_SECRET das functions do Base44")
