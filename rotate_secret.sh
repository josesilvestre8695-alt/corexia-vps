#!/usr/bin/env bash
# Rotaciona o WEBHOOK_SECRET pra um valor forte (detector, gravador e backend leem o mesmo .env).
cd "$(dirname "$0")"
NEW=$(./venv/bin/python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
if grep -q '^WEBHOOK_SECRET=' .env; then
  sed -i "s|^WEBHOOK_SECRET=.*|WEBHOOK_SECRET=$NEW|" .env
else
  echo "WEBHOOK_SECRET=$NEW" >> .env
fi
grep -q '^ADMIN_PASSWORD=' .env || echo 'ADMIN_PASSWORD=corexia123' >> .env
echo "WEBHOOK_SECRET rotacionado (comprimento ${#NEW})"
