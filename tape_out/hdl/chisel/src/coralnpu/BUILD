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

load(
    "@coralnpu_hw//rules:chisel.bzl",
    "chisel_binary",
    "chisel_cc_library",
    "chisel_library",
)
load("@rules_hdl//verilog:providers.bzl", "verilog_library")

package(default_visibility = ["//visibility:public"])

# Tape-out top + verification top + LVDS bridge (chip + FPGA sides) +
# CoreChipKernel + Emit objects.
chisel_library(
    name = "tape_out_coralnpu",
    srcs = [
        "CoreChipKernel.scala",
        "Core_Axi_Chip.scala",
        "Core_Chip.scala",
        "Core_Jtag_Chip.scala",
        "EmitCoreChip.scala",
        "LvdsAdapterChip.scala",
        "LvdsAdapterFpga.scala",
        "LvdsLink.scala",
    ],
    resource_strip_prefix = "tape_out/hdl/verilog",
    resources = [
        "//tape_out/hdl/verilog:asyn_fifo.sv",
        "//tape_out/hdl/verilog:dp_sram.sv",
        "//tape_out/hdl/verilog:dmi_jtag_wrapper.sv",
    ],
    deps = [
        "//hdl/chisel/src/bus",
        "//hdl/chisel/src/common",
        "//hdl/chisel/src/coralnpu",
        "//hdl/chisel/src/coralnpu:RstSync",
        "//hdl/chisel/src/coralnpu:clock_gate",
        "//hdl/chisel/src/coralnpu:coralnpu_base",
        "//hdl/chisel/src/coralnpu:coralnpu_params",
        "//hdl/chisel/src/coralnpu:coralnpu_scalar",
        "//hdl/chisel/src/coralnpu:srams",
        "@chipsalliance_rocket_chip//:asyncqueue",
    ],
)

# Default tape-out core variant: matches RvvCoreMiniHighmemAxi (the
# existing pre-tape-out reference top): RVV+float, 1024KB ITCM/DTCM,
# fetchDataBits=lsuDataBits=128, FetchL0 disabled. The moduleName
# resolves to `CoreMiniHighmem` because itcm/dtcm match the highmem
# preset (see EmitCoreChipCommon.parse).
TAPE_OUT_GEN_FLAGS = [
    "--enableFetchL0=False",
    "--fetchDataBits=128",
    "--lsuDataBits=128",
    "--enableRvv=True",
    "--enableFloat=True",
    "--itcmSizeKBytes=1024",
    "--dtcmSizeKBytes=1024",
    "--moduleName=CoreMini",
]

chisel_cc_library(
    name = "core_chip_cc_library",
    chisel_lib = ":tape_out_coralnpu",
    emit_class = "coralnpu.EmitCore_Chip",
    extra_outs = [
        "VCore_Chip_parameters.h",
        "Core_Chip.zip",
    ],
    gen_flags = TAPE_OUT_GEN_FLAGS,
    module_name = "Core_Chip",
    verilog_file_path = "Core_Chip.sv",
)

chisel_cc_library(
    name = "core_axi_chip_cc_library",
    chisel_lib = ":tape_out_coralnpu",
    emit_class = "coralnpu.EmitCore_Axi_Chip",
    extra_outs = [
        "VCore_Axi_Chip_parameters.h",
        "Core_Axi_Chip.zip",
    ],
    gen_flags = TAPE_OUT_GEN_FLAGS,
    module_name = "Core_Axi_Chip",
    verilog_file_path = "Core_Axi_Chip.sv",
)

chisel_binary(
    name = "emit_core_chip",
    main_class = "coralnpu.EmitCore_Chip",
    deps = [":tape_out_coralnpu"],
)

chisel_binary(
    name = "emit_core_axi_chip",
    main_class = "coralnpu.EmitCore_Axi_Chip",
    deps = [":tape_out_coralnpu"],
)

# JTAG-driven verification top. Same internal AXI loopback as Core_Axi_Chip,
# but with the dmi_jtag pads promoted to the chip top so cocotb tests can
# bit-bang the embedded TAP. Used exclusively by the JTAG-DMI cocotb suite
# under //tape_out/tests/cocotb/jtag.
chisel_cc_library(
    name = "core_jtag_chip_cc_library",
    chisel_lib = ":tape_out_coralnpu",
    emit_class = "coralnpu.EmitCore_Jtag_Chip",
    extra_outs = [
        "VCore_Jtag_Chip_parameters.h",
        "Core_Jtag_Chip.zip",
    ],
    gen_flags = TAPE_OUT_GEN_FLAGS,
    module_name = "Core_Jtag_Chip",
    verilog_file_path = "Core_Jtag_Chip.sv",
)

chisel_binary(
    name = "emit_core_jtag_chip",
    main_class = "coralnpu.EmitCore_Jtag_Chip",
    deps = [":tape_out_coralnpu"],
)

# Convenience verilog_library for simulator-based verification of the
# Core_Axi_Chip top: aggregates the chisel-emitted SV (containing the
# Chisel core, the LVDS adapters and the dmi_jtag_wrapper BlackBox body)
# with the actual `dmi_jtag` IP from pulp-platform/riscv-dbg + opentitan
# prim cells (the same source the FPGA flow consumes via fusesoc), so a
# single label resolves the full sim source set.
verilog_library(
    name = "core_axi_chip_sim_verilog",
    deps = [
        ":core_axi_chip_cc_library_verilog",
        "//tape_out/hdl/verilog:dmi_jtag",
    ],
    visibility = ["//visibility:public"],
)

# Same as core_axi_chip_sim_verilog but for the JTAG-driven top.
verilog_library(
    name = "core_jtag_chip_sim_verilog",
    deps = [
        ":core_jtag_chip_cc_library_verilog",
        "//tape_out/hdl/verilog:dmi_jtag",
    ],
    visibility = ["//visibility:public"],
)
