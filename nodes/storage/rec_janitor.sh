#!/usr/bin/env bash
# Organiza segmentos orfaos (staging parado >120s). rec_move.py remuxa fmp4->mp4 padrao.
NOW=$(date +%s)
for d in /gravacoes/_staging/cam/*/; do
  [ -d "$d" ] || continue; k=$(basename "$d")
  for f in "$d"*.mp4; do
    [ -f "$f" ] || continue
    if [ $(( NOW - $(stat -c %Y "$f") )) -gt 120 ]; then
      /usr/bin/python3 /opt/corexia/rec_move.py "$f" "cam/$k" >> /gravacoes/_organize.log 2>&1
    fi
  done
done
