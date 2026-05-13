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

package(default_visibility = ["//visibility:public"])

# -----------------------------------------------------------------------------
# Core_Axi_Chip verification model.
#
# Core_Axi_Chip exposes the same external AXI / status / interrupt interface
# as the legacy CoreAxi, so it plugs into the existing tfmicro cocotb tests
# (which talk to the DUT through axi_master / axi_slave). Internally it is
# Core_Chip + LvdsAdapterFpga in loopback, and Core_Chip instantiates the
# real `dmi_jtag` IP (via the `dmi_jtag_wrapper` BlackBox) using the same
# pulp-platform/riscv-dbg + opentitan prim sources that the FPGA flow uses
# (see //tape_out/hdl/verilog:dmi_jtag). The `-Itape_out/hdl/verilog`
# include path lets verilator resolve the `\`include "prim_assert.sv"\``
# inside opentitan's prim cells against the local self-contained shim.
# -----------------------------------------------------------------------------
verilator_cocotb_model(
    name = "core_axi_chip_model",
    cflags = VERILATOR_BUILD_ARGS + ["-Itape_out/hdl/verilog"],
    hdl_toplevel = "Core_Axi_Chip",
    trace = False,
    verilog_source = "//tape_out/hdl/chisel/src/coralnpu:Core_Axi_Chip.sv",
    deps = [
        "//hdl/verilog:sram_backdoor",
        "//tape_out/hdl/verilog:dmi_jtag",
    ],
)

CORE_AXI_CHIP_VCS_VERILOG_SOURCES = [
    "//tape_out/hdl/chisel/src/coralnpu:core_axi_chip_sim_verilog",
]

CORE_AXI_CHIP_COMMON_VCS_DATA = [
    "//tests/cocotb:coverage_exclude.cfg",
]

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
        "waves": False,
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

coralnpu_v2_binary(
    name = "run_mobilenet_1x1_v1_binary",
    srcs = [
        "mobilenet_v1_1_0_224_quantized_metadata_cc",
        "run_mobilenet_1x1_v1.cc",
    ],
    hdrs = ["mobilenet_v1_1_0_224_quantized_metadata.h"],
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
    name = "mobilenet_v1_1_0_224_quantized_metadata_cc",
    src = "models/mobilenet_v1_1_0_224_quantized_metadata.tflite",
    out = "mobilenet_v1_1_0_224_quantized_metadata.cc",
)

generate_cc_arrays(
    name = "mobilenet_v1_1_0_224_quantized_metadata_h",
    src = "models/mobilenet_v1_1_0_224_quantized_metadata.tflite",
    out = "mobilenet_v1_1_0_224_quantized_metadata.h",
)

