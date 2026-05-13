# Copyright 2025 Google LLC
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

load("//rules:coco_tb.bzl", "cocotb_test_suite", "verilator_cocotb_model")
load(
    "//tests/cocotb:build_defs.bzl",
    "VCS_BUILD_ARGS",
    "VCS_DEFINES",
    "VCS_TEST_ARGS",
    "VERILATOR_BUILD_ARGS",
)

package(default_visibility = ["//visibility:public"])

# -----------------------------------------------------------------------------
# Core_Jtag_Chip verification model.
#
# Same idea as //tape_out/tests/cocotb/tutorial/tfmicro:core_axi_chip_model
# but for the JTAG-driven top: the DUT is Core_Jtag_Chip (Core_Chip +
# LvdsAdapterFpga loopback + JTAG pads at the boundary), and the simulator
# needs the dmi_jtag IP from //tape_out/hdl/verilog:dmi_jtag in addition to
# the chisel-emitted SV. The `-Itape_out/hdl/verilog` include path is what
# lets opentitan prim cells resolve their `\`include "prim_assert.sv"\``
# against our local self-contained shim.
# -----------------------------------------------------------------------------
verilator_cocotb_model(
    name = "core_jtag_chip_model",
    cflags = VERILATOR_BUILD_ARGS + ["-Itape_out/hdl/verilog"],
    hdl_toplevel = "Core_Jtag_Chip",
    trace = False,
    verilog_source = "//tape_out/hdl/chisel/src/coralnpu:Core_Jtag_Chip.sv",
    deps = [
        "//hdl/verilog:sram_backdoor",
        "//tape_out/hdl/verilog:dmi_jtag",
    ],
)

CORE_JTAG_CHIP_VCS_VERILOG_SOURCES = [
    "//tape_out/hdl/chisel/src/coralnpu:core_jtag_chip_sim_verilog",
]

# Mirrors //tests/cocotb:CORE_MINI_AXI_DEBUG_TESTCASES, minus the gdbserver
# entry point (pyocd-driven; not retargeted at JTAG transport here).
CORE_JTAG_CHIP_DEBUG_TESTCASES = [
    "core_jtag_chip_debug_dmactive",
    "core_jtag_chip_debug_probe_impl",
    "core_jtag_chip_debug_ndmreset",
    "core_jtag_chip_debug_halt_resume",
    "core_jtag_chip_debug_hartsel",
    "core_jtag_chip_debug_abstract_access_registers",
    "core_jtag_chip_debug_abstract_access_nonexistent_register",
    "core_jtag_chip_debug_single_step",
    "core_jtag_chip_debug_breakpoint",
    "core_jtag_chip_debug_scalar_registers",
]

# Reuse the existing debug ELF binaries built under //tests/cocotb. We do
# not duplicate the .cc/.S sources here -- they are pure software and the
# upstream BUILD already produces noop.elf / registers.elf / etc. We pass
# them as runfiles so the cocotb tests can resolve them via runfiles
# (`coralnpu_hw/tests/cocotb/<name>.elf`).
CORE_JTAG_CHIP_DEBUG_ELFS = [
    "//tests/cocotb:noop.elf",
    "//tests/cocotb:registers.elf",
]

# Diagnostic-only smoke that bypasses JTAG entirely. Used to triage AXI
# loopback issues vs JTAG-driver issues. Manual.
cocotb_test_suite(
    name = "smoke_axi_cocotb",
    simulators = ["verilator"],
    testcases = ["smoke_axi_write_only"],
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],
        "hdl_toplevel": "Core_Jtag_Chip",
        "size": "medium",
        "test_module": ["_smoke_axi.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
        ],
    },
    verilator_model = ":core_jtag_chip_model",
)

cocotb_test_suite(
    name = "core_jtag_chip_debug_cocotb",
    simulators = [
        "verilator",
        "vcs",
    ],
    testcases = CORE_JTAG_CHIP_DEBUG_TESTCASES,
    testcases_vname = "CORE_JTAG_CHIP_DEBUG_TESTCASES",
    tests_kwargs = {
        "waves": False,
        "hdl_toplevel": "Core_Jtag_Chip",
        "seed": "42",
        "size": "enormous",
        "default_testcase_size": "enormous",
        "test_module": ["core_jtag_chip_debug.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//tape_out/coralnpu_test_utils:jtag_dmi_interface",
            "@bazel_tools//tools/python/runfiles",
        ],
        "data": CORE_JTAG_CHIP_DEBUG_ELFS,
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = CORE_JTAG_CHIP_DEBUG_ELFS + [
        "//tests/cocotb:coverage_exclude.cfg",
    ],
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = CORE_JTAG_CHIP_VCS_VERILOG_SOURCES,
    verilator_model = ":core_jtag_chip_model",
)
