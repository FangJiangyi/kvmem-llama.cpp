#pragma once

// llama.cpp's HIP backend compiles the CUDA-shaped kernel sources with hipcc
// and supplies CUDA-to-HIP aliases in this internal compatibility header.
// Keep all adapter runtime includes here so the C++ control path and the
// stage-in kernels select the same runtime as ggml.
#if defined(GGML_USE_HIP)
#include "vendors/hip.h"

using cudaPointerAttributes = hipPointerAttribute_t;
#define cudaPointerGetAttributes hipPointerGetAttributes
#else
#include <cuda_runtime.h>
#endif
