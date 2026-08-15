@echo off
REM Windows: rode este .bat e o run_gpu1.bat em janelas separadas
set CUDA_VISIBLE_DEVICES=0
python detector.py 0
