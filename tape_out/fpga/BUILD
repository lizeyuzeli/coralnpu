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

load("@bazel_skylib//rules:common_settings.bzl", "string_list_flag")
load("@lowrisc_opentitan_gh//rules:fusesoc.bzl", "fusesoc_build")
load("//rules:coralnpu_v2.bzl", "coralnpu_v2_binary")
load("//rules:utils.bzl", "cc_embed_data")
load("@internal_check//:status.bzl", internal_exists = "FOLDER_EXISTS")

load("//rules:coralnpu_v2.bzl", "coralnpu_v2_binary")
load("//rules:utils.bzl", "generate_cc_arrays")

package(default_visibility = ["//visibility:public"])

_CLOCK_FREQUENCY_MHZ = "10"
_NEXUS_CLOCK_FREQUENCY_MHZ = "10"

filegroup(
    name = "rtl_files",
    srcs = glob(["**/*.sv"]) + glob(["**/*.core"]) + [
        "//tape_out/fpga/rtl:rtl_files",
    ],
)

coralnpu_v2_binary(
    name = "run_mobilenet_1x1_v1_025_partial_binary",
    srcs = [
        "mobilenet_v1_025_partial_layers_cc",
        "sw/run_mobilenet_1x1_partial_layers.cc",
    ],
    copts = ["-DCLOCK_FREQUENCY_MHZ=" + _CLOCK_FREQUENCY_MHZ],
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
        "//fpga:uart",
    ],
)

generate_cc_arrays(
    name = "mobilenet_v1_025_partial_layers_cc",
    src = "sw/models/mobilenet_v1_025_partial_layers.tflite",
    out = "mobilenet_v1_025_partial_layers.cc",
)

generate_cc_arrays(
    name = "mobilenet_v1_025_partial_layers_h",
    src = "sw/models/mobilenet_v1_025_partial_layers.tflite",
    out = "mobilenet_v1_025_partial_layers.h",
)

CORALNPU_SOC_CORES = [
    "//fpga:coralnpu_soc.core",
    "//fpga:coralnpu_soc_pkg.core",
    "//fpga:racl_pkg.core",
    "@lowrisc_opentitan_gh//hw:check_tool_requirements.core",
]

CORALNPU_SOC_SRCS = [
    ":rtl_files",
    "//fpga:rtl_files",
    "@lowrisc_opentitan_gh//hw/dv/sv:dv_macros",
    "@lowrisc_opentitan_gh//hw/dv:verilator_files",
    "@lowrisc_opentitan_gh//hw:check_tool_requirements.py",
    "@lowrisc_opentitan_gh//hw:lint/tools/verilator/common.vlt",
    "@lowrisc_opentitan_gh//hw:lint/tools/verilator/comportable.vlt",
    "@lowrisc_opentitan_gh//hw:rtl_files",
    "@lowrisc_opentitan_gh//hw:tool_requirements.py",
    "@lowrisc_opentitan_gh//hw:vendor/lint/pulp_riscv_dbg.vlt",
    "@lowrisc_opentitan_gh//hw:vendor/pulp_riscv_dbg/debug_rom/debug_rom.sv",
    "@lowrisc_opentitan_gh//hw:vendor/pulp_riscv_dbg/debug_rom/debug_rom_one_scratch.sv",
    "@lowrisc_opentitan_gh//hw:vendor/pulp_riscv_dbg/src/dm_csrs.sv",
    "@lowrisc_opentitan_gh//hw:vendor/pulp_riscv_dbg/src/dm_mem.sv",
    "@lowrisc_opentitan_gh//hw:vendor/pulp_riscv_dbg/src/dm_pkg.sv",
    "@lowrisc_opentitan_gh//hw:vendor/pulp_riscv_dbg/src/dm_sba.sv",
    "@lowrisc_opentitan_gh//hw:vendor/pulp_riscv_dbg/src/dm_top.sv",
    "@lowrisc_opentitan_gh//hw:vendor/pulp_riscv_dbg/src/dmi_cdc.sv",
    "@lowrisc_opentitan_gh//hw:vendor/pulp_riscv_dbg/src/dmi_jtag.sv",
    "@lowrisc_opentitan_gh//hw:vendor/pulp_riscv_dbg/src/dmi_jtag_tap.sv",
    "@lowrisc_opentitan_gh//hw:verilator_files",
    "//fpga:main.cc",
]

DDR_CORES = [
    "//internal/fpga/ip/ddr4:ddr4.core",
    "//internal/fpga/ip/ddr4_phy:ddr4_phy.core",
] if internal_exists else [
    "//tape_out/fpga/ip/ddr4_stub:ddr4_stub.core",
]
DDR_SRCS = [
    "//internal/fpga/ip/ddr4:elf_files",
    "//internal/fpga/ip/ddr4:rtl_files",
    "//internal/fpga/ip/ddr4_phy:rtl_files",
] if internal_exists else [
    "//tape_out/fpga/ip/ddr4_stub:rtl_files",
]

fusesoc_build(
    name = "build_chip_p1_bitstream",
    srcs = CORALNPU_SOC_SRCS + [
        "pins_p1.xdc",
        "//fpga:vivado_setup_hooks.tcl",
        "//fpga:vivado_hook_write_bitstream_post.tcl",
    ] + DDR_SRCS,
    cores = CORALNPU_SOC_CORES + [
        ":chip_p1.core",
        "//fpga/ip/coralnpu_chisel_subsystem_default:coralnpu_chisel_subsystem_default.core",
    ] + DDR_CORES,
    flags = [
        "--ClockFrequencyMhz=" + _NEXUS_CLOCK_FREQUENCY_MHZ,
    ],
    output_groups = {
        "all": ["com.google.coralnpu_fpga_chip_p1_0.1/"],
    },
    systems = ["com.google.coralnpu:fpga:chip_p1:0.1"],
    target = "synth",
    tags = ["manual"],
)

fusesoc_build(
    name = "build_chip_p1_bitstream_highmem",
    srcs = CORALNPU_SOC_SRCS + [
        "pins_p1.xdc",
        "//fpga:vivado_setup_hooks.tcl",
        "//fpga:vivado_hook_write_bitstream_post.tcl",
    ] + DDR_SRCS,
    cores = CORALNPU_SOC_CORES + [
        ":chip_p1.core",
        "//fpga/ip/i2c_master:i2c_master.core",
        "//fpga/ip/coralnpu_chisel_subsystem_highmem:coralnpu_chisel_subsystem_highmem.core",
    ] + DDR_CORES,
    flags = [
        "--ClockFrequencyMhz=" + _NEXUS_CLOCK_FREQUENCY_MHZ,
    ],
    output_groups = {
        "all": ["com.google.coralnpu_fpga_chip_p1_0.1/"],
    },
    systems = ["com.google.coralnpu:fpga:chip_p1:0.1"],
    target = "synth",
    tags = ["manual"],
)