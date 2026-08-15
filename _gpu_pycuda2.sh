#!/bin/bash
cd "$(dirname "$0")" || exit 1
set -x
export MAMBA_ROOT_PREFIX="$PWD/mm"

# headers que faltaram (cudaProfiler.h, driver API)
./bin/micromamba install -y -p "$PWD/mm_cuda" -c nvidia -c conda-forge \
   cuda-profiler-api=12.4 cuda-driver-dev=12.4 cuda-nvrtc-dev=12.4 || exit 3

echo "=== cudaProfiler.h em: ==="
find "$PWD/mm_cuda" -name "cudaProfiler.h" 2>/dev/null | head

SP="$PWD/venv/lib/python3.10/site-packages/nvidia"
export CUDA_HOME="$PWD/mm_cuda" CUDA_ROOT="$PWD/mm_cuda"
export PATH="$PWD/mm_cuda/bin:$PATH"
# inclui TANTO include quanto targets/.../include (conda espalha os headers)
export CPATH="$PWD/mm_cuda/include:$PWD/mm_cuda/targets/x86_64-linux/include"
export CUDA_INC_DIR="$PWD/mm_cuda/include"
export LD_LIBRARY_PATH="$PWD/mm_cuda/lib:$(echo $SP/*/lib | tr ' ' ':'):/usr/lib/x86_64-linux-gnu"
export LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:$PWD/mm_cuda/lib:$PWD/mm_cuda/lib/stubs:$PWD/mm_cuda/targets/x86_64-linux/lib"

./venv/bin/pip install --no-build-isolation --no-cache-dir --force-reinstall pycuda 2>&1 | tail -16 || exit 5
./venv/bin/python -c "import pycuda.driver as d; d.init(); print('PYCUDA_OK GPUs=%d' % d.Device.count())" || exit 6
echo "=== TUDO OK ==="
