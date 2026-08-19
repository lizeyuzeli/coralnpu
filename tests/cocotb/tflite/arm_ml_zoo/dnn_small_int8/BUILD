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

# End-to-end TFLite-Micro test for the Arm ML-zoo "DNN Small INT8"
# keyword-spotting model. This is a real model (no dummy/partial weights);
# the cocotb harness pushes the ML-zoo reference input over AXI, runs the
# interpreter, and compares the produced output against the reference.

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
    name = "dnn_s_quantized_cc",
    src = "models/dnn_s_quantized.tflite",
    out = "dnn_s_quantized.cc",
)

generate_cc_arrays(
    name = "dnn_s_quantized_h",
    src = "models/dnn_s_quantized.tflite",
    out = "dnn_s_quantized.h",
)

coralnpu_v2_binary(
    name = "run_dnn_small_int8_binary",
    srcs = [
        "run_dnn_small_int8.cc",
        ":dnn_s_quantized_cc",
    ],
    hdrs = [":dnn_s_quantized_h"],
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

DNN_SMALL_INT8_TESTCASES = [
    "core_mini_rvv_dnn_small_int8",
]

cocotb_test_suite(
    name = "cocotb_dnn_small_int8",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = DNN_SMALL_INT8_TESTCASES,
    testcases_vname = "DNN_SMALL_INT8_TESTCASES",
    tests_kwargs = {
        "waves": False,
        # Real-model inference is too slow for CI by default; mark manual
        # and let developers/nightly opt in explicitly.
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": ["cocotb_dnn_small_int8.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_dnn_small_int8_binary.elf",
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

# Fault-injection campaign on this model. The reusable harness lives in
# //tests/cocotb/fault_injection (target registry, bit-flip primitives,
# campaign scheduler, CSV writer); only the model-specific glue lives here.
#
# One testcase == one (module, fault_type) group == one bazel test target, so
# submitting the suite parallelizes the 12 groups across cores. Each name must
# match a @cocotb.test in cocotb_dnn_small_int8_fi.py (fi_<module>_<faulttype>).
# fi_run_all keeps the whole-matrix, FI_*-env-driven path for one-off runs.
DNN_SMALL_INT8_FI_TESTCASES = [
    "fi_decode_path_seu",
    "fi_decode_path_set",
    "fi_decode_path_stuck",
    "fi_compute_ctrl_seu",
    "fi_compute_ctrl_set",
    "fi_compute_ctrl_stuck",
    # Diagnostic item since Stage 3, not part of the 4-sub-module matrix: the
    # ROB data plane answers no FT-scheme question (DMR already covers what
    # reaches it) and its bit count distorted the cross-module ranking. Kept
    # runnable by name so the Stage 2 baseline stays reproducible.
    "fi_rob_data_seu",
    "fi_rob_data_set",
    "fi_rob_data_stuck",
    "fi_execute_seu",
    "fi_execute_set",
    "fi_execute_stuck",
    "fi_storage_seu",
    "fi_storage_set",
    "fi_storage_stuck",
    # Diagnostic items, not part of the 4-sub-module matrix (see fi_utils
    # INV-3). lsu is the 2296-bit LSU-interface buffer set the Stage 5 space
    # audit found outside every module's fault space.
    "fi_lsu_seu",
    "fi_lsu_set",
    "fi_lsu_stuck",
    "fi_fifo_ptr_seu",
    "fi_fifo_ptr_set",
    "fi_fifo_ptr_stuck",
    "fi_deposit_probe",
    "fi_space_audit",
    "fi_run_all",
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
            "//tests/cocotb/fault_injection:fi_utils",
            "//tests/cocotb/fault_injection:fi_audit",
            "//tests/cocotb/fault_injection:fi_campaign",
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_dnn_small_int8_binary.elf",
            "test_data/input_0.npy",
            "test_data/expected_output_0.npy",
        ],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + glob(["test_data/*.npy"]) + VCS_COMMON_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = ["//hdl/chisel/src/coralnpu:rvv_core_mini_highmem_axi_cc_library_verilog"],
    # FI-specific model: -DSTOP_COND=0 so a scalar-core assertion tripped by an
    # injected fault reports instead of aborting the group (see
    # //tests/cocotb/fault_injection:BUILD).
    verilator_model = "//tests/cocotb/fault_injection:rvv_core_mini_highmem_axi_fi_model",
)
