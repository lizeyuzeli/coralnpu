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

# Framework-only package for the RVV RTL fault-injection harness.
# Per-model FI cocotb_test_suite targets live next to each model under
# //tests/cocotb/tflite/.../<model>:cocotb_<model>_fi and depend on the
# libraries exported here.

load("//rules:coco_tb.bzl", "verilator_cocotb_model")
load("//tests/cocotb:build_defs.bzl", "VERILATOR_BUILD_ARGS")

# Fault-injection variant of the RVV highmem Verilator model. Same RTL, same
# toplevel; the only difference is the two firtool assertion gates:
#
#   -DSTOP_COND=0           the `$fatal` after a failed assertion is gated off
#   -DASSERT_VERBOSE_COND=0 the `$error` print is gated off too
#
# Both are needed. Verilator stops the simulation on `$error` itself, so gating
# only the stop condition still aborts the run (measured: `Lsu.scala:1339`
# killed 6 of 18 campaign groups with STOP_COND=0 alone).
#
# Why gate them at all: an injected fault legitimately makes the core
# misbehave, and a scalar-core assertion firing IS that misbehaviour. Aborting
# turns the whole campaign group into zero data points -- while the outcome the
# experiment wants (hang / crash / detected / SDC) is decided by io_halted,
# io_fault and the output buffer, none of which need the assertion. Functional
# regressions keep the normal model, where an assertion failure must still stop
# the simulation.
#
# It lives here rather than next to the other models in //tests/cocotb because
# verilator_cocotb_model names its output directory after `hdl_toplevel`, not
# after the target: two same-toplevel models in one package would declare the
# same output file. A separate package gives it its own path.
verilator_cocotb_model(
    name = "rvv_core_mini_highmem_axi_fi_model",
    cflags = VERILATOR_BUILD_ARGS + [
        "-DSTOP_COND=0",
        "-DASSERT_VERBOSE_COND=0",
    ],
    hdl_toplevel = "RvvCoreMiniHighmemAxi",
    trace = False,
    verilog_source = "//hdl/chisel/src/coralnpu:RvvCoreMiniHighmemAxi.sv",
    visibility = ["//visibility:public"],
    deps = [
        "//hdl/verilog:sram_backdoor",
    ],
)

py_library(
    name = "fi_utils",
    srcs = ["fi_utils.py"],
    # Put this directory on PYTHONPATH for consumers so they can
    # `import fi_utils` directly, regardless of which Bazel package they
    # live in. Without this, only same-package callers can find it.
    imports = ["."],
    visibility = ["//visibility:public"],
)

# Static half of the reconciliation. Not a test: it reads a `verilator -E`
# dump of the generated toplevel, which is a file the caller produces, so it
# takes a path rather than a runfile. `bazel run` it as documented in the
# script's docstring.
py_binary(
    name = "fi_seq_audit",
    srcs = ["fi_seq_audit.py"],
    visibility = ["//visibility:public"],
)

# Runtime half of the fault-space reconciliation (fi_seq_audit.py is the
# static half and is not a bazel target: it reads `verilator -E` output, not a
# running simulation). Depends on fi_utils because the audit reuses the
# registry's own leaf flattening -- an audit that enumerated cells differently
# from the injector would diff two things that were never comparable.
py_library(
    name = "fi_audit",
    srcs = ["fi_audit.py"],
    imports = ["."],
    visibility = ["//visibility:public"],
    deps = [":fi_utils"],
)

py_library(
    name = "fi_campaign",
    srcs = ["fi_campaign.py"],
    imports = ["."],
    visibility = ["//visibility:public"],
    deps = [":fi_utils"],
)
