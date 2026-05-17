# Copyright 2026 Li Zeyu <lizeyuzeli000lzy@gmail.com>
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

# End-to-end TFLite-Micro test for the ST-AI-Zoo "MobileNet-V1 alpha=0.25
# 224x224 INT8" ImageNet classifier. Exercises the optimized RVV CONV_2D /
# DEPTHWISE_CONV_2D kernels (28 layers total) plus the boundary
# QUANTIZE (uint8 -> int8) / DEQUANTIZE (int8 -> float32) wrappers.

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

# Embed the ~590 KB .tflite flatbuffer as C arrays. The basename is reused
# verbatim as the C symbol prefix: g_mobilenetv1_a025_224_int8_model_data.
generate_cc_arrays(
    name = "mobilenetv1_a025_224_int8_cc",
    src = "models/mobilenetv1_a025_224_int8.tflite",
    out = "mobilenetv1_a025_224_int8.cc",
)

generate_cc_arrays(
    name = "mobilenetv1_a025_224_int8_h",
    src = "models/mobilenetv1_a025_224_int8.tflite",
    out = "mobilenetv1_a025_224_int8.h",
)

coralnpu_v2_binary(
    name = "run_mobilenetv1_a025_224_int8_binary",
    srcs = [
        "run_mobilenetv1_a025_224_int8.cc",
        ":mobilenetv1_a025_224_int8_cc",
    ],
    hdrs = [":mobilenetv1_a025_224_int8_h"],
    # RvvCoreMiniHighmemAxi is hard-bound to 1024 KB ITCM + 1024 KB DTCM in
    # Parameters.scala. The .tflite (~590 KB) lives in .rodata -> ITCM; the
    # tensor_arena (768 KB) + .data/.bss/.stack live in DTCM. Tight but fits.
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
    visibility = ["//visibility:public"],
    deps = [
        "//sw/opt/litert-micro:conv",
        "//sw/opt/litert-micro:depthwise_conv",
        "@tflite_micro//tensorflow/lite/micro:micro_framework",
        "@tflite_micro//tensorflow/lite/micro:micro_log",
        "@tflite_micro//tensorflow/lite/micro:micro_profiler",
        "@tflite_micro//tensorflow/lite/micro:op_resolvers",
        "@tflite_micro//tensorflow/lite/micro:system_setup",
    ],
)

MOBILENETV1_A025_224_INT8_TESTCASES = [
    "core_mini_rvv_mobilenetv1_a025_224_int8",
]

cocotb_test_suite(
    name = "cocotb_mobilenetv1_a025_224_int8",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = MOBILENETV1_A025_224_INT8_TESTCASES,
    testcases_vname = "MOBILENETV1_A025_224_INT8_TESTCASES",
    tests_kwargs = {
        "waves": False,
        # Real-model inference is too slow for default CI; opt in explicitly.
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": ["cocotb_mobilenetv1_a025_224_int8.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_mobilenetv1_a025_224_int8_binary.elf",
            "test_data/input_0.npy",
            "test_data/expected_output_0.npy",
        ],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + glob(["test_data/*.npy"]) + VCS_COMMON_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = ["//hdl/chisel/src/coralnpu:rvv_core_mini_highmem_axi_cc_library_verilog"],
    verilator_model = "//tests/cocotb:rvv_core_mini_highmem_axi_model",
)
