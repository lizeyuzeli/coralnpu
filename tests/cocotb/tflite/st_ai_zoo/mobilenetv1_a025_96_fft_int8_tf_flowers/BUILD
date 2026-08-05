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
# 96x96 FFT INT8" tf_flowers (5-class) classifier. Exercises the optimized
# RVV CONV_2D / DEPTHWISE_CONV_2D / FULLY_CONNECTED kernels (28 layers total)
# plus the boundary QUANTIZE (uint8 -> int8) / DEQUANTIZE (int8 -> float32)
# wrappers.
#
# Two flavours of the same end-to-end run are wired here:
#   - the regular `cocotb_mobilenetv1_a025_96_fft_int8_tf_flowers`
#     target (clean runner, just argmax + tolerance check);
#   - the `cocotb_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_debug`
#     target which routes each heavy op through a stats wrapper and dumps
#     per-layer min/max/mean/zero-count stats back to the cocotb side -- used
#     for diagnosing layer-level regressions.

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

# Embed the ~300 KB .tflite flatbuffer as C arrays. The basename (without
# extension) is reused verbatim as the C symbol prefix:
# g_mobilenetv1_a025_96_fft_int8_model_data.
generate_cc_arrays(
    name = "mobilenetv1_a025_96_fft_int8_cc",
    src = "models/mobilenetv1_a025_96_fft_int8.tflite",
    out = "mobilenetv1_a025_96_fft_int8.cc",
)

generate_cc_arrays(
    name = "mobilenetv1_a025_96_fft_int8_h",
    src = "models/mobilenetv1_a025_96_fft_int8.tflite",
    out = "mobilenetv1_a025_96_fft_int8.h",
)

# --- Clean runner (default).
coralnpu_v2_binary(
    name = "run_mobilenetv1_a025_96_fft_int8_tf_flowers_binary",
    srcs = [
        "run_mobilenetv1_a025_96_fft_int8_tf_flowers.cc",
        ":mobilenetv1_a025_96_fft_int8_cc",
    ],
    hdrs = [":mobilenetv1_a025_96_fft_int8_h"],
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
    visibility = ["//visibility:public"],
    deps = [
        "//sw/opt/litert-micro:conv",
        "//sw/opt/litert-micro:depthwise_conv",
        "//sw/opt/litert-micro:fully_connected",
        "@tflite_micro//tensorflow/lite/micro:micro_framework",
        "@tflite_micro//tensorflow/lite/micro:micro_log",
        "@tflite_micro//tensorflow/lite/micro:micro_profiler",
        "@tflite_micro//tensorflow/lite/micro:op_resolvers",
        "@tflite_micro//tensorflow/lite/micro:system_setup",
    ],
)

MOBILENETV1_A025_96_FFT_INT8_TF_FLOWERS_TESTCASES = [
    "core_mini_rvv_mobilenetv1_a025_96_fft_int8_tf_flowers",
]

cocotb_test_suite(
    name = "cocotb_mobilenetv1_a025_96_fft_int8_tf_flowers",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = MOBILENETV1_A025_96_FFT_INT8_TF_FLOWERS_TESTCASES,
    testcases_vname = "MOBILENETV1_A025_96_FFT_INT8_TF_FLOWERS_TESTCASES",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": ["cocotb_mobilenetv1_a025_96_fft_int8_tf_flowers.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_mobilenetv1_a025_96_fft_int8_tf_flowers_binary.elf",
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

# --- Per-op debug runner (manual-only; pinpoints the layer responsible
# for any future activation collapse / divergence).
coralnpu_v2_binary(
    name = "run_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_debug_binary",
    srcs = [
        "run_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_debug.cc",
        ":mobilenetv1_a025_96_fft_int8_cc",
    ],
    hdrs = [":mobilenetv1_a025_96_fft_int8_h"],
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
    visibility = ["//visibility:public"],
    deps = [
        "//sw/opt/litert-micro:conv",
        "//sw/opt/litert-micro:depthwise_conv",
        "//sw/opt/litert-micro:fully_connected",
        "@tflite_micro//tensorflow/lite/micro:micro_framework",
        "@tflite_micro//tensorflow/lite/micro:micro_log",
        "@tflite_micro//tensorflow/lite/micro:micro_profiler",
        "@tflite_micro//tensorflow/lite/micro:op_resolvers",
        "@tflite_micro//tensorflow/lite/micro:system_setup",
    ],
)

MOBILENETV1_A025_96_FFT_INT8_TF_FLOWERS_PER_OP_DEBUG_TESTCASES = [
    "core_mini_rvv_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_debug",
]

# --- Per-op cycle profiler runner (manual-only). Wires a MicroProfiler into
# the interpreter, dumps a per-op cycle/time CSV to tests/.../reports/.
coralnpu_v2_binary(
    name = "run_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_cycles_binary",
    srcs = [
        "run_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_cycles.cc",
        ":mobilenetv1_a025_96_fft_int8_cc",
    ],
    hdrs = [":mobilenetv1_a025_96_fft_int8_h"],
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
    visibility = ["//visibility:public"],
    deps = [
        "//sw/opt/litert-micro:conv",
        "//sw/opt/litert-micro:depthwise_conv",
        "//sw/opt/litert-micro:fully_connected",
        "//sw/utils:utils",
        "@tflite_micro//tensorflow/lite/micro:micro_framework",
        "@tflite_micro//tensorflow/lite/micro:micro_log",
        "@tflite_micro//tensorflow/lite/micro:micro_profiler",
        "@tflite_micro//tensorflow/lite/micro:op_resolvers",
        "@tflite_micro//tensorflow/lite/micro:system_setup",
    ],
)

MOBILENETV1_A025_96_FFT_INT8_TF_FLOWERS_PER_OP_CYCLES_TESTCASES = [
    "core_mini_rvv_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_cycles",
]

cocotb_test_suite(
    name = "cocotb_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_cycles",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = MOBILENETV1_A025_96_FFT_INT8_TF_FLOWERS_PER_OP_CYCLES_TESTCASES,
    testcases_vname = "MOBILENETV1_A025_96_FFT_INT8_TF_FLOWERS_PER_OP_CYCLES_TESTCASES",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": [
            "cocotb_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_cycles.py"
        ],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_cycles_binary.elf",
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

cocotb_test_suite(
    name = "cocotb_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_debug",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = MOBILENETV1_A025_96_FFT_INT8_TF_FLOWERS_PER_OP_DEBUG_TESTCASES,
    testcases_vname = "MOBILENETV1_A025_96_FFT_INT8_TF_FLOWERS_PER_OP_DEBUG_TESTCASES",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": [
            "cocotb_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_debug.py"
        ],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_debug_binary.elf",
            "test_data/input_0.npy",
            "test_data/expected_output_0.npy",
            "test_data/first_conv_expected.npy",
        ],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + glob(["test_data/*.npy"]) + VCS_COMMON_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = ["//hdl/chisel/src/coralnpu:rvv_core_mini_highmem_axi_cc_library_verilog"],
    verilator_model = "//tests/cocotb:rvv_core_mini_highmem_axi_model",
)
