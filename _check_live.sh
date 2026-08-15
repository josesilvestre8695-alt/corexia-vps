#!/usr/bin/env bash
cd "$(dirname "$0")"
CID=$(ls gravacoes_live 2>/dev/null | head -1)
echo "camera restreamada: $CID"
curl -s -o /dev/null -w "/live/index.m3u8 -> %{http_code}\n" "http://localhost:8000/live/$CID/index.m3u8"
echo "segmentos:"; ls gravacoes_live/"$CID"/ 2>/dev/null | head -5
