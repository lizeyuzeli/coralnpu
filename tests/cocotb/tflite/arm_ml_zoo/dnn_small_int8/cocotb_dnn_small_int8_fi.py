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

Thin shell over //tests/cocotb/fault_injection:fi_campaign. All
campaign / target / fault-model knobs are env-var driven; see the
fault-injection framework README for details.
"""

import cocotb
import numpy as np

from bazel_tools.tools.python.runfiles import runfiles

from coralnpu_test_utils.sim_test_fixture import Fixture

import fi_campaign


_RUNFILES_PREFIX = "coralnpu_hw/tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8/"
_ELF = "run_dnn_small_int8_binary.elf"
_INPUT_NPY = "test_data/input_0.npy"
_EXPECTED_NPY = "test_data/expected_output_0.npy"


@cocotb.test()
async def core_mini_rvv_dnn_small_int8_fi(dut):
    fixture = await Fixture.Create(dut, highmem=True)
    r = runfiles.Create()
    elf_path = r.Rlocation(_RUNFILES_PREFIX + _ELF)
    input_path = r.Rlocation(_RUNFILES_PREFIX + _INPUT_NPY)
    expected_path = r.Rlocation(_RUNFILES_PREFIX + _EXPECTED_NPY)

    input_data = np.load(input_path).astype(np.int8).flatten()
    expected_output = np.load(expected_path).astype(np.int8).flatten()

    await fi_campaign.run_campaign(
        dut, fixture, elf_path, input_data, expected_output,
        model_name="dnn_small_int8")
