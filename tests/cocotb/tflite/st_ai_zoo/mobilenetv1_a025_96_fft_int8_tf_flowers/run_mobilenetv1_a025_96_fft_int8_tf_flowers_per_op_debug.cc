// Copyright 2026 Li Zeyu <lizeyuzeli000lzy@gmail.com>
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// End-to-end TFLite-Micro inference test for the ST-AI-Zoo
// "MobileNet-V1 alpha=0.25 96x96 INT8" tf_flowers (5-class) classifier.
//
// Operator inventory (32 ops, 7 distinct kinds):
//   QUANTIZE x1, CONV_2D x14, DEPTHWISE_CONV_2D x13, MEAN x1,
//   FULLY_CONNECTED x1, SOFTMAX x1, DEQUANTIZE x1.
//
// Boundary dtypes:
//   Input  : (1, 96, 96, 3) uint8   (RGB pixels; the model's QUANTIZE op
//                                    converts uint8 -> int8 internally).
//   Output : (1, 5)         float32 (per-class probabilities; the model's
//                                    DEQUANTIZE op converts int8 -> float32).
//
// All compute-heavy ops (CONV_2D, DEPTHWISE_CONV_2D, FULLY_CONNECTED) are
// int8->int8 internally and dispatch to the optimized RVV kernels.

#include <stdint.h>
#include <stdio.h>

#include <cstring>

#include "sw/opt/litert-micro/conv.h"
#include "sw/opt/litert-micro/depthwise_conv.h"
#include "sw/opt/litert-micro/fully_connected.h"
#include "tensorflow/lite/core/c/common.h"
#include "tensorflow/lite/micro/kernels/kernel_util.h"
#include "tensorflow/lite/micro/kernels/micro_ops.h"
#include "tensorflow/lite/micro/micro_common.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/system_setup.h"
#include "tests/cocotb/tflite/st_ai_zoo/mobilenetv1_a025_96_fft_int8_tf_flowers/mobilenetv1_a025_96_fft_int8.h"

// Constants shared between the per-op stats wrappers and the global
// definitions.
constexpr int kFirstConvElems = 1 * 48 * 48 * 8;   // shape (1,48,48,8) int8

// Per-op output stats: we wrap CONV_2D / DEPTHWISE_CONV_2D / FULLY_CONNECTED
// invokes and after each successful inner.invoke() compute a small fingerprint
// of the produced output tensor. Cocotb dumps the table so we can pinpoint
// which exact layer's output goes "all zero" / saturated / unusable.
// Layout: kStatsPerOp uint32s per op record:
//   [0] op_kind (low byte) | output_type (next byte) | dims_size (next byte)
//   [1] num_elems
//   [2] int8 min (signed-extended into int32, cast to uint32 bit pattern)
//   [3] int8 max (signed-extended into int32, cast to uint32 bit pattern)
//   [4] int32 abs_mean * 1000  (over raw int8 values, NO zero-point subtract)
//   [5] int32 count of exact zeros
constexpr int kMaxOps = 64;
constexpr int kStatsPerOp = 6;
constexpr int kOpStatsWords = kMaxOps * kStatsPerOp;

// Forward declarations of the C-linkage debug globals referenced from the
// per-op stats wrappers below; the defining declarations live further down in
// the `extern "C"` block. We MUST keep the wrapper functions out of the
// anonymous namespace path of these symbols -- otherwise `extern "C"` inside
// `namespace { ... }` still gives them internal linkage and cocotb's ELF
// symbol lookup returns None.
extern "C" {
extern int8_t inference_status;
extern char inference_status_message[31];
extern uint32_t inference_diag[16];
extern int8_t first_conv_output[kFirstConvElems];
extern uint32_t op_stats[kOpStatsWords];
extern uint32_t op_stats_count;
}

