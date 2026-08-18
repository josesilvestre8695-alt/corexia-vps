#!/usr/bin/env bash
SEC=$(cat /opt/corexia/webhook_secret 2>/dev/null)
[ -z "$SEC" ] && { echo "$(date -Iseconds) sem secret"; exit 1; }
TMP=/opt/corexia/cam_map.json.tmp
if curl -s -m20 -X POST https://grupocorexia.com.br/api/gravacoes/cam-map -H "Content-Type: application/json" -d "{\"secret\":\"$SEC\"}" -o "$TMP"; then
  if python3 -c "import json,sys;d=json.load(open('$TMP'));sys.exit(0 if isinstance(d,dict) and len(d)>0 else 1)" 2>/dev/null; then
    mv -f "$TMP" /opt/corexia/cam_map.json
    echo "$(date -Iseconds) cam_map ok: $(python3 -c "import json;print(len(json.load(open('/opt/corexia/cam_map.json'))))") entradas"
  else echo "$(date -Iseconds) resposta invalida - mantem mapa antigo"; rm -f "$TMP"; fi
else echo "$(date -Iseconds) curl falhou - mantem mapa antigo"; rm -f "$TMP"; fi
