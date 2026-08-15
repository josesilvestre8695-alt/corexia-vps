#!/usr/bin/env bash
# "Camera de teste": loopa uma imagem/video como HLS + serve via http.
# Uso:  ./teststream.sh [arquivo_fonte] [porta]   (default: gun.jpg 8888)
SRC="${1:-$HOME/corexia-vision-ai/gun.jpg}"
PORT="${2:-8888}"
D="$HOME/teststream_$PORT"
mkdir -p "$D"
rm -f "$D"/*.ts "$D"/*.m3u8
pkill -f "teststream_$PORT/stream.m3u8" 2>/dev/null
pkill -f "serve.py $PORT" 2>/dev/null
pkill -f "http.server $PORT" 2>/dev/null
sleep 1

case "$SRC" in
  *.jpg|*.jpeg|*.png)
    setsid ffmpeg -loop 1 -re -i "$SRC" -c:v libx264 -preset veryfast -t 86400 \
      -pix_fmt yuv420p -vf scale=1280:720 -r 10 -g 20 \
      -f hls -hls_time 2 -hls_list_size 4 -hls_flags delete_segments "$D/stream.m3u8" \
      >"$D/ffmpeg.log" 2>&1 </dev/null &
    ;;
  *)
    setsid ffmpeg -stream_loop -1 -re -i "$SRC" -c:v libx264 -preset veryfast \
      -pix_fmt yuv420p -g 20 \
      -f hls -hls_time 2 -hls_list_size 4 -hls_flags delete_segments "$D/stream.m3u8" \
      >"$D/ffmpeg.log" 2>&1 </dev/null &
    ;;
esac

setsid python3 "$HOME/corexia-vision-ai/serve.py" "$PORT" "$D" >"$D/httpd.log" 2>&1 </dev/null &
echo "teststream porta $PORT (fonte: $SRC)"
