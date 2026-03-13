# Copyright 2025 Google LLC
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

load("@coralnpu_hw//third_party/python:requirements.bzl", "requirement")
load("//rules:coco_tb.bzl", "cocotb_test_suite", "verilator_cocotb_model")
load("//rules:coralnpu_v2.bzl", "coralnpu_v2_binary")
load("//rules:utils.bzl", "generate_cc_arrays")
load(
    "//tests/cocotb:build_defs.bzl",
    "VCS_BUILD_ARGS",
    "VCS_DEFINES",
    "VCS_TEST_ARGS",
    "VERILATOR_BUILD_ARGS",
)

coralnpu_v2_binary(
    name = "conv2d_1x1_test",
    srcs = ["conv2d_1x1_test.cc"],
    dtcm_size_kbytes = 1024,  # Highmem
    itcm_size_kbytes = 1024,  # Highmem
    deps = [
        "//tape_out/sw/opt/litert-micro:conv_1x1",
        "@tflite_micro//tensorflow/lite/kernels/internal:reference_base",
    ],
)

cocotb_test_suite(
    name = "cocotb_conv2d_1x1",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = [
        "test_conv2d_1x1_48x48_h2w2",
    ],
    tests_kwargs = {
        "waves": True,
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "size": "enormous",
        "default_testcase_size": "medium",
        "test_module": ["cocotb_conv2d_1x1.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["conv2d_1x1_test.elf"],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + [
        "//tests/cocotb:coverage_exclude.cfg",
    ],
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = ["//hdl/chisel/src/coralnpu:rvv_core_mini_highmem_axi_cc_library_verilog"],
    verilator_model = "//tests/cocotb:rvv_core_mini_highmem_axi_model",
)

coralnpu_v2_binary(
    name = "run_mobilenet_1x1_v1_025_partial_binary",
    srcs = [
        "mobilenet_v1_025_partial_layers_cc",
        "run_mobilenet_1x1_partial_layers.cc",
    ],
    hdrs = ["mobilenet_v1_025_partial_layers.h"],
    dtcm_size_kbytes = 1024,  # Highmem
    itcm_size_kbytes = 1024,  # Highmem
    visibility = ["//visibility:public"],
    deps = [
        "//tape_out/sw/opt/litert-micro:conv_1x1",
        "//sw/opt/litert-micro:depthwise_conv",
        "@tflite_micro//tensorflow/lite/micro:micro_framework",
        "@tflite_micro//tensorflow/lite/micro:micro_log",
        "@tflite_micro//tensorflow/lite/micro:micro_profiler",
        "@tflite_micro//tensorflow/lite/micro:op_resolvers",
        "@tflite_micro//tensorflow/lite/micro:system_setup",
    ],
)

generate_cc_arrays(
    name = "mobilenet_v1_025_partial_layers_cc",
    src = "models/mobilenet_v1_025_partial_layers.tflite",
    out = "mobilenet_v1_025_partial_layers.cc",
)

generate_cc_arrays(
    name = "mobilenet_v1_025_partial_layers_h",
    src = "models/mobilenet_v1_025_partial_layers.tflite",
    out = "mobilenet_v1_025_partial_layers.h",
)

# BEGIN_TESTCASES_FOR_running_tflite
RUN_MOBILENET_TEST = [
    "core_mini_rvv_mobilenet_v1",
]
# END_TESTCASES_FOR_running_tflite

cocotb_test_suite(
    name = "cocotb_run_mobilenet_1x1_v1_partial_layers",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = RUN_MOBILENET_TEST,
    testcases_vname = "RUN_MOBILENET_TEST",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": ["cocotb_run_mobilenet_1x1_v1_partial_layers.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["run_mobilenet_1x1_v1_025_partial_binary.elf"],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + [
        "//tests/cocotb:coverage_exclude.cfg",
    ],
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = ["//hdl/chisel/src/coralnpu:rvv_core_mini_highmem_axi_cc_library_verilog"],
    verilator_model = "//tests/cocotb:rvv_core_mini_highmem_axi_model",
)