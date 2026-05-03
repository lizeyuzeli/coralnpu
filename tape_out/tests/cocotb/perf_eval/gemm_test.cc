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

#include <cstdint>

#include "tape_out/sw/opt/generic_kernels/gemm.h"

namespace {

constexpr int kMaxM = 128;
constexpr int kMaxN = 128;
constexpr int kMaxK = 128;

}  // namespace

int32_t m_dim __attribute__((section(".data"))) = 64;
int32_t n_dim __attribute__((section(".data"))) = 64;
int32_t k_dim __attribute__((section(".data"))) = 128;
int32_t b_is_transposed __attribute__((section(".data"))) = 1;

int8_t a_data[kMaxM * kMaxK] __attribute__((section(".extdata"), aligned(16)));
int8_t b_data[kMaxN * kMaxK] __attribute__((section(".extdata"), aligned(16)));
int32_t c_data[kMaxM * kMaxN] __attribute__((section(".extdata"), aligned(16)));

using coralnpu_v2::opt::generic_kernels::GemmParams;

extern "C" {

__attribute__((used, retain)) void run_ref() {
  GemmParams params = {
      .m = m_dim,
      .n = n_dim,
      .k = k_dim,
      .lda = k_dim,
      .ldb = b_is_transposed ? k_dim : n_dim,
      .ldc = n_dim,
      .b_transposed = b_is_transposed != 0,
  };
  coralnpu_v2::opt::generic_kernels::GemmReferenceI8(a_data, b_data, c_data,
                                                      params);
}

__attribute__((used, retain)) void run_opt() {
  GemmParams params = {
      .m = m_dim,
      .n = n_dim,
      .k = k_dim,
      .lda = k_dim,
      .ldb = b_is_transposed ? k_dim : n_dim,
      .ldc = n_dim,
      .b_transposed = b_is_transposed != 0,
  };
  coralnpu_v2::opt::generic_kernels::GemmRvvI8(a_data, b_data, c_data, params);
}

}

void (*impl)() __attribute__((section(".data"))) = run_opt;

int main(void) {
  impl();
  return 0;
}
