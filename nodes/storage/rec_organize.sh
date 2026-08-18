#!/usr/bin/env bash
# Hook do MediaMTX (runOnRecordSegmentComplete). Env: MTX_PATH (cam/<key>), MTX_SEGMENT_PATH (arquivo).
/usr/bin/python3 /opt/corexia/rec_move.py "$MTX_SEGMENT_PATH" "$MTX_PATH" >> /gravacoes/_organize.log 2>&1
