#!/usr/bin/env bash
# Configure + build KVMem with llama.cpp's HIP/ROCm backend.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CMAKE="${CMAKE:-$ROOT/.venv/bin/cmake}"
if [[ ! -x "$CMAKE" ]]; then
    CMAKE="$(command -v cmake)"
fi

BUILD="${BUILD_DIR:-$ROOT/build-rocm}"
TYPE="${CMAKE_BUILD_TYPE:-Release}"
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
GPU_TARGETS="${AMDGPU_TARGETS:-${CMAKE_HIP_ARCHITECTURES:-gfx1100}}"

"$CMAKE" -S "$ROOT" -B "$BUILD" \
    -DCMAKE_BUILD_TYPE="$TYPE" \
    -DROCM_PATH="$ROCM_PATH" \
    -DCMAKE_HIP_COMPILER="${CMAKE_HIP_COMPILER:-$ROCM_PATH/llvm/bin/clang++}" \
    -DCMAKE_HIP_ARCHITECTURES="$GPU_TARGETS" \
    -DKVMEM_ROCM=ON \
    -DGGML_HIP=ON \
    -DGGML_CUDA=OFF \
    -DGGML_CUDA_FA_ALL_QUANTS=ON \
    -DKVMEM_BUILD_LLAMA=ON \
    -DLLAMA_KVMEM=ON \
    -DLLAMA_KVMEM_ROOT="$ROOT"

"$CMAKE" --build "$BUILD" -j"${NPROC:-$(nproc)}"
echo "binaries under $BUILD/bin"
