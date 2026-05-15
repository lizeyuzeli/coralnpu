# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# End-to-end TFLite-Micro test for the Arm ML-zoo "RNNoise INT8"
# noise-suppression model. The cocotb harness pushes all four ML-zoo
# reference inputs over AXI, runs the interpreter, and compares each of
# the five produced outputs against the ML-zoo reference.

load("//rules:coco_tb.bzl", "cocotb_test_suite")
load("//rules:coralnpu_v2.bzl", "coralnpu_v2_binary")
load("//rules:utils.bzl", "generate_cc_arrays")
load(
    "//tests/cocotb:build_defs.bzl",
    "VCS_BUILD_ARGS",
    "VCS_DEFINES",
    "VCS_TEST_ARGS",
)

VCS_COMMON_DATA = [
    "//tests/cocotb:coverage_exclude.cfg",
    "//hdl/verilog:dpi_files",
]

# Embed the .tflite flatbuffer as C arrays.
generate_cc_arrays(
    name = "rnnoise_int8_cc",
    src = "models/rnnoise_INT8.tflite",
    out = "rnnoise_int8.cc",
)

generate_cc_arrays(
    name = "rnnoise_int8_h",
    src = "models/rnnoise_INT8.tflite",
    out = "rnnoise_int8.h",
)

coralnpu_v2_binary(
    name = "run_rnn_noise_int8_binary",
    srcs = [
        "run_rnn_noise_int8.cc",
        ":rnnoise_int8_cc",
    ],
    hdrs = [":rnnoise_int8_h"],
    dtcm_size_kbytes = 1024,  # Highmem
    itcm_size_kbytes = 1024,  # Highmem
    visibility = ["//visibility:public"],
    deps = [
        "//sw/opt/litert-micro:fully_connected",
        "@tflite_micro//tensorflow/lite/micro:micro_framework",
        "@tflite_micro//tensorflow/lite/micro:micro_log",
        "@tflite_micro//tensorflow/lite/micro:micro_profiler",
        "@tflite_micro//tensorflow/lite/micro:op_resolvers",
        "@tflite_micro//tensorflow/lite/micro:system_setup",
    ],
)

RNN_NOISE_INT8_TESTCASES = [
    "core_mini_rvv_rnn_noise_int8",
]

cocotb_test_suite(
    name = "cocotb_rnn_noise_int8",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = RNN_NOISE_INT8_TESTCASES,
    testcases_vname = "RNN_NOISE_INT8_TESTCASES",
    tests_kwargs = {
        "waves": False,
        # Real-model inference is too slow for default CI; opt in explicitly.
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": ["cocotb_rnn_noise_int8.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_rnn_noise_int8_binary.elf",
            "test_data/main_input_int8_0.npy",
            "test_data/vad_gru_prev_state_int8_0.npy",
            "test_data/noise_gru_prev_state_int8_0.npy",
            "test_data/denoise_gru_prev_state_int8_0.npy",
            "test_data/Identity_int8_0.npy",
            "test_data/Identity_1_int8_0.npy",
            "test_data/Identity_2_int8_0.npy",
            "test_data/Identity_3_int8_0.npy",
            "test_data/Identity_4_int8_0.npy",
        ],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + glob(["test_data/*.npy"]) + VCS_COMMON_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = ["//hdl/chisel/src/coralnpu:rvv_core_mini_highmem_axi_cc_library_verilog"],
    verilator_model = "//tests/cocotb:rvv_core_mini_highmem_axi_model",
)
