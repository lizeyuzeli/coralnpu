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

# End-to-end TFLite-Micro test for the ST-AI-Zoo "ResNet-8 32x32 tfs INT8"
# CIFAR-10 (10-class) classifier. Two suites: a clean argmax+tolerance
# inference run and a per-op cycle profiler run.

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
    name = "resnet8_32_tfs_int8_cc",
    src = "models/resnet8_32_tfs_int8.tflite",
    out = "resnet8_32_tfs_int8.cc",
)

generate_cc_arrays(
    name = "resnet8_32_tfs_int8_h",
    src = "models/resnet8_32_tfs_int8.tflite",
    out = "resnet8_32_tfs_int8.h",
)

# --- Clean runner.
coralnpu_v2_binary(
    name = "run_resnet8_32_tfs_int8_cifar10_binary",
    srcs = [
        "run_resnet8_32_tfs_int8_cifar10.cc",
        ":resnet8_32_tfs_int8_cc",
    ],
    hdrs = [":resnet8_32_tfs_int8_h"],
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
    visibility = ["//visibility:public"],
    deps = [
        "//sw/opt/litert-micro:conv",
        "//sw/opt/litert-micro:fully_connected",
        "@tflite_micro//tensorflow/lite/micro:micro_framework",
        "@tflite_micro//tensorflow/lite/micro:micro_log",
        "@tflite_micro//tensorflow/lite/micro:micro_profiler",
        "@tflite_micro//tensorflow/lite/micro:op_resolvers",
        "@tflite_micro//tensorflow/lite/micro:system_setup",
    ],
)

RESNET8_32_TFS_INT8_CIFAR10_TESTCASES = [
    "core_mini_rvv_resnet8_32_tfs_int8_cifar10",
]

cocotb_test_suite(
    name = "cocotb_resnet8_32_tfs_int8_cifar10",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = RESNET8_32_TFS_INT8_CIFAR10_TESTCASES,
    testcases_vname = "RESNET8_32_TFS_INT8_CIFAR10_TESTCASES",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": ["cocotb_resnet8_32_tfs_int8_cifar10.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_resnet8_32_tfs_int8_cifar10_binary.elf",
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

# --- Per-op cycle profiler runner.
coralnpu_v2_binary(
    name = "run_resnet8_32_tfs_int8_cifar10_per_op_cycles_binary",
    srcs = [
        "run_resnet8_32_tfs_int8_cifar10_per_op_cycles.cc",
        ":resnet8_32_tfs_int8_cc",
    ],
    hdrs = [":resnet8_32_tfs_int8_h"],
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
    visibility = ["//visibility:public"],
    deps = [
        "//sw/opt/litert-micro:conv",
        "//sw/opt/litert-micro:fully_connected",
        "//sw/utils:utils",
        "@tflite_micro//tensorflow/lite/micro:micro_framework",
        "@tflite_micro//tensorflow/lite/micro:micro_log",
        "@tflite_micro//tensorflow/lite/micro:micro_profiler",
        "@tflite_micro//tensorflow/lite/micro:op_resolvers",
        "@tflite_micro//tensorflow/lite/micro:system_setup",
    ],
)

RESNET8_32_TFS_INT8_CIFAR10_PER_OP_CYCLES_TESTCASES = [
    "core_mini_rvv_resnet8_32_tfs_int8_cifar10_per_op_cycles",
]

cocotb_test_suite(
    name = "cocotb_resnet8_32_tfs_int8_cifar10_per_op_cycles",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = RESNET8_32_TFS_INT8_CIFAR10_PER_OP_CYCLES_TESTCASES,
    testcases_vname = "RESNET8_32_TFS_INT8_CIFAR10_PER_OP_CYCLES_TESTCASES",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": ["cocotb_resnet8_32_tfs_int8_cifar10_per_op_cycles.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_resnet8_32_tfs_int8_cifar10_per_op_cycles_binary.elf",
            "test_data/input_0.npy",
        ],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + glob(["test_data/*.npy"]) + VCS_COMMON_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = ["//hdl/chisel/src/coralnpu:rvv_core_mini_highmem_axi_cc_library_verilog"],
    verilator_model = "//tests/cocotb:rvv_core_mini_highmem_axi_model",
)
