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

# End-to-end TFLite-Micro test for the Arm ML-zoo "MicroNet VWW-2 INT8"
# visual-wake-words model. Exercises Conv2D (incl. the optimized 1x1/3x3
# RVV kernels), DepthwiseConv2D, AveragePool2D, Add, Pad, ReLU6, Reshape.

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

generate_cc_arrays(
    name = "vww2_50_50_INT8_cc",
    src = "models/vww2_50_50_INT8.tflite",
    out = "vww2_50_50_INT8.cc",
)

generate_cc_arrays(
    name = "vww2_50_50_INT8_h",
    src = "models/vww2_50_50_INT8.tflite",
    out = "vww2_50_50_INT8.h",
)

coralnpu_v2_binary(
    name = "run_micronet_vww2_int8_binary",
    srcs = [
        "run_micronet_vww2_int8.cc",
        ":vww2_50_50_INT8_cc",
    ],
    hdrs = [":vww2_50_50_INT8_h"],
    dtcm_size_kbytes = 1024,  # Highmem
    itcm_size_kbytes = 1024,  # Highmem
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

MICRONET_VWW2_INT8_TESTCASES = [
    "core_mini_rvv_micronet_vww2_int8",
]

cocotb_test_suite(
    name = "cocotb_micronet_vww2_int8",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = MICRONET_VWW2_INT8_TESTCASES,
    testcases_vname = "MICRONET_VWW2_INT8_TESTCASES",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": ["cocotb_micronet_vww2_int8.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_micronet_vww2_int8_binary.elf",
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
