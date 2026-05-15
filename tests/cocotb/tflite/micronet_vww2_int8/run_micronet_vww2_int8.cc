// Copyright 2026 Google LLC
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

// End-to-end TFLite-Micro inference test for the Arm ML-zoo
// "MicroNet VWW-2 INT8" visual-wake-words model.
//
// Operators (see definition.yaml): ADD, AVERAGE_POOL_2D, CONV_2D,
// DEPTHWISE_CONV_2D, PAD, RELU6, RESHAPE.
//
// Input shape  : (1, 50, 50, 1) int8 (50x50 grayscale image)
// Output shape : (1, 2)         int8 (per-class confidence)

#include <stdint.h>
#include <stdio.h>

#include <cstring>

#include "sw/opt/litert-micro/conv.h"
#include "sw/opt/litert-micro/depthwise_conv.h"
#include "tensorflow/lite/core/c/common.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/system_setup.h"
#include "tests/cocotb/tflite/micronet_vww2_int8/vww2_50_50_INT8.h"

namespace {
using VwwOpResolver = tflite::MicroMutableOpResolver<7>;
using coralnpu_v2::opt::litert_micro::Register_CONV_2D;
using coralnpu_v2::opt::litert_micro::Register_DEPTHWISE_CONV_2D;

TfLiteStatus RegisterOps(VwwOpResolver& op_resolver) {
  TF_LITE_ENSURE_STATUS(op_resolver.AddAdd());
  TF_LITE_ENSURE_STATUS(op_resolver.AddAveragePool2D());
  TF_LITE_ENSURE_STATUS(op_resolver.AddConv2D(Register_CONV_2D()));
  TF_LITE_ENSURE_STATUS(
      op_resolver.AddDepthwiseConv2D(Register_DEPTHWISE_CONV_2D()));
  TF_LITE_ENSURE_STATUS(op_resolver.AddPad());
  TF_LITE_ENSURE_STATUS(op_resolver.AddRelu6());
  TF_LITE_ENSURE_STATUS(op_resolver.AddReshape());
  return kTfLiteOk;
}

constexpr int kInputBytes = 1 * 50 * 50 * 1;  // int8 image
constexpr int kOutputBytes = 1 * 2;           // int8 logits
}  // namespace

extern "C" {
int8_t inference_status = -1;
char inference_status_message[31]
    __attribute__((section(".data"), aligned(16)));

int8_t inference_input[kInputBytes]
    __attribute__((section(".data"), aligned(16)));
int8_t inference_output[kOutputBytes]
    __attribute__((section(".data"), aligned(16)));

// MicroNet VWW-2 (~280KB model) needs working memory for several conv blocks
// + the largest activation tile (~50*50*channels). 1MB is comfortable.
constexpr size_t kTensorArenaSize = 512 * 1024;
uint8_t tensor_arena[kTensorArenaSize]
    __attribute__((section(".data"), aligned(16)));
}

int main(int argc, char** argv) {
  std::strncpy(inference_status_message, "Started", 31);

  const tflite::Model* model = tflite::GetModel(g_vww2_50_50_INT8_model_data);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    std::strncpy(inference_status_message, "Bad model schema version", 31);
    return -1;
  }

  VwwOpResolver op_resolver;
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
