#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   backend: $(systemctl is-active corexia-backend)"
echo ">> escreve Caddyfile (temp) + copia via sudo"
cat > /tmp/Caddyfile.new <<'CF'
{
	email contato.heavenmkt@gmail.com
	on_demand_tls {
		ask http://127.0.0.1:8000/api/comercial/branding/domain-ok
	}
}

grupocorexia.com.br, www.grupocorexia.com.br {
	reverse_proxy 127.0.0.1:8000
}

https:// {
	tls {
		on_demand
	}
	reverse_proxy 127.0.0.1:8000
}
CF
echo tvlantvlan | sudo -S cp /tmp/Caddyfile.new /etc/caddy/Caddyfile && echo "   Caddyfile copiado"
rm -f /tmp/Caddyfile.new
echo ">> valida + reload"; echo tvlantvlan | sudo -S caddy validate --config /etc/caddy/Caddyfile 2>&1 | grep -iE 'valid|error'
echo tvlantvlan | sudo -S systemctl reload caddy; sleep 3; echo "   caddy: $(systemctl is-active caddy)"
echo ">> on_demand no Caddyfile?"; echo tvlantvlan | sudo -S grep -c on_demand /etc/caddy/Caddyfile
echo ">> TESTES"
echo -n "   domain-ok grupocorexia -> "; curl -s -o /dev/null -w '%{http_code} (esp 200)\n' "http://127.0.0.1:8000/api/comercial/branding/domain-ok?domain=grupocorexia.com.br"
echo -n "   domain-ok aleatorio    -> "; curl -s -o /dev/null -w '%{http_code} (esp 404)\n' "http://127.0.0.1:8000/api/comercial/branding/domain-ok?domain=nao-existe-xyz.com"
echo -n "   grupocorexia HTTPS ainda OK -> "; curl -sI --resolve www.grupocorexia.com.br:443:127.0.0.1 https://www.grupocorexia.com.br/Login | head -1
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "white-label: corrige ordem da rota domain-ok (antes de {pid}) + Caddy on-demand TLS aplicado"; fi
git log --oneline -1
