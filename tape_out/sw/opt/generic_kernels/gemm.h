// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#ifndef SW_OPT_GENERIC_KERNELS_GEMM_H_
#define SW_OPT_GENERIC_KERNELS_GEMM_H_

#include <cstdint>

namespace coralnpu_v2::opt::generic_kernels {

// int8 GEMM: C[M, N] = A[M, K] * B[K, N]
//
// Layout conventions:
// - A is row-major with row stride `lda` (in elements).
// - C is row-major with row stride `ldc` (in elements).
// - If `b_transposed == false`, B is row-major [K, N], row stride `ldb`.
// - If `b_transposed == true`, B is row-major [N, K] (i.e. pre-transposed),
//   row stride `ldb` (typically K).
struct GemmParams {
  int m;
  int n;
  int k;
  int lda;
  int ldb;
  int ldc;
  bool b_transposed;
};

void GemmRvvI8(const int8_t* a, const int8_t* b, int32_t* c,
               const GemmParams& params);

void GemmReferenceI8(const int8_t* a, const int8_t* b, int32_t* c,
                     const GemmParams& params);

}  // namespace coralnpu_v2::opt::generic_kernels

#endif  // SW_OPT_GENERIC_KERNELS_GEMM_H_
