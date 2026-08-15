#!/bin/bash
cd "$(dirname "$0")" || exit 1
set -x
export MAMBA_ROOT_PREFIX="$PWD/mm"

# TOOLKIT COMPLETO (todos os headers dev de uma vez — acaba o whack-a-mole)
./bin/micromamba install -y -p "$PWD/mm_cuda" -c nvidia -c conda-forge cuda-toolkit=12.4 || exit 3

SP="$PWD/venv/lib/python3.10/site-packages/nvidia"
export CUDA_HOME="$PWD/mm_cuda" CUDA_ROOT="$PWD/mm_cuda"
export PATH="$PWD/mm_cuda/bin:$PATH"
export CPATH="$PWD/mm_cuda/include:$PWD/mm_cuda/targets/x86_64-linux/include"
export CUDA_INC_DIR="$PWD/mm_cuda/include"
export LD_LIBRARY_PATH="$PWD/mm_cuda/lib:$PWD/mm_cuda/targets/x86_64-linux/lib:$(echo $SP/*/lib | tr ' ' ':'):/usr/lib/x86_64-linux-gnu"
export LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:$PWD/mm_cuda/lib:$PWD/mm_cuda/lib/stubs:$PWD/mm_cuda/targets/x86_64-linux/lib:$PWD/mm_cuda/targets/x86_64-linux/lib/stubs"

echo "=== headers-chave presentes? ==="
for h in cuda.h cudaProfiler.h curand.h cuda_runtime.h; do
  f=$(find "$PWD/mm_cuda" -name "$h" 2>/dev/null | head -1); echo "$h -> ${f:-FALTA}"
done

./venv/bin/pip install --no-build-isolation --no-cache-dir --force-reinstall pycuda 2>&1 | tail -16 || exit 5
./venv/bin/python -c "import pycuda.driver as d; d.init(); print('PYCUDA_OK GPUs=%d' % d.Device.count())" || exit 6
echo "=== TUDO OK ==="
