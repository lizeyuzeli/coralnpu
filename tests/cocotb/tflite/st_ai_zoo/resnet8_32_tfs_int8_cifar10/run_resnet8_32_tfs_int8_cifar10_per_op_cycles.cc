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

// Per-op cycle profiling variant of the ResNet-8 32x32 INT8 CIFAR-10 runner.

#include <stdint.h>
#include <stdio.h>

#include <cstring>

#include "sw/opt/litert-micro/conv.h"
#include "sw/opt/litert-micro/fully_connected.h"
#include "sw/utils/utils.h"
#include "tensorflow/lite/core/c/common.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_profiler_interface.h"
#include "tensorflow/lite/micro/system_setup.h"
#include "tests/cocotb/tflite/st_ai_zoo/resnet8_32_tfs_int8_cifar10/resnet8_32_tfs_int8.h"

namespace {
using ResNet8OpResolver = tflite::MicroMutableOpResolver<8>;
using coralnpu_v2::opt::litert_micro::Register_CONV_2D;
using coralnpu_v2::opt::litert_micro::Register_FULLY_CONNECTED;

TfLiteStatus RegisterOps(ResNet8OpResolver& op_resolver) {
  TF_LITE_ENSURE_STATUS(op_resolver.AddConv2D(Register_CONV_2D()));
  TF_LITE_ENSURE_STATUS(
      op_resolver.AddFullyConnected(Register_FULLY_CONNECTED()));
  TF_LITE_ENSURE_STATUS(op_resolver.AddAdd());
  TF_LITE_ENSURE_STATUS(op_resolver.AddAveragePool2D());
  TF_LITE_ENSURE_STATUS(op_resolver.AddReshape());
  TF_LITE_ENSURE_STATUS(op_resolver.AddSoftmax());
  TF_LITE_ENSURE_STATUS(op_resolver.AddQuantize());
  TF_LITE_ENSURE_STATUS(op_resolver.AddDequantize());
  return kTfLiteOk;
}

constexpr int kInputElems = 1 * 32 * 32 * 3;
constexpr int kInputBytes = kInputElems * 1;
constexpr int kOutputElems = 1 * 10;
constexpr int kOutputBytes = kOutputElems * 4;

constexpr int kMaxOps = 64;
constexpr int kTagBytes = 24;

class CycleProfiler : public tflite::MicroProfilerInterface {
 public:
  uint32_t BeginEvent(const char* tag) override;
  void EndEvent(uint32_t handle) override;

 private:
  uint64_t start_cycles_[kMaxOps] = {};
};
}  // namespace

extern "C" {
int8_t inference_status = -1;
char inference_status_message[31]
    __attribute__((section(".data"), aligned(16)));

uint8_t inference_input[kInputBytes]
    __attribute__((section(".data"), aligned(16)));
float inference_output[kOutputElems]
    __attribute__((section(".data"), aligned(16)));

uint32_t op_profile_count __attribute__((section(".data"))) = 0;
uint32_t op_profile_cycles[kMaxOps] __attribute__((section(".data"))) = {};
char op_profile_tags[kMaxOps * kTagBytes]
    __attribute__((section(".data"))) = {};

constexpr size_t kTensorArenaSize = 512 * 1024;
uint8_t tensor_arena[kTensorArenaSize]
    __attribute__((section(".data"), aligned(16)));
}

namespace {
uint32_t CycleProfiler::BeginEvent(const char* tag) {
  if (op_profile_count >= kMaxOps) {
    return kMaxOps - 1;
  }
  uint32_t h = op_profile_count;
  char* dst = &op_profile_tags[h * kTagBytes];
  std::strncpy(dst, tag != nullptr ? tag : "?", kTagBytes - 1);
  dst[kTagBytes - 1] = '\0';
  start_cycles_[h] = mcycle_read();
  ++op_profile_count;
  return h;
}

void CycleProfiler::EndEvent(uint32_t handle) {
  uint64_t end = mcycle_read();
  if (handle < kMaxOps) {
    uint64_t delta = end - start_cycles_[handle];
    op_profile_cycles[handle] =
        delta > 0xFFFFFFFFull ? 0xFFFFFFFFu : static_cast<uint32_t>(delta);
  }
}
}  // namespace

int main(int argc, char** argv) {
  std::strncpy(inference_status_message, "Started", 31);

  const tflite::Model* model =
      tflite::GetModel(g_resnet8_32_tfs_int8_model_data);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    std::strncpy(inference_status_message, "Bad model schema version", 31);
    return -1;
  }

  ResNet8OpResolver op_resolver;
  if (RegisterOps(op_resolver) != kTfLiteOk) {
    std::strncpy(inference_status_message, "Error registering ops", 31);
    return -1;
  }
  std::strncpy(inference_status_message, "Halted after op resolver", 31);

  CycleProfiler profiler;
  tflite::MicroInterpreter interpreter(
      model, op_resolver, tensor_arena, kTensorArenaSize,
      /*resource_variables=*/nullptr, &profiler);
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
