#!/usr/bin/env bash
cd "$(dirname "$0")"
CUDA_VISIBLE_DEVICES=0 ./venv/bin/python detector.py 0 &
CUDA_VISIBLE_DEVICES=1 ./venv/bin/python detector.py 1 &
wait