#!/usr/bin/env bash
LOG=/gravacoes/_backfill.log
echo "=== backfill inicio $(date) ===" >> "$LOG"
tot=0; okc=0
while IFS= read -r f; do
  tot=$((tot+1))
  fx="${f%.mp4}.bf.mp4"
  if ffmpeg -nostdin -y -loglevel error -i "$f" -c copy -movflags +faststart "$fx" 2>/dev/null && [ -s "$fx" ]; then
    touch -r "$f" "$fx"; mv -f "$fx" "$f"; chown corexia:corexia "$f"; okc=$((okc+1))
  else
    rm -f "$fx"; echo "falhou: $f" >> "$LOG"
  fi
done < <(find /gravacoes -path /gravacoes/_staging -prune -o -type f -name '*.mp4' -print)
echo "=== backfill fim $(date) : $okc/$tot remuxados ===" >> "$LOG"
