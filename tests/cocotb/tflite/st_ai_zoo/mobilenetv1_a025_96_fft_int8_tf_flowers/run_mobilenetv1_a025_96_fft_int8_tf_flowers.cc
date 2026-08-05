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
// "MobileNet-V1 alpha=0.25 96x96 FFT INT8" tf_flowers (5-class) classifier.
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
// All compute-heavy ops (CONV_2D / DEPTHWISE_CONV_2D / FULLY_CONNECTED) are
// int8->int8 internally and exercise the optimized RVV kernels; FC weights
// in this model are per-channel quantized, which the RVV kernel handles via
// its FullyConnectedPerChannel path.

#include <stdint.h>
#include <stdio.h>

#include <cstring>

#include "sw/opt/litert-micro/conv.h"
#include "sw/opt/litert-micro/depthwise_conv.h"
#include "sw/opt/litert-micro/fully_connected.h"
#include "tensorflow/lite/core/c/common.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/system_setup.h"
#include "tests/cocotb/tflite/st_ai_zoo/mobilenetv1_a025_96_fft_int8_tf_flowers/mobilenetv1_a025_96_fft_int8.h"

namespace {
using MobileNetV1OpResolver = tflite::MicroMutableOpResolver<7>;
using coralnpu_v2::opt::litert_micro::Register_CONV_2D;
using coralnpu_v2::opt::litert_micro::Register_DEPTHWISE_CONV_2D;
using coralnpu_v2::opt::litert_micro::Register_FULLY_CONNECTED;

TfLiteStatus RegisterOps(MobileNetV1OpResolver& op_resolver) {
  // Heavy ops -- routed to the optimized RVV kernels.
  TF_LITE_ENSURE_STATUS(op_resolver.AddConv2D(Register_CONV_2D()));
  TF_LITE_ENSURE_STATUS(
      op_resolver.AddDepthwiseConv2D(Register_DEPTHWISE_CONV_2D()));
  TF_LITE_ENSURE_STATUS(
      op_resolver.AddFullyConnected(Register_FULLY_CONNECTED()));

  // Boundary quantize / dequantize wrappers.
  TF_LITE_ENSURE_STATUS(op_resolver.AddQuantize());
  TF_LITE_ENSURE_STATUS(op_resolver.AddDequantize());

  // Spatial / structural ops used inside MobileNet-V1.
  TF_LITE_ENSURE_STATUS(op_resolver.AddMean());     // global avg-pool
  TF_LITE_ENSURE_STATUS(op_resolver.AddSoftmax());

  return kTfLiteOk;
}

constexpr int kInputElems = 1 * 96 * 96 * 3;        // uint8 RGB image
constexpr int kInputBytes = kInputElems * 1;
constexpr int kOutputElems = 1 * 5;                 // float32 probabilities
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

  MobileNetV1OpResolver op_resolver;
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