namespace {
using MobileNetV1FftOpResolver = tflite::MicroMutableOpResolver<7>;
using coralnpu_v2::opt::litert_micro::Register_CONV_2D;
using coralnpu_v2::opt::litert_micro::Register_DEPTHWISE_CONV_2D;
using coralnpu_v2::opt::litert_micro::Register_FULLY_CONNECTED;

// === Per-op stats wrappers (DEBUG) ==========================================
// We wrap CONV_2D / DEPTHWISE_CONV_2D / FULLY_CONNECTED so that after each
// successful inner-invoke we record a small fingerprint of the produced int8
// output tensor (min / max / abs_mean*1000 / zero_count) into `op_stats[]`.
// In addition, the very first CONV_2D invocation also memcpy's its full int8
// output into `first_conv_output[]` so cocotb can keep cross-checking it
// against the host TFLite golden.
//
// Op kinds (low byte of op_stats[*][0]):
//   1 = CONV_2D       output
//   2 = DEPTHWISE_CONV_2D output
//   4 = FULLY_CONNECTED output
//   5 = FULLY_CONNECTED INPUT  (== upstream MEAN output, our cheap proxy)
constexpr uint8_t kOpKindConv = 1;
constexpr uint8_t kOpKindDw = 2;
constexpr uint8_t kOpKindFc = 4;
constexpr uint8_t kOpKindFcIn = 5;

static bool g_first_conv_done = false;

static void RecordTensorStats(const TfLiteEvalTensor* tensor,
                              uint8_t op_kind) {
  if (op_stats_count >= static_cast<uint32_t>(kMaxOps)) return;
  if (tensor == nullptr || tensor->data.data == nullptr) return;

  size_t n = 1;
  for (int i = 0; i < tensor->dims->size; ++i) {
    n *= static_cast<size_t>(tensor->dims->data[i]);
  }

  uint32_t* slot = &op_stats[op_stats_count * kStatsPerOp];
  slot[0] = static_cast<uint32_t>(op_kind) |
            (static_cast<uint32_t>(tensor->type) << 8) |
            (static_cast<uint32_t>(tensor->dims->size) << 16);
  slot[1] = static_cast<uint32_t>(n);

  if (tensor->type == kTfLiteInt8 && n > 0) {
    const int8_t* p = static_cast<const int8_t*>(tensor->data.data);
    int lo = 127, hi = -128;
    int64_t sum_abs = 0;
    int32_t zeros = 0;
    for (size_t i = 0; i < n; ++i) {
      int v = static_cast<int>(p[i]);
      if (v < lo) lo = v;
      if (v > hi) hi = v;
      sum_abs += (v < 0) ? -v : v;
      if (v == 0) ++zeros;
    }
    slot[2] = static_cast<uint32_t>(static_cast<int32_t>(lo));
    slot[3] = static_cast<uint32_t>(static_cast<int32_t>(hi));
    slot[4] = static_cast<uint32_t>(
        static_cast<int32_t>((sum_abs * 1000) / static_cast<int64_t>(n)));
    slot[5] = static_cast<uint32_t>(zeros);
  } else {
    slot[2] = slot[3] = slot[4] = slot[5] = 0;
  }

  ++op_stats_count;
}

// Per-op-kind wrapper template. Distinct instantiation per `Kind` gives each
// wrapper its own static `upstream` storage for the captured inner
// registration -- otherwise a single global pointer could only host one op.
template <uint8_t Kind>
struct OpWrap {
  static TFLMRegistration upstream;
  static TfLiteStatus Invoke(TfLiteContext* context, TfLiteNode* node) {
    TfLiteStatus s = upstream.invoke(context, node);
    if (s != kTfLiteOk) return s;
    // Conv2D special-case: also memcpy first invocation's output for the
    // golden-comparison sanity check we proved out earlier.
    if (Kind == kOpKindConv && !g_first_conv_done) {
      g_first_conv_done = true;
      TfLiteEvalTensor* out = tflite::micro::GetEvalOutput(context, node, 0);
      if (out != nullptr && out->data.data != nullptr) {
        size_t out_elems = 1;
        for (int i = 0; i < out->dims->size; ++i) {
          out_elems *= static_cast<size_t>(out->dims->data[i]);
        }
        size_t bytes = out_elems;
        if (bytes > static_cast<size_t>(kFirstConvElems)) {
          bytes = static_cast<size_t>(kFirstConvElems);
        }
        std::memcpy(first_conv_output, out->data.data, bytes);
        inference_diag[14] = static_cast<uint32_t>(bytes);
        inference_diag[15] = static_cast<uint32_t>(out_elems);
      }
    }
    // For FC we ALSO record stats on its input tensor (== upstream MEAN
    // output) so we can tell whether MEAN already collapsed to a constant.
    if (Kind == kOpKindFc) {
      const TfLiteEvalTensor* fc_in =
          tflite::micro::GetEvalInput(context, node, 0);
      RecordTensorStats(fc_in, kOpKindFcIn);
    }
    TfLiteEvalTensor* out_t =
        tflite::micro::GetEvalOutput(context, node, 0);
    RecordTensorStats(out_t, Kind);
    return kTfLiteOk;
  }
  static TFLMRegistration Make(TFLMRegistration inner) {
    upstream = inner;
    TFLMRegistration r = inner;
    r.invoke = &Invoke;
    return r;
  }
};
template <uint8_t Kind> TFLMRegistration OpWrap<Kind>::upstream = {};
// === End per-op stats wrappers ==============================================

TfLiteStatus RegisterOps(MobileNetV1FftOpResolver& op_resolver) {
  // DEBUG: route CONV_2D / DEPTHWISE_CONV_2D / FULLY_CONNECTED through the
  // per-op stats wrappers. Each wrapper still uses the RVV optimized inner
  // implementation -- it just additionally records a fingerprint of the
  // output tensor into `op_stats[]` so cocotb can pinpoint which exact layer
  // produces unusable activations.
  TF_LITE_ENSURE_STATUS(
      op_resolver.AddConv2D(OpWrap<kOpKindConv>::Make(Register_CONV_2D())));
  TF_LITE_ENSURE_STATUS(op_resolver.AddDepthwiseConv2D(
      OpWrap<kOpKindDw>::Make(Register_DEPTHWISE_CONV_2D())));
  TF_LITE_ENSURE_STATUS(op_resolver.AddFullyConnected(
      OpWrap<kOpKindFc>::Make(Register_FULLY_CONNECTED())));

  // Boundary quantize / dequantize wrappers.
  TF_LITE_ENSURE_STATUS(op_resolver.AddQuantize());
  TF_LITE_ENSURE_STATUS(op_resolver.AddDequantize());

  // Reduction + classification head. MicroMutableOpResolver::AddMean() is
  // hard-coded to the upstream Register_MEAN() and there's no overload taking
  // a custom registration (AddBuiltin is private). Instead, the FC wrapper
  // records stats on its INPUT tensor as well, which IS the MEAN output --
  // same diagnostic information without needing to subclass the resolver.
  TF_LITE_ENSURE_STATUS(op_resolver.AddMean());
  TF_LITE_ENSURE_STATUS(op_resolver.AddSoftmax());

  return kTfLiteOk;
}

constexpr int kInputElems = 1 * 96 * 96 * 3;       // uint8 RGB image
constexpr int kInputBytes = kInputElems * 1;
constexpr int kOutputElems = 1 * 5;                // float32 probabilities
constexpr int kOutputBytes = kOutputElems * 4;
}  // namespace

