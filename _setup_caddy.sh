#!/usr/bin/env bash
# roda como root (via sudo -S)
echo "== DNS atual (via 8.8.8.8) =="
nslookup grupocorexia.com.br 8.8.8.8 2>/dev/null | grep -A1 'Name:' || echo "  raiz NAO resolve ainda"
nslookup www.grupocorexia.com.br 8.8.8.8 2>/dev/null | grep -A1 'Name:' || echo "  www NAO resolve ainda"
echo "== instala binario Caddy (se preciso) =="
if ! command -v caddy >/dev/null 2>&1; then
  curl -fsSL -o /usr/bin/caddy "https://caddyserver.com/api/download?os=linux&arch=amd64" || { echo "FALHA download caddy"; exit 1; }
  chmod +x /usr/bin/caddy
fi
echo "  $(/usr/bin/caddy version 2>/dev/null | head -1)"
id caddy >/dev/null 2>&1 || useradd --system --home /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
mkdir -p /etc/caddy /var/lib/caddy; chown -R caddy:caddy /var/lib/caddy
cat > /etc/caddy/Caddyfile <<'CF'
{
	email contato.heavenmkt@gmail.com
}
grupocorexia.com.br, www.grupocorexia.com.br {
	reverse_proxy 127.0.0.1:8000
}
CF
cat > /etc/systemd/system/caddy.service <<'UNIT'
[Unit]
Description=Caddy
After=network-online.target
Wants=network-online.target
[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/bin/caddy run --config /etc/caddy/Caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force
Restart=on-failure
TimeoutStopSec=5s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
[Install]
WantedBy=multi-user.target
UNIT
echo "== valida config =="
/usr/bin/caddy validate --config /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now caddy
sleep 3
echo "== estado =="
echo "  caddy: $(systemctl is-active caddy)"
ss -tlnp 2>/dev/null | grep -E ':80 |:443 ' | sed 's/^/  /' || echo "  (80/443 ainda nao)"
echo "== teste roteamento do dominio (http -> deve redirecionar p/ https) =="
curl -s -o /dev/null -w '  http Host grupocorexia -> %{http_code} (redirect esperado)\n' -H 'Host: grupocorexia.com.br' http://127.0.0.1/ || true
