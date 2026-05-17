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
// "RNNoise INT8" noise-suppression model.
//
// The model has four int8 inputs and five int8 outputs (see
// models/definition.yaml). The cocotb harness stages every input over AXI
// into a dedicated named global buffer and reads back every output from a
// dedicated named global buffer. Inputs/outputs are dispatched into the
// right tensor index using the (unique) tensor byte size.
//
// Model operators (see definition.yaml): ADD, CONCATENATION, DEQUANTIZE,
// FULLY_CONNECTED, LOGISTIC, MUL, PACK, QUANTIZE, RELU, RESHAPE, SPLIT,
// SPLIT_V, SUB, TANH, UNPACK.

#include <stdint.h>
#include <stdio.h>

#include <cstring>

#include "sw/opt/litert-micro/fully_connected.h"
#include "tensorflow/lite/core/c/common.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/system_setup.h"
#include "tests/cocotb/tflite/arm_ml_zoo/rnn_noise_int8/rnnoise_int8.h"

namespace {
using RnnNoiseOpResolver = tflite::MicroMutableOpResolver<16>;
using coralnpu_v2::opt::litert_micro::Register_FULLY_CONNECTED;

TfLiteStatus RegisterOps(RnnNoiseOpResolver& op_resolver) {
  TF_LITE_ENSURE_STATUS(op_resolver.AddAdd());
  TF_LITE_ENSURE_STATUS(op_resolver.AddConcatenation());
  TF_LITE_ENSURE_STATUS(op_resolver.AddDequantize());
  TF_LITE_ENSURE_STATUS(op_resolver.AddFullyConnected(Register_FULLY_CONNECTED()));
  TF_LITE_ENSURE_STATUS(op_resolver.AddLogistic());
  TF_LITE_ENSURE_STATUS(op_resolver.AddMul());
  TF_LITE_ENSURE_STATUS(op_resolver.AddPack());
  TF_LITE_ENSURE_STATUS(op_resolver.AddQuantize());
  TF_LITE_ENSURE_STATUS(op_resolver.AddRelu());
  TF_LITE_ENSURE_STATUS(op_resolver.AddReshape());
  TF_LITE_ENSURE_STATUS(op_resolver.AddSplit());
  TF_LITE_ENSURE_STATUS(op_resolver.AddSplitV());
  TF_LITE_ENSURE_STATUS(op_resolver.AddSub());
  TF_LITE_ENSURE_STATUS(op_resolver.AddTanh());
  TF_LITE_ENSURE_STATUS(op_resolver.AddUnpack());
  return kTfLiteOk;
}

// Input tensor sizes (bytes, int8). All unique -> usable for dispatch.
constexpr int kInMainBytes = 42;          // main_input_int8       (1,1,42)
constexpr int kInVadBytes = 24;           // vad_gru_prev_state    (1,24)
constexpr int kInNoiseBytes = 48;         // noise_gru_prev_state  (1,48)
constexpr int kInDenoiseBytes = 96;       // denoise_gru_prev_state (1,96)

// Output tensor sizes (bytes, int8). All unique -> usable for dispatch.
constexpr int kOutDenoiseStateBytes = 96; // Identity   (denoise gru next state)
constexpr int kOutGainsBytes = 22;        // Identity_1 (gain values)
constexpr int kOutNoiseStateBytes = 48;   // Identity_2 (noise gru next state)
constexpr int kOutVadStateBytes = 24;     // Identity_3 (vad gru next state)
constexpr int kOutVadProbBytes = 1;       // Identity_4 (voice activity prob)
}  // namespace

