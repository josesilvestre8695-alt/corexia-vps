#!/bin/bash
# Instala nvcc (via micromamba, sem sudo) e compila pycuda -> destrava GPU pra roboflow inference.
cd "$(dirname "$0")" || exit 1
set -x

# 1) micromamba (binario unico, sem root)
if [ ! -x ./bin/micromamba ]; then
  curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj bin/micromamba || exit 2
fi

# 2) cuda-nvcc 12.4 + headers (cudart-dev) do canal nvidia
export MAMBA_ROOT_PREFIX="$PWD/mm"
./bin/micromamba create -y -p "$PWD/mm_cuda" -c nvidia -c conda-forge \
   cuda-nvcc=12.4 cuda-cudart-dev=12.4 cuda-nvrtc-dev=12.4 || exit 3

NVCC="$PWD/mm_cuda/bin/nvcc"
[ -x "$NVCC" ] || { echo "NVCC AUSENTE"; exit 4; }
"$NVCC" --version | tail -2

# 3) CUDA_HOME = mm_cuda (nvcc+headers) ; libs de runtime do pip + driver libcuda
SP="$PWD/venv/lib/python3.10/site-packages/nvidia"
export CUDA_HOME="$PWD/mm_cuda" CUDA_ROOT="$PWD/mm_cuda"
export PATH="$PWD/mm_cuda/bin:$PATH"
export CUDA_INC_DIR="$PWD/mm_cuda/include"
export LD_LIBRARY_PATH="$PWD/mm_cuda/lib:$(echo $SP/*/lib | tr ' ' ':'):/usr/lib/x86_64-linux-gnu"
export LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:$PWD/mm_cuda/lib:$PWD/mm_cuda/lib/stubs"

# 4) compila pycuda contra esse toolkit
./venv/bin/pip install --no-build-isolation --no-cache-dir pycuda 2>&1 | tail -14 || exit 5

# 5) testa o import + driver
./venv/bin/python -c "import pycuda.driver as d; d.init(); print('PYCUDA_OK GPUs=%d' % d.Device.count())" || exit 6
echo "=== TUDO OK ==="
