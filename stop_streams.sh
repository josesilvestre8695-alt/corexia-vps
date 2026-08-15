#!/usr/bin/env bash
# para os streams de teste (ffmpeg + servidor http). Rodar via: bash stop_streams.sh
pkill -f teststream_ 2>/dev/null
pkill -f 'serve.py 88' 2>/dev/null
pkill -f 'hls .* stream.m3u8' 2>/dev/null
pkill -f 'i .*teststream' 2>/dev/null
exit 0
