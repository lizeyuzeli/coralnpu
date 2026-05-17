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

// End-to-end TFLite-Micro inference test for the Arm ML-zoo
// "DNN Small INT8" keyword-spotting model.
//
// The model has shape (1, 250) int8 input and (1, 12) int8 output and uses
// FULLY_CONNECTED, RELU and SOFTMAX kernels.
//
// Inputs/outputs are exposed as named globals so the cocotb harness can push
// in test inputs and read back the produced outputs over AXI.

#include <stdint.h>
#include <stdio.h>

#include <cstring>

#include "sw/opt/litert-micro/fully_connected.h"
#include "tensorflow/lite/core/c/common.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/system_setup.h"
#include "tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8/dnn_s_quantized.h"

namespace {
// FULLY_CONNECTED, RELU, SOFTMAX -- size 4 leaves a little headroom.
using DnnSmallOpResolver = tflite::MicroMutableOpResolver<4>;
using coralnpu_v2::opt::litert_micro::Register_FULLY_CONNECTED;

TfLiteStatus RegisterOps(DnnSmallOpResolver& op_resolver) {
  TF_LITE_ENSURE_STATUS(op_resolver.AddFullyConnected(Register_FULLY_CONNECTED()));
  TF_LITE_ENSURE_STATUS(op_resolver.AddRelu());
  TF_LITE_ENSURE_STATUS(op_resolver.AddSoftmax());
  return kTfLiteOk;
}

constexpr int kInputBytes = 1 * 250;   // int8
constexpr int kOutputBytes = 1 * 12;   // int8
}  // namespace

extern "C" {
// Status flags read by the cocotb harness.
int8_t inference_status = -1;
char inference_status_message[31]
    __attribute__((section(".data"), aligned(16)));

// Cocotb writes the test input here before execution and reads the
// inference output back from inference_output after halt.
int8_t inference_input[kInputBytes]
    __attribute__((section(".data"), aligned(16)));
int8_t inference_output[kOutputBytes]
    __attribute__((section(".data"), aligned(16)));

// Tensor arena. The DNN Small model is tiny; 256KB is plenty.
constexpr size_t kTensorArenaSize = 256 * 1024;
uint8_t tensor_arena[kTensorArenaSize]
    __attribute__((section(".data"), aligned(16)));
}

int main(int argc, char** argv) {
  std::strncpy(inference_status_message, "Started", 31);

  const tflite::Model* model = tflite::GetModel(g_dnn_s_quantized_model_data);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    std::strncpy(inference_status_message, "Bad model schema version", 31);
    return -1;
  }

  DnnSmallOpResolver op_resolver;
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