extern "C" {
int8_t inference_status = -1;
char inference_status_message[31]
    __attribute__((section(".data"), aligned(16)));

// Inputs (written by cocotb before launch).
int8_t input_main[kInMainBytes]
    __attribute__((section(".data"), aligned(16)));
int8_t input_vad_state[kInVadBytes]
    __attribute__((section(".data"), aligned(16)));
int8_t input_noise_state[kInNoiseBytes]
    __attribute__((section(".data"), aligned(16)));
int8_t input_denoise_state[kInDenoiseBytes]
    __attribute__((section(".data"), aligned(16)));

// Outputs (read by cocotb after halt).
int8_t output_denoise_state[kOutDenoiseStateBytes]
    __attribute__((section(".data"), aligned(16)));
int8_t output_gains[kOutGainsBytes]
    __attribute__((section(".data"), aligned(16)));
int8_t output_noise_state[kOutNoiseStateBytes]
    __attribute__((section(".data"), aligned(16)));
int8_t output_vad_state[kOutVadStateBytes]
    __attribute__((section(".data"), aligned(16)));
int8_t output_vad_prob[kOutVadProbBytes]
    __attribute__((section(".data"), aligned(16)));

// Tensor arena. RNNoise (~113KB model) is small; give it 1MB headroom.
constexpr size_t kTensorArenaSize = 1024 * 1024;
uint8_t tensor_arena[kTensorArenaSize]
    __attribute__((section(".extdata"), aligned(16)));
}

namespace {

bool StageInputs(tflite::MicroInterpreter& interpreter) {
  for (size_t i = 0; i < interpreter.inputs_size(); ++i) {
    TfLiteTensor* t = interpreter.input(i);
    if (t == nullptr) return false;
    const int8_t* src = nullptr;
    size_t expected = 0;
    switch (t->bytes) {
      case kInMainBytes:    src = input_main;          expected = kInMainBytes; break;
      case kInVadBytes:     src = input_vad_state;     expected = kInVadBytes; break;
      case kInNoiseBytes:   src = input_noise_state;   expected = kInNoiseBytes; break;
      case kInDenoiseBytes: src = input_denoise_state; expected = kInDenoiseBytes; break;
      default: return false;
    }
    if (t->bytes != expected) return false;
    std::memcpy(t->data.data, src, expected);
  }
  return true;
}

bool CollectOutputs(tflite::MicroInterpreter& interpreter) {
  for (size_t i = 0; i < interpreter.outputs_size(); ++i) {
    TfLiteTensor* t = interpreter.output(i);
    if (t == nullptr) return false;
    int8_t* dst = nullptr;
    size_t expected = 0;
    switch (t->bytes) {
      case kOutDenoiseStateBytes: dst = output_denoise_state; expected = kOutDenoiseStateBytes; break;
      case kOutGainsBytes:        dst = output_gains;         expected = kOutGainsBytes; break;
      case kOutNoiseStateBytes:   dst = output_noise_state;   expected = kOutNoiseStateBytes; break;
      case kOutVadStateBytes:     dst = output_vad_state;     expected = kOutVadStateBytes; break;
      case kOutVadProbBytes:      dst = output_vad_prob;      expected = kOutVadProbBytes; break;
      default: return false;
    }
    if (t->bytes != expected) return false;
    std::memcpy(dst, t->data.data, expected);
  }
  return true;
}

}  // namespace

int main(int argc, char** argv) {
  std::strncpy(inference_status_message, "Started", 31);

  const tflite::Model* model = tflite::GetModel(g_rnnoise_INT8_model_data);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    std::strncpy(inference_status_message, "Bad model schema version", 31);
    return -1;
  }

  RnnNoiseOpResolver op_resolver;
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

  if (!StageInputs(interpreter)) {
    std::strncpy(inference_status_message, "Error staging inputs", 31);
    return -1;
  }

  if (interpreter.Invoke() != kTfLiteOk) {
    std::strncpy(inference_status_message, "Error during Invoke", 31);
    return -1;
  }

  if (!CollectOutputs(interpreter)) {
    std::strncpy(inference_status_message, "Error collecting outputs", 31);
    return -1;
  }

  std::strncpy(inference_status_message, "Invoke successful", 31);
  inference_status = 0;
  return 0;
}
