#!/bin/bash
# Monta um CUDA_HOME a partir das libs pip (sem sudo/apt) e compila o pycuda contra ele.
set -e
cd "$(dirname "$0")"
SP="$PWD/venv/lib/python3.10/site-packages/nvidia"
CH="$PWD/cuda_home"

echo "=== g++/gcc presentes? ==="
which g++ gcc || { echo "SEM g++ - nvcc nao compila"; exit 1; }

rm -rf "$CH"; mkdir -p "$CH/bin" "$CH/lib64" "$CH/include"

# nvcc + nvvm (nvcc procura ../nvvm relativo a si)
ln -sf "$SP"/cuda_nvcc/bin/* "$CH/bin/" 2>/dev/null || true
ln -sfn "$SP/cuda_nvcc/nvvm" "$CH/nvvm" 2>/dev/null || true

# headers CUDA 12 (cuda.h, cuda_runtime.h, crt, cccl...)
for d in cuda_nvcc cuda_runtime cuda_cccl cccl cublas cudnn; do
  [ -d "$SP/$d/include" ] && cp -rsfn "$SP/$d/include/." "$CH/include/" 2>/dev/null || true
done

# libs CUDA 12 + driver stub (libcuda)
for d in cuda_runtime cublas cudnn cufft curand cuda_nvrtc nvjitlink; do
  [ -d "$SP/$d/lib" ] && ln -sf "$SP/$d/lib/"* "$CH/lib64/" 2>/dev/null || true
done
ln -sf /usr/lib/x86_64-linux-gnu/libcuda.so   "$CH/lib64/" 2>/dev/null || true
ln -sf /usr/lib/x86_64-linux-gnu/libcuda.so.1 "$CH/lib64/" 2>/dev/null || true

echo "=== nvcc funciona? ==="
export PATH="$CH/bin:$PATH"
export CUDA_ROOT="$CH" CUDA_HOME="$CH" CUDA_INC_DIR="$CH/include" CUDA_LIB_DIR="$CH/lib64"
export LD_LIBRARY_PATH="$CH/lib64:$LD_LIBRARY_PATH"
"$CH/bin/nvcc" --version | tail -2 || { echo "nvcc NAO RODA"; exit 1; }

echo "=== compilando pycuda (pode demorar) ==="
./venv/bin/pip install --no-build-isolation pycuda 2>&1 | tail -12
