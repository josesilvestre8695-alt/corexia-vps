#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo "== entidades no banco (nome + qtd) =="
./venv/bin/python - <<'PY'
import sqlite3, json
c=sqlite3.connect("corexia.db")
rows=c.execute("SELECT entity, COUNT(*) FROM entities GROUP BY entity ORDER BY 2 DESC").fetchall()
for e,n in rows: print("  %-30s %d" % (e,n))
print("\n== chaves das entidades candidatas (analit/analytic/camera/config) ==")
for (e,_n) in rows:
    if any(k in e.lower() for k in ['analit','analytic','config']) or e.lower()=='camera':
        r=c.execute("SELECT data FROM entities WHERE entity=? LIMIT 1",(e,)).fetchone()
        if r: print("  [%s] -> %s" % (e, list(json.loads(r[0]).keys())))
# se achar entidade de config de analitico, mostra 1 exemplo completo
for (e,_n) in rows:
    if 'analit' in e.lower() or 'analytic' in e.lower():
        r=c.execute("SELECT data FROM entities WHERE entity=? LIMIT 1",(e,)).fetchone()
        if r: print("\n== exemplo [%s] ==\n%s" % (e, json.dumps(json.loads(r[0]), ensure_ascii=False, indent=1)[:900]))
c.close()
PY
echo "== CAMERAS_URL que o detector consome =="
grep -nE 'CAMERAS_URL|CAMERAS_ENDPOINT|WEBHOOK_URL' detector_saas.py | head
echo "== rota de cameras no server.py (o que devolve pro detector) =="
grep -niE 'def .*camera|@app\.(post|get).*camera|secret.*validar|validar.*secret|stream_valido|ia_placa' server.py | head -20
