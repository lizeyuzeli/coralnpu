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
// "FD-MobileNet alpha=0.25 128x128 tfs INT8" tf_flowers (5-class) classifier.
//
// Op inventory (10 distinct kinds):
//   CONV_2D x13, DEPTHWISE_CONV_2D x11, MEAN x1, SHAPE x2,
//   STRIDED_SLICE x2, PACK x2, RESHAPE x2, SOFTMAX x1,
//   QUANTIZE x1, DEQUANTIZE x1.
//
// Boundary dtypes:
//   Input  : (1, 128, 128, 3) uint8 -- mapped to [-1.0, 0.0] in float-domain
//                                       (scale=1/255, zp=255).
//   Output : (1, 5)           float32 (the trailing DEQUANTIZE converts the
//                                      int8 logits to float32 probabilities).
//
// Heavy ops (CONV_2D / DEPTHWISE_CONV_2D) are routed through the optimized
// RVV kernels; the remaining structural / shape / softmax / quantize ops
// use the reference TFLM kernels.

#include <stdint.h>
#include <stdio.h>

#include <cstring>

#include "sw/opt/litert-micro/conv.h"
#include "sw/opt/litert-micro/depthwise_conv.h"
#include "tensorflow/lite/core/c/common.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/system_setup.h"
#include "tests/cocotb/tflite/st_ai_zoo/fdmobilenet_a025_128_tfs_int8_tf_flowers/fdmobilenet_a025_128_tfs_int8.h"

namespace {
using FdMobileNetOpResolver = tflite::MicroMutableOpResolver<10>;
using coralnpu_v2::opt::litert_micro::Register_CONV_2D;
using coralnpu_v2::opt::litert_micro::Register_DEPTHWISE_CONV_2D;

TfLiteStatus RegisterOps(FdMobileNetOpResolver& op_resolver) {
  // Heavy ops -- routed to the optimized RVV kernels.
  TF_LITE_ENSURE_STATUS(op_resolver.AddConv2D(Register_CONV_2D()));
  TF_LITE_ENSURE_STATUS(
      op_resolver.AddDepthwiseConv2D(Register_DEPTHWISE_CONV_2D()));

  // Reference (non-RVV) ops used in this model.
  TF_LITE_ENSURE_STATUS(op_resolver.AddMean());
  TF_LITE_ENSURE_STATUS(op_resolver.AddShape());
  TF_LITE_ENSURE_STATUS(op_resolver.AddStridedSlice());
  TF_LITE_ENSURE_STATUS(op_resolver.AddPack());
  TF_LITE_ENSURE_STATUS(op_resolver.AddReshape());
  TF_LITE_ENSURE_STATUS(op_resolver.AddSoftmax());

  // Boundary quantize/dequantize wrappers (uint8 -> int8, int8 -> float32).
  TF_LITE_ENSURE_STATUS(op_resolver.AddQuantize());
  TF_LITE_ENSURE_STATUS(op_resolver.AddDequantize());
  return kTfLiteOk;
}

constexpr int kInputElems = 1 * 128 * 128 * 3;     // uint8 image
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

// FD-MobileNet @128 is larger than the 96x96 model -- first-layer activations
// are 128x128x8 int8 (~128 KB) and the int8 QUANTIZE output is 128x128x3
// (~48 KB). 768 KB arena is comfortable; place in .extdata to stay outside
// DTCM.
constexpr size_t kTensorArenaSize = 512 * 1024;
uint8_t tensor_arena[kTensorArenaSize]
    __attribute__((section(".data"), aligned(16)));
}

int main(int argc, char** argv) {
  std::strncpy(inference_status_message, "Started", 31);

  const tflite::Model* model =
      tflite::GetModel(g_fdmobilenet_a025_128_tfs_int8_model_data);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    std::strncpy(inference_status_message, "Bad model schema version", 31);
    return -1;
  }

  FdMobileNetOpResolver op_resolver;
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

  TfLiteTensor* input = interpreter.input(0);
  if (input == nullptr || input->bytes < kInputBytes) {
    std::strncpy(inference_status_message, "Bad input tensor", 31);
    return -1;
  }
  std::memcpy(input->data.data, inference_input, kInputBytes);

  if (interpreter.Invoke() != kTfLiteOk) {
    std::strncpy(inference_status_message, "Error during Invoke", 31);
    return -1;
  }

  TfLiteTensor* output = interpreter.output(0);
  if (output == nullptr || output->bytes < kOutputBytes) {
    std::strncpy(inference_status_message, "Bad output tensor", 31);
    return -1;
  }
  std::memcpy(inference_output, output->data.data, kOutputBytes);

  std::strncpy(inference_status_message, "Invoke successful", 31);
  inference_status = 0;
  return 0;
}