extern "C" {
int8_t inference_status = -1;
char inference_status_message[31]
    __attribute__((section(".data"), aligned(16)));

uint8_t inference_input[kInputBytes]
    __attribute__((section(".data"), aligned(16)));
float inference_output[kOutputElems]
    __attribute__((section(".data"), aligned(16)));

// Lightweight diagnostic block. Lets cocotb introspect what tflite-micro
// reports back even when status==0 ("Invoke successful").
//   [0]  inputs_size           (uint32)
//   [1]  outputs_size          (uint32)
//   [2]  in0_type              (uint32, TfLiteType)
//   [3]  in0_bytes             (uint32)
//   [4]  in0_zero_point        (int32)
//   [5]  in0_scale             (float -> reinterpret as uint32)
//   [6]  out0_type             (uint32, TfLiteType)
//   [7]  out0_bytes            (uint32)
//   [8]  out0_zero_point       (int32)
//   [9]  out0_scale            (float -> reinterpret as uint32)
//   [10] first 4 raw bytes of out0 buffer (after Invoke)
//   [11] last  4 raw bytes of out0 buffer (after Invoke)
//   [12] first 4 raw bytes of in0  buffer (after staging)
//   [13] arena_used_bytes      (uint32)
//   [14] reserved
//   [15] reserved
uint32_t inference_diag[16]
    __attribute__((section(".data"), aligned(16)));

// First-conv output dump (still captured every run as a golden cross-check).
int8_t first_conv_output[kFirstConvElems]
    __attribute__((section(".data"), aligned(16)));

// Per-op output stats table. Filled in by the OpWrap<>::Invoke wrappers.
uint32_t op_stats[kOpStatsWords]
    __attribute__((section(".data"), aligned(16)));
uint32_t op_stats_count
    __attribute__((section(".data"), aligned(16))) = 0;

// Largest activation tensor in this 96x96 alpha=0.25 variant is
// (1,48,48,8) = 18 KB; with double-buffering + scratch the arena stays
// well under 256 KB. 384 KB is comfortable headroom.
constexpr size_t kTensorArenaSize = 512 * 1024;
uint8_t tensor_arena[kTensorArenaSize]
    __attribute__((section(".data"), aligned(16)));
}

