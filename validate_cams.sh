#!/bin/bash
# valida cameras: resolve embed->m3u8 e ffprobe (codec/res/fps). Uso: validate_cams.sh lista.txt [limite] [paralelo]
REF="https://analitico.grupocorexia.com.br/"
LIST="${1:-cameras.txt}"
LIMIT="${2:-0}"
PAR="${3:-1}"

probe_one() {
  local id="$1" url="$2"
  local m3u8="$url"
  if echo "$url" | grep -q "/camera/embed/"; then
    m3u8=$(curl -s --max-time 10 -e "$REF" "$url" 2>/dev/null | grep -oE 'https?://[A-Za-z0-9._/:?=%&-]+\.m3u8[A-Za-z0-9._/:?=%&-]*' | head -1)
  fi
  if [ -z "$m3u8" ]; then echo "[$id] NAO-RESOLVEU"; return; fi
  local info
  info=$(timeout 15 ffprobe -v error -referer "$REF" -select_streams v:0 -show_entries stream=codec_name,width,height,avg_frame_rate -of csv=p=0 "$m3u8" 2>/dev/null)
  if [ -z "$info" ]; then echo "[$id] OFFLINE"; else echo "[$id] OK $info"; fi
}
export -f probe_one; export REF

n=0
: > /tmp/_cam_jobs
while IFS='|' read -r id url; do
  [ -z "$url" ] && continue
  n=$((n+1))
  [ "$LIMIT" -gt 0 ] && [ "$n" -gt "$LIMIT" ] && break
  echo "$id|$url" >> /tmp/_cam_jobs
done < "$LIST"

cat /tmp/_cam_jobs | xargs -P "$PAR" -I{} bash -c 'IFS="|" read -r id url <<< "{}"; probe_one "$id" "$url"' > /tmp/_cam_results 2>/dev/null
sort /tmp/_cam_results > /tmp/_cam_sorted
echo "===== RESULTADOS ====="
cat /tmp/_cam_sorted
echo "===== RESUMO ====="
echo "OK:          $(grep -c ' OK ' /tmp/_cam_sorted)"
echo "OFFLINE:     $(grep -c 'OFFLINE' /tmp/_cam_sorted)"
echo "NAO-RESOLVEU:$(grep -c 'NAO-RESOLVEU' /tmp/_cam_sorted)"
echo "-- resolucoes vistas --"
grep ' OK ' /tmp/_cam_sorted | grep -oE '[0-9]+,[0-9]+,[0-9]+/[0-9]+' | sort | uniq -c
