#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   backend: $(systemctl is-active corexia-backend)"
echo ">> atualiza Caddyfile (on-demand TLS p/ dominios de provedor)"
echo tvlantvlan | sudo -S tee /etc/caddy/Caddyfile >/dev/null <<'CF'
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
echo ">> valida Caddyfile"; echo tvlantvlan | sudo -S caddy validate --config /etc/caddy/Caddyfile 2>&1 | grep -iE 'valid|error' | tail -2
echo ">> reload caddy"; echo tvlantvlan | sudo -S systemctl reload caddy; sleep 3; echo "   caddy: $(systemctl is-active caddy)"
echo ">> TESTES"
echo -n "   domain-ok grupocorexia -> "; curl -s -o /dev/null -w '%{http_code} (esp 200)\n' "http://127.0.0.1:8000/api/comercial/branding/domain-ok?domain=grupocorexia.com.br"
echo -n "   domain-ok aleatorio    -> "; curl -s -o /dev/null -w '%{http_code} (esp 404)\n' "http://127.0.0.1:8000/api/comercial/branding/domain-ok?domain=nao-existe-xyz.com"
echo -n "   tela Provedor/Revenda tem botao marca (ocorrencias) -> "; curl -s http://localhost:8000/comercial/clientes | grep -oc 'marca('
echo -n "   grupocorexia HTTPS ainda OK -> "; curl -sI --resolve www.grupocorexia.com.br:443:127.0.0.1 https://www.grupocorexia.com.br/Login | head -1
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "white-label: tela Marca (cor/menu/logo/dominio) + endpoint domain-ok + Caddy on-demand TLS p/ dominios proprios de provedor"; fi
git log --oneline -1
