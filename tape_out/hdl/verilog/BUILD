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

load("@rules_hdl//verilog:providers.bzl", "verilog_library")

exports_files(
    srcs = [
        "asyn_fifo.sv",
        "dp_sram.sv",
        "dmi_jtag_wrapper.sv",
        "prim_assert.sv",
    ],
    visibility = ["//visibility:public"],
)

verilog_library(
    name = "asyn_fifo",
    srcs = [
        "asyn_fifo.sv",
        "dp_sram.sv",
    ],
    visibility = ["//visibility:public"],
)

# Chisel-side BlackBox wrapper for pulp_riscv_dbg's `dmi_jtag` module.
# Pull in `dmi_jtag` (the actual debug-module-interface JTAG implementation
# from pulp-platform/riscv-dbg, vendored into lowrisc/opentitan) and its
# transitive dependencies via the `:dmi_jtag` library below.
verilog_library(
    name = "dmi_jtag_wrapper",
    srcs = [
        "dmi_jtag_wrapper.sv",
    ],
    deps = [
        ":dmi_jtag",
    ],
    visibility = ["//visibility:public"],
)

# Real `dmi_jtag` plus its full transitive SV dependency tree, locally
# vendored under `vendor/` from pulp-platform/riscv-dbg and lowrisc/opentitan
# (no `@lowrisc_opentitan_gh//` external dependency). Used by both the
# tape-out flow (the synthesized chip ships with this library) and any sim
# (verilator / VCS) that needs to elaborate Core_Chip / Core_Axi_Chip.
#
# Dep chain (instantiations only):
#   dmi_jtag       -> dm_pkg, dmi_jtag_tap, dmi_cdc
#   dmi_jtag_tap   -> dm_pkg, prim_clock_inv
#   dmi_cdc        -> dm_pkg, prim_flop_2sync, prim_clock_mux2,
#                     prim_fifo_async_simple
#   prim_fifo_async_simple -> prim_sync_reqack
#   prim_sync_reqack       -> prim_flop_2sync
#   prim_flop_2sync        -> prim_flop, prim_cdc_rand_delay
#   prim_clock_inv         -> prim_clock_mux2
verilog_library(
    name = "dmi_jtag",
    srcs = [
        "vendor/dm_pkg.sv",
        "vendor/dmi_cdc.sv",
        "vendor/dmi_jtag.sv",
        "vendor/dmi_jtag_tap.sv",
    ],
    deps = [
        ":opentitan_prim_generic",
    ],
    visibility = ["//visibility:public"],
)

# Subset of opentitan prim cells that `dmi_jtag` / `dmi_cdc` /
# `dmi_jtag_tap` transitively pull in. We use the *generic* (sim-friendly)
# prim implementations -- the FPGA flow's `chip_verilator` selects the same
# family. The upstream `\`include "prim_assert.sv"\`` directive in each
# prim cell is resolved against our local self-contained shim
# (`prim_assert.sv` next to this BUILD), which inlines the empty-macro
# variant chosen by upstream `prim_assert.sv` under `\`ifdef VERILATOR\``.
# This keeps the dmi RTL bit-identical to silicon while avoiding the long
# chain of `.svh` macro headers from the upstream tree.
verilog_library(
    name = "opentitan_prim_generic",
    srcs = [
        "prim_assert.sv",
        "vendor/prim_cdc_rand_delay.sv",
        "vendor/prim_clock_inv.sv",
        "vendor/prim_clock_mux2.sv",
        "vendor/prim_fifo_async_simple.sv",
        "vendor/prim_flop.sv",
        "vendor/prim_flop_2sync.sv",
        "vendor/prim_sync_reqack.sv",
    ],
    visibility = ["//visibility:public"],
)
