# Copyright 2026 Li Zeyu <lizeyuzeli000lzy@gmail.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Phase-1 fault-injection campaign on the DNN-Small INT8 inference test.
# Reuses the ELF + reference IO arrays produced by //tests/cocotb/tflite/
# arm_ml_zoo/dnn_small_int8 and adds a cocotb test that performs single-bit transient
# flips of the RVV vector register file.

load("//rules:coco_tb.bzl", "cocotb_test_suite")
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

py_library(
    name = "fi_utils",
    srcs = ["fi_utils.py"],
    visibility = ["//visibility:public"],
)

DNN_SMALL_INT8_FI_TESTCASES = [
    "core_mini_rvv_dnn_small_int8_fi",
]

cocotb_test_suite(
    name = "cocotb_dnn_small_int8_fi",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = DNN_SMALL_INT8_FI_TESTCASES,
    testcases_vname = "DNN_SMALL_INT8_FI_TESTCASES",
    tests_kwargs = {
        "waves": False,
        # Manual-only: this is a research/eval flow, not part of CI.
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": ["cocotb_dnn_small_int8_fi.py"],
        "deps": [
            ":fi_utils",
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "//tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:run_dnn_small_int8_binary_elf_public",
            "//tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:test_data/input_0.npy",
            "//tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:test_data/expected_output_0.npy",
        ],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = VCS_COMMON_DATA + [
        "//tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:run_dnn_small_int8_binary_elf_public",
        "//tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:test_data/input_0.npy",
        "//tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:test_data/expected_output_0.npy",
    ],
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = ["//hdl/chisel/src/coralnpu:rvv_core_mini_highmem_axi_cc_library_verilog"],
    verilator_model = "//tests/cocotb:rvv_core_mini_highmem_axi_model",
)