int main(int argc, char** argv) {
  std::strncpy(inference_status_message, "Started", 31);

  const tflite::Model* model =
      tflite::GetModel(g_mobilenetv1_a025_96_fft_int8_model_data);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    std::strncpy(inference_status_message, "Bad model schema version", 31);
    return -1;
  }

  MobileNetV1FftOpResolver op_resolver;
  if (RegisterOps(op_resolver) != kTfLiteOk) {
    std::strncpy(inference_status_message, "Error registering ops", 31);
    return -1;
  }
  std::strncpy(inference_status_message, "Halted after op resolver", 31);

  tflite::MicroInterpreter interpreter(model, op_resolver, tensor_arena,
                                       kTensorArenaSize);
  std::strncpy(inference_status_message, "Halted after Interpreter setup", 31);

  if (interpreter.AllocateTensors() != kTfLiteOk) {
    std::strncpy(inference_status_message, "Error during AllocateTensors", 31);
    return -1;
  }

  inference_diag[0] = static_cast<uint32_t>(interpreter.inputs_size());
  inference_diag[1] = static_cast<uint32_t>(interpreter.outputs_size());

  TfLiteTensor* input = interpreter.input(0);
  if (input == nullptr || input->bytes < kInputBytes) {
    std::strncpy(inference_status_message, "Bad input tensor", 31);
    return -1;
  }
  inference_diag[2] = static_cast<uint32_t>(input->type);
  inference_diag[3] = static_cast<uint32_t>(input->bytes);
  inference_diag[4] = static_cast<uint32_t>(input->params.zero_point);
  {
    float s = input->params.scale;
    uint32_t bits;
    std::memcpy(&bits, &s, 4);
    inference_diag[5] = bits;
  }
  std::memcpy(input->data.data, inference_input, kInputBytes);
  std::memcpy(&inference_diag[12], input->data.data, 4);

  const TfLiteStatus invoke_status = interpreter.Invoke();
  if (invoke_status != kTfLiteOk) {
    std::strncpy(inference_status_message, "Error during Invoke", 31);
    return -1;
  }

  TfLiteTensor* output = interpreter.output(0);
  if (output == nullptr || output->bytes < kOutputBytes) {
    std::strncpy(inference_status_message, "Bad output tensor", 31);
    return -1;
  }
  inference_diag[6] = static_cast<uint32_t>(output->type);
  inference_diag[7] = static_cast<uint32_t>(output->bytes);
  inference_diag[8] = static_cast<uint32_t>(output->params.zero_point);
  {
    float s = output->params.scale;
    uint32_t bits;
    std::memcpy(&bits, &s, 4);
    inference_diag[9] = bits;
  }
  std::memcpy(&inference_diag[10], output->data.data, 4);
  if (output->bytes >= 4) {
    std::memcpy(&inference_diag[11],
                static_cast<const uint8_t*>(output->data.data) +
                    (output->bytes - 4),
                4);
  }
  std::memcpy(inference_output, output->data.data, kOutputBytes);

  inference_diag[13] = static_cast<uint32_t>(interpreter.arena_used_bytes());

  std::strncpy(inference_status_message, "Invoke successful", 31);
  inference_status = 0;
  return 0;
}
