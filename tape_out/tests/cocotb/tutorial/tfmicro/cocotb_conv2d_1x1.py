# Copyright 2026 Google LLC
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

import cocotb
import numpy as np

from bazel_tools.tools.python.runfiles import runfiles
from coralnpu_test_utils.sim_test_fixture import Fixture


def tolerate(target: int, tolerance = 1.2) -> int:
    return int(target * tolerance)


class Conv2DTest:
    # frozen filter_xy=4, padding=1
    def __init__(self, in_d, out_d, stride=1, out_h=4, out_w=4):
        self.stride = stride
        in_h = out_h * stride
        in_w = out_w * stride
        self.in_shape = np.array([1, in_h, in_w, in_d], dtype=np.uint32)
        self.f_shape = np.array([out_d, 1, 1, in_d], dtype=np.uint32)
        self.bias_shape = np.array([out_d], dtype=np.uint32)
        self.out_shape = np.array([1, out_h, out_w, out_d], dtype=np.uint32)
        self.out_size = int(np.prod(self.out_shape))

        r = runfiles.Create()
        self.elf_file = r.Rlocation(
            'coralnpu_hw/tape_out/tests/cocotb/tutorial/tfmicro/conv2d_1x1_test.elf')
        self.fixture = None

    async def load_and_populate_input(self, dut):
        self.fixture = await Fixture.Create(dut, highmem=True)
        await self.fixture.load_elf_and_lookup_symbols(
            self.elf_file,
            [
                'impl',
                'run_ref',
                'run_opt',
                'stride',
                'filter_shape',
                'filter_data',
                'bias_shape',
                'bias_data',
                'input_shape',
                'input_data',
                'output_shape',
                'output_data',
            ]
        )

        rng = np.random.default_rng()
        filter_data = rng.integers(
            -128, 128, self.f_shape, dtype=np.int8).flatten()
        # acc comes from 16x int16 so bias can't be full range.
        bias_data = rng.integers(
            -100000, 100000, self.out_shape[3], dtype=np.int32)
        input_data = rng.integers(
            -128, 128, self.in_shape, dtype=np.int8).flatten()

        await self.fixture.write_word('stride', self.stride)
        await self.fixture.write('filter_shape', self.f_shape)
        await self.fixture.write('filter_data', filter_data)
        await self.fixture.write('bias_shape', self.bias_shape)
        await self.fixture.write('bias_data', bias_data)
        await self.fixture.write('input_shape', self.in_shape)
        await self.fixture.write('input_data', input_data)
        await self.fixture.write('output_shape', self.out_shape)

    async def run(self, func_ptr: str, timeout_cycles):
        await self.fixture.write_ptr('impl', func_ptr)
        await self.fixture.write(
            'output_data', np.zeros([self.out_size], dtype=np.int8))
        cycles = await self.fixture.run_to_halt(timeout_cycles=timeout_cycles)
        outputs = (await self.fixture.read(
            'output_data', self.out_size)).view(np.int8)
        return outputs, cycles

    async def test(self, ref_target, opt_target):
        ref_output, ref_cycles = await self.run(
            'run_ref', tolerate(ref_target, tolerance = 1.2))
        print(f'ref_cycles={ref_cycles}', flush=True)
        opt_output, opt_cycles = await self.run(
            'run_opt', tolerate(opt_target, tolerance = 5))
        print(f'opt_cycles={opt_cycles}', flush=True)

        assert (opt_output == ref_output).all()
    async def test_opt(self, opt_target):
        opt_output, opt_cycles = await self.run(
            'run_opt', tolerate(opt_target, tolerance = 5))
        print(f'opt_cycles={opt_cycles}', flush=True)


# Tests
# Cycle count targets come from `-c dbg` runs and are significantly
# slower than `-c opt` because DCHECKs are enabled.

@cocotb.test()
async def test_conv2d_1x1_48x48_h2w2(dut):
    t = Conv2DTest(in_d=48, out_d=48, out_h=4, out_w=4)
    await t.load_and_populate_input(dut)
    await t.test_opt(opt_target=275_700)
