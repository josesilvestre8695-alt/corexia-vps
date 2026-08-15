#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> limpa faturas antigas (misturadas com outros produtos da conta)"
./venv/bin/python - <<'PY'
import sqlite3
c=sqlite3.connect("corexia.db")
n=c.execute("SELECT COUNT(*) FROM entities WHERE entity='Fatura'").fetchone()[0]
c.execute("DELETE FROM entities WHERE entity='Fatura'")
c.commit()
m=c.execute("SELECT COUNT(*) FROM entities WHERE entity='Fatura'").fetchone()[0]
c.close()
print("   faturas antes: %d -> depois: %d" % (n, m))
PY
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   backend: $(systemctl is-active corexia-backend) | /comercial/faturas -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000/comercial/faturas)"
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "asaas: isola faturamento da Corexia na conta compartilhada (webhook ignora cliente desconhecido; sync so clientes da Corexia); limpa faturas antigas misturadas"; fi
git log --oneline -1
