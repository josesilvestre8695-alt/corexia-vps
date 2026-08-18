#!/usr/bin/env bash
# Empurra pro VPS quais stream_keys estao publicando agora no MediaMTX local (p/ o alerta offline).
# So empurra se o MediaMTX respondeu (se estiver fora, NAO empurra -> VPS trata como indisponivel, sem falso alarme).
SEC=$(cat /opt/corexia/webhook_secret 2>/dev/null)
[ -z "$SEC" ] && exit 1
RESP=$(curl -s -m5 http://127.0.0.1:9997/v3/paths/list)
[ -z "$RESP" ] && exit 0
READY=$(printf '%s' "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print(json.dumps([i['name'][4:] for i in d.get('items',[]) if i.get('ready') and i.get('name','').startswith('cam/')]))" 2>/dev/null)
[ -z "$READY" ] && exit 0
curl -s -m10 -X POST https://grupocorexia.com.br/api/mediamtx-ready -H "Content-Type: application/json" -d "{\"secret\":\"$SEC\",\"ready\":$READY}" >/dev/null 2>&1
