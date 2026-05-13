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

// Diagnostic for the LVDS extdata data path on Core_Axi_Chip.
//
// `src_buf` lives in `.extdata` (EXTMEM, reached via the master EBus path
// through LvdsAdapterChip / LvdsAdapterFpga); the dest buffers live in
// `.data` (DTCM, no LVDS). The python driver writes a known byte pattern
// to `src_buf` via the slave path, then runs three kernels and compares
// each kernel's DTCM output against that pattern:
//   - run_scalar_read: byte-by-byte loads (single-beat AXI reads).
//   - run_vector_read: 16-byte RVV vector loads (single-beat too, but
//                      goes through the vector LSU instead of scalar).
//   - run_scratch_round_trip: vector load src_buf, vector store into
//                      another extdata buffer, vector load it back, store
//                      to DTCM. Stresses the master EBus write+read
//                      round trip.

#include <cstddef>
#include <cstdint>

#include <riscv_vector.h>

namespace {
constexpr size_t kBufSize = 256;
}  // namespace

// Source buffer in EXTMEM (LVDS-bridged).
int8_t src_buf[kBufSize] __attribute__((section(".extdata"), aligned(16)));

// Destinations in DTCM (no LVDS).
int8_t dst_scalar[kBufSize] __attribute__((section(".data"), aligned(16)));
int8_t dst_vector[kBufSize] __attribute__((section(".data"), aligned(16)));
int8_t dst_scratch[kBufSize] __attribute__((section(".data"), aligned(16)));

// Round-trip scratch in EXTMEM.
int8_t scratch_buf[kBufSize] __attribute__((section(".extdata"), aligned(16)));

extern "C" {

__attribute__((used, retain)) void run_scalar_read() {
  for (size_t i = 0; i < kBufSize; ++i) {
    dst_scalar[i] = src_buf[i];
  }
}

__attribute__((used, retain)) void run_vector_read() {
  for (size_t i = 0; i < kBufSize; i += 16) {
    vint8m1_t v = __riscv_vle8_v_i8m1(src_buf + i, 16);
    __riscv_vse8_v_i8m1(dst_vector + i, v, 16);
  }
}

__attribute__((used, retain)) void run_scratch_round_trip() {
  // Stage 1: vector copy src_buf -> scratch_buf (extdata->extdata via LVDS).
  for (size_t i = 0; i < kBufSize; i += 16) {
    vint8m1_t v = __riscv_vle8_v_i8m1(src_buf + i, 16);
    __riscv_vse8_v_i8m1(scratch_buf + i, v, 16);
  }
  // Stage 2: vector copy scratch_buf -> dst_scratch (extdata->dtcm).
  for (size_t i = 0; i < kBufSize; i += 16) {
    vint8m1_t v = __riscv_vle8_v_i8m1(scratch_buf + i, 16);
    __riscv_vse8_v_i8m1(dst_scratch + i, v, 16);
  }
}

}  // extern "C"

void (*impl)() __attribute__((section(".data"))) = run_scalar_read;

int main(void) {
  impl();
  return 0;
}
