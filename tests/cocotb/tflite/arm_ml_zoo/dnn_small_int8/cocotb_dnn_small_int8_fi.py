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

"""RTL fault-injection campaign for the DNN-Small INT8 model.

Thin shell over //tests/cocotb/fault_injection:fi_campaign.

This file defines one @cocotb.test per (module, fault_type) GROUP, named
`fi_<module>_<faulttype>` (e.g. fi_storage_seu). Each name is also listed as a
testcase in BUILD, so bazel generates one independent test target per group.
Submitting the suite runs all 12 targets at once and bazel parallelizes them
across cores -- each single-group target is far smaller than the 600-run
monolith, which overran bazel's per-action wall-clock ceiling.

The fi_run_all testcase preserves the whole-matrix behavior (FI_*-env driven)
for ad-hoc single-target runs.
"""

import cocotb
import numpy as np

from bazel_tools.tools.python.runfiles import runfiles

from coralnpu_test_utils.sim_test_fixture import Fixture

import fi_campaign
import fi_utils


_RUNFILES_PREFIX = "coralnpu_hw/tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8/"
_ELF = "run_dnn_small_int8_binary.elf"
_INPUT_NPY = "test_data/input_0.npy"
_EXPECTED_NPY = "test_data/expected_output_0.npy"


async def _load_inputs(dut):
    fixture = await Fixture.Create(dut, highmem=True)
    r = runfiles.Create()
    elf_path = r.Rlocation(_RUNFILES_PREFIX + _ELF)
    input_data = np.load(
        r.Rlocation(_RUNFILES_PREFIX + _INPUT_NPY)).astype(np.int8).flatten()
    expected_output = np.load(
        r.Rlocation(_RUNFILES_PREFIX + _EXPECTED_NPY)).astype(np.int8).flatten()
    return fixture, elf_path, input_data, expected_output


def _make_group_test(module, fault_type):
    """Build a @cocotb.test coroutine pinned to one (module, fault_type)."""
    async def _test(dut):
        fixture, elf_path, input_data, expected_output = await _load_inputs(dut)
        await fi_campaign.run_campaign(
            dut, fixture, elf_path, input_data, expected_output,
            model_name="dnn_small_int8",
            module=module, fault_type=fault_type)
    return _test


# Register the 12 per-group testcases (4 modules x 3 fault types). Each becomes
# its own bazel target via the matching name in DNN_SMALL_INT8_FI_TESTCASES.
# cocotb derives the test name from func.__qualname__ unless name= is given;
# since these share a factory-local qualname, we pass name= explicitly so the
# bazel --testcase filter (fi_<module>_<faulttype>) resolves to exactly one.
for _module in fi_utils.MODULE_NAMES:
    for _ft in fi_utils.FAULT_TYPES:
        _name = f"fi_{_module}_{_ft}"
        globals()[_name] = cocotb.test(name=_name)(
            _make_group_test(_module, _ft))


@cocotb.test()
async def fi_run_all(dut):
    """Whole-matrix run driven by FI_* env vars (single-target convenience)."""
    fixture, elf_path, input_data, expected_output = await _load_inputs(dut)
    await fi_campaign.run_campaign(
        dut, fixture, elf_path, input_data, expected_output,
        model_name="dnn_small_int8")