cocotb_test_suite(
    name = "cocotb_run_mobilenet_1x1_v1",
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
        "test_module": ["cocotb_run_mobilenet_1x1_v1.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["run_mobilenet_1x1_v1_binary.elf"],
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
    name = "run_mobilenet_1x1_v1_dummy_binary",
    srcs = [
        "mobilenet_v1_0.25_224_int8_dummy_cc",
        "run_mobilenet_1x1_v1_dummy.cc",
    ],
    hdrs = ["mobilenet_v1_0.25_224_int8_dummy.h"],
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
    name = "mobilenet_v1_0.25_224_int8_dummy_cc",
    src = "models/mobilenet_v1_0.25_224_int8_dummy.tflite",
    out = "mobilenet_v1_0.25_224_int8_dummy.cc",
)

generate_cc_arrays(
    name = "mobilenet_v1_0.25_224_int8_dummy_h",
    src = "models/mobilenet_v1_0.25_224_int8_dummy.tflite",
    out = "mobilenet_v1_0.25_224_int8_dummy.h",
)

cocotb_test_suite(
    name = "cocotb_run_mobilenet_1x1_v1_dummy",
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
        "test_module": ["cocotb_run_mobilenet_1x1_v1_dummy.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["run_mobilenet_1x1_v1_dummy_binary.elf"],
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

# =============================================================================
# Core_Axi_Chip variants of the four tflite tests above.
#
# Each target reuses the *same* Python test module and the *same* `*.elf`
# data binary as its `RvvCoreMiniHighmemAxi` counterpart -- only the DUT top
# (`hdl_toplevel`), the verilator model, and the VCS verilog sources change.
# Core_Axi_Chip's external interface is bit-compatible with CoreAxi, so the
# Fixture and CoreMiniAxiInterface bind unchanged. The internal LVDS link is
# put in self-loopback inside Core_Axi_Chip so it is invisible to the test
# bench, and `boot_addr` / `debug` / `dm` / JTAG are politely tied off.
# =============================================================================

cocotb_test_suite(
    name = "cocotb_conv2d_1x1_chip",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = [
        "test_conv2d_1x1_48x48_h2w2",
    ],
    tests_kwargs = {
        "waves": False,
        "hdl_toplevel": "Core_Axi_Chip",
        "size": "enormous",
        # LVDS-bridged sim is much slower; keep per-test Bazel timeout at
        # enormous (medium caps the cocotb runner at 300s wall).
        "default_testcase_size": "enormous",
        "test_module": ["cocotb_conv2d_1x1.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["conv2d_1x1_test.elf"],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + CORE_AXI_CHIP_COMMON_VCS_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = CORE_AXI_CHIP_VCS_VERILOG_SOURCES,
    verilator_model = ":core_axi_chip_model",
)

cocotb_test_suite(
    name = "cocotb_run_mobilenet_1x1_v1_partial_layers_chip",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = RUN_MOBILENET_TEST,
    testcases_vname = "RUN_MOBILENET_TEST",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "Core_Axi_Chip",
        "seed": "42",
        "size": "enormous",
        "default_testcase_size": "enormous",
        "test_module": ["cocotb_run_mobilenet_1x1_v1_partial_layers.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["run_mobilenet_1x1_v1_025_partial_binary.elf"],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + CORE_AXI_CHIP_COMMON_VCS_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = CORE_AXI_CHIP_VCS_VERILOG_SOURCES,
    verilator_model = ":core_axi_chip_model",
)

cocotb_test_suite(
    name = "cocotb_run_mobilenet_1x1_v1_chip",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = RUN_MOBILENET_TEST,
    testcases_vname = "RUN_MOBILENET_TEST",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "Core_Axi_Chip",
        "seed": "42",
        "size": "enormous",
        "default_testcase_size": "enormous",
        "test_module": ["cocotb_run_mobilenet_1x1_v1.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["run_mobilenet_1x1_v1_binary.elf"],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + CORE_AXI_CHIP_COMMON_VCS_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = CORE_AXI_CHIP_VCS_VERILOG_SOURCES,
    verilator_model = ":core_axi_chip_model",
)

# -----------------------------------------------------------------------------
# Core_Axi_Chip variants of the *original* tfmicro tests:
# conv2d / depthwise_conv / fully_connected / logistic / pooling.
#
# Each test's `.cc` source and cocotb `.py` driver are local copies of the
# upstream files under //tests/cocotb/tutorial/tfmicro (no cross-package
# dependency). The python copies bump the `run_to_halt` cycle budgets ~20x
# vs. upstream because the LVDS bridge in Core_Axi_Chip's loopback path
# spends a lot more cycles per memory access than a raw AXI core.
# -----------------------------------------------------------------------------

coralnpu_v2_binary(
    name = "conv2d_test",
    srcs = ["conv2d_test.cc"],
    dtcm_size_kbytes = 1024,  # Highmem
    itcm_size_kbytes = 1024,  # Highmem
    deps = [
        "//sw/opt/litert-micro:conv",
        "@tflite_micro//tensorflow/lite/kernels/internal:reference_base",
    ],
)

cocotb_test_suite(
    name = "cocotb_conv2d_chip",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = [
        "test_conv2d_4x1_h2w2",
        "test_conv2d_9x2",
        "test_conv2d_16x1",
        "test_conv2d_18x1",
        "test_conv2d_37x1_h2w2",
        "test_conv2d_48x48_h2w2",
        "test_conv2d_16x3_s2",
        ("test_conv2d_21x48_h8w8", "enormous"),
        ("test_conv2d_16x16", "enormous"),
        ("test_conv2d_16x16_h2w8", "enormous"),
    ],
    tests_kwargs = {
        "waves": False,
        "hdl_toplevel": "Core_Axi_Chip",
        "size": "enormous",
        "default_testcase_size": "enormous",
        "test_module": ["cocotb_conv2d.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["conv2d_test.elf"],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + CORE_AXI_CHIP_COMMON_VCS_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = CORE_AXI_CHIP_VCS_VERILOG_SOURCES,
    verilator_model = ":core_axi_chip_model",
)

coralnpu_v2_binary(
    name = "depthwise_conv_test",
    srcs = ["depthwise_conv_test.cc"],
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
    deps = [
        "//sw/opt/litert-micro:depthwise_conv",
        "@tflite_micro//tensorflow/lite/kernels/internal:reference_base",
    ],
)

cocotb_test_suite(
    name = "cocotb_depthwise_conv_chip",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = [
        "test_dwconv8to8stride1",
        "test_dwconv8to8stride2",
        ("test_dwconv32to32stride1", "enormous"),
        ("test_dwconv32to32stride2", "enormous"),
        ("test_dwconv64to64stride1", "enormous"),
        ("test_dwconv64to64stride2", "enormous"),
        ("test_dwconv16to32stride2", "enormous"),
    ],
    tests_kwargs = {
        "waves": False,
        "hdl_toplevel": "Core_Axi_Chip",
        "size": "enormous",
        "default_testcase_size": "enormous",
        "test_module": ["cocotb_depthwise_conv.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["depthwise_conv_test.elf"],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + CORE_AXI_CHIP_COMMON_VCS_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = CORE_AXI_CHIP_VCS_VERILOG_SOURCES,
    verilator_model = ":core_axi_chip_model",
)

coralnpu_v2_binary(
    name = "fully_connected_test",
    srcs = ["fully_connected_test.cc"],
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
    deps = [
        "//sw/opt/litert-micro:fully_connected",
        "//sw/utils",
        "@tflite_micro//tensorflow/lite/kernels/internal:reference_base",
    ],
)

cocotb_test_suite(
    name = "cocotb_fully_connected_chip",
    simulators = ["verilator"],
    testcases = [
        "test_fc_16_16",
        "test_fc_64_64",
        "test_fc_256_256",
    ],
    tests_kwargs = {
        "waves": False,
        "hdl_toplevel": "Core_Axi_Chip",
        "size": "enormous",
        "default_testcase_size": "enormous",
        "test_module": ["cocotb_fully_connected.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["fully_connected_test.elf"],
    },
    verilator_model = ":core_axi_chip_model",
)

coralnpu_v2_binary(
    name = "logistic_test",
    srcs = ["logistic_test.cc"],
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
    deps = [
        "//sw/opt/litert-micro:logistic",
        "//sw/utils",
        "@tflite_micro//tensorflow/lite/kernels/internal:reference_base",
    ],
)

cocotb_test_suite(
    name = "cocotb_logistic_chip",
    simulators = ["verilator"],
    testcases = [
        "test_logistic_256",
        "test_logistic_1024",
    ],
    tests_kwargs = {
        "waves": False,
        "hdl_toplevel": "Core_Axi_Chip",
        "size": "enormous",
        "default_testcase_size": "enormous",
        "test_module": ["cocotb_logistic.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["logistic_test.elf"],
    },
    verilator_model = ":core_axi_chip_model",
)

coralnpu_v2_binary(
    name = "pooling_test",
    srcs = ["pooling_test.cc"],
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
    deps = [
        "//sw/opt/litert-micro:pooling",
        "//sw/utils",
        "@tflite_micro//tensorflow/lite/kernels/internal:reference_base",
    ],
)

cocotb_test_suite(
    name = "cocotb_pooling_chip",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = [
        "test_maxpool16stride2",
        "test_maxpool32stride2",
        "test_maxpool64stride2",
    ],
    tests_kwargs = {
        "waves": False,
        "hdl_toplevel": "Core_Axi_Chip",
        "size": "enormous",
        "default_testcase_size": "enormous",
        "test_module": ["cocotb_pooling.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["pooling_test.elf"],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + CORE_AXI_CHIP_COMMON_VCS_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = CORE_AXI_CHIP_VCS_VERILOG_SOURCES,
    verilator_model = ":core_axi_chip_model",
)

coralnpu_v2_binary(
    name = "extdata_diag_test",
    srcs = ["extdata_diag_test.cc"],
    dtcm_size_kbytes = 1024,
    itcm_size_kbytes = 1024,
)

cocotb_test_suite(
    name = "cocotb_extdata_diag_chip",
    simulators = ["verilator"],
    testcases = [
        "test_extdata_scalar_read",
        "test_extdata_vector_read",
        "test_extdata_scratch_round_trip",
    ],
    tests_kwargs = {
        "waves": False,
        "hdl_toplevel": "Core_Axi_Chip",
        "size": "enormous",
        "default_testcase_size": "enormous",
        "test_module": ["cocotb_extdata_diag.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["extdata_diag_test.elf"],
    },
    verilator_model = ":core_axi_chip_model",
)

cocotb_test_suite(
    name = "cocotb_run_mobilenet_1x1_v1_dummy_chip",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = RUN_MOBILENET_TEST,
    testcases_vname = "RUN_MOBILENET_TEST",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "Core_Axi_Chip",
        "seed": "42",
        "size": "enormous",
        "default_testcase_size": "enormous",
        "test_module": ["cocotb_run_mobilenet_1x1_v1_dummy.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": ["run_mobilenet_1x1_v1_dummy_binary.elf"],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + CORE_AXI_CHIP_COMMON_VCS_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = CORE_AXI_CHIP_VCS_VERILOG_SOURCES,
    verilator_model = ":core_axi_chip_model",
)