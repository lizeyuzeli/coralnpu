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

#include "tape_out/sw/opt/generic_kernels/gemm.h"

#include <riscv_vector.h>

#include <cstddef>
#include <cstdint>

namespace coralnpu_v2::opt::generic_kernels {

namespace {

inline int32_t ReduceSum(vint32m4_t v, size_t vl) {
  return __riscv_vmv_x_s_i32m1_i32(
      __riscv_vredsum_vs_i32m4_i32m1(v, __riscv_vmv_v_x_i32m1(0, 1), vl));
}

}  // namespace

void GemmRvvI8(const int8_t* a, const int8_t* b, int32_t* c,
               const GemmParams& params) {
  const int m = params.m;
  const int n = params.n;
  const int k = params.k;
  const int lda = params.lda;
  const int ldb = params.ldb;
  const int ldc = params.ldc;
  const bool b_transposed = params.b_transposed;

  constexpr int kColBlock = 4;

  for (int i = 0; i < m; ++i) {
    const int8_t* a_row = a + i * lda;
    int32_t* c_row = c + i * ldc;

    for (int j = 0; j < n; j += kColBlock) {
      const int cols = (j + kColBlock <= n) ? kColBlock : (n - j);
      int32_t acc0 = 0;
      int32_t acc1 = 0;
      int32_t acc2 = 0;
      int32_t acc3 = 0;

      for (int kk = 0; kk < k;) {
        const size_t vl = __riscv_vsetvl_e8m1(k - kk);
        const vint8m1_t a_vec = __riscv_vle8_v_i8m1(a_row + kk, vl);
        const vint16m2_t a_vec_ext = __riscv_vsext_vf2_i16m2(a_vec, vl);

        if (cols > 0) {
          const int8_t* b0 = b_transposed ? (b + (j + 0) * ldb + kk)
                                          : (b + kk * ldb + (j + 0));
          const vint8m1_t b_vec0 = b_transposed
                                       ? __riscv_vle8_v_i8m1(b0, vl)
                                       : __riscv_vlse8_v_i8m1(b0, ldb, vl);
          const vint16m2_t b_vec0_ext = __riscv_vsext_vf2_i16m2(b_vec0, vl);
          const vint32m4_t prod0 = __riscv_vwmul_vv_i32m4(a_vec_ext, b_vec0_ext, vl);
          acc0 += ReduceSum(prod0, vl);
        }
        if (cols > 1) {
          const int8_t* b1 = b_transposed ? (b + (j + 1) * ldb + kk)
                                          : (b + kk * ldb + (j + 1));
          const vint8m1_t b_vec1 = b_transposed
                                       ? __riscv_vle8_v_i8m1(b1, vl)
                                       : __riscv_vlse8_v_i8m1(b1, ldb, vl);
          const vint16m2_t b_vec1_ext = __riscv_vsext_vf2_i16m2(b_vec1, vl);
          const vint32m4_t prod1 = __riscv_vwmul_vv_i32m4(a_vec_ext, b_vec1_ext, vl);
          acc1 += ReduceSum(prod1, vl);
        }
        if (cols > 2) {
          const int8_t* b2 = b_transposed ? (b + (j + 2) * ldb + kk)
                                          : (b + kk * ldb + (j + 2));
          const vint8m1_t b_vec2 = b_transposed
                                       ? __riscv_vle8_v_i8m1(b2, vl)
                                       : __riscv_vlse8_v_i8m1(b2, ldb, vl);
          const vint16m2_t b_vec2_ext = __riscv_vsext_vf2_i16m2(b_vec2, vl);
          const vint32m4_t prod2 = __riscv_vwmul_vv_i32m4(a_vec_ext, b_vec2_ext, vl);
          acc2 += ReduceSum(prod2, vl);
        }
        if (cols > 3) {
          const int8_t* b3 = b_transposed ? (b + (j + 3) * ldb + kk)
                                          : (b + kk * ldb + (j + 3));
          const vint8m1_t b_vec3 = b_transposed
                                       ? __riscv_vle8_v_i8m1(b3, vl)
                                       : __riscv_vlse8_v_i8m1(b3, ldb, vl);
          const vint16m2_t b_vec3_ext = __riscv_vsext_vf2_i16m2(b_vec3, vl);
          const vint32m4_t prod3 = __riscv_vwmul_vv_i32m4(a_vec_ext, b_vec3_ext, vl);
          acc3 += ReduceSum(prod3, vl);
        }

        kk += static_cast<int>(vl);
      }

      if (cols > 0) c_row[j + 0] = acc0;
      if (cols > 1) c_row[j + 1] = acc1;
      if (cols > 2) c_row[j + 2] = acc2;
      if (cols > 3) c_row[j + 3] = acc3;
    }
  }
}

void GemmReferenceI8(const int8_t* a, const int8_t* b, int32_t* c,
                     const GemmParams& params) {
  const int m = params.m;
  const int n = params.n;
  const int k = params.k;
  const int lda = params.lda;
  const int ldb = params.ldb;
  const int ldc = params.ldc;
  const bool b_transposed = params.b_transposed;

  for (int i = 0; i < m; ++i) {
    for (int j = 0; j < n; ++j) {
      int32_t acc = 0;
      for (int kk = 0; kk < k; ++kk) {
        const int32_t av = static_cast<int32_t>(a[i * lda + kk]);
        const int32_t bv = b_transposed
                               ? static_cast<int32_t>(b[j * ldb + kk])
                               : static_cast<int32_t>(b[kk * ldb + j]);
        acc += av * bv;
      }
      c[i * ldc + j] = acc;
    }
  }
}

}  // namespace coralnpu_v2::opt::generic_kernels
