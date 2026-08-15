#!/bin/bash
# resolve embed->m3u8 e acha ate N cameras VIVAS agora, imprimindo id|m3u8|WxH
REF="https://analitico.grupocorexia.com.br/"
N="${2:-3}"
found=0
while IFS='|' read -r id url; do
  [ -z "$url" ] && continue
  m3u8="$url"
  if echo "$url" | grep -q "/camera/embed/"; then
    m3u8=$(curl -s --max-time 10 -e "$REF" "$url" 2>/dev/null | grep -oE 'https?://[A-Za-z0-9._/:?=%&-]+\.m3u8[A-Za-z0-9._/:?=%&-]*' | head -1)
  fi
  [ -z "$m3u8" ] && continue
  wh=$(timeout 12 ffprobe -v error -referer "$REF" -select_streams v:0 -show_entries stream=width,height -of csv=p=0:s=x "$m3u8" 2>/dev/null)
  if [ -n "$wh" ]; then
    echo "$id|$m3u8|$wh"
    found=$((found+1))
    [ "$found" -ge "$N" ] && break
  fi
done < "${1:-cameras.txt}"
