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


def tolerate(target: int, tolerance=1.2) -> int:
    return int(target * tolerance)


class GemmTest:
    def __init__(self, m: int, n: int, k: int):
        self.m = m
        self.n = n
        self.k = k
        self.a_size = m * k
        self.bt_size = n * k
        self.c_size = m * n

        r = runfiles.Create()
        self.elf_file = r.Rlocation(
            'coralnpu_hw/tape_out/tests/cocotb/perf_eval/gemm_test_binary.elf')
        self.fixture = None

    async def load_and_populate_input(self, dut):
        self.fixture = await Fixture.Create(dut, highmem=True)
        await self.fixture.load_elf_and_lookup_symbols(
            self.elf_file,
            [
                'impl',
                'run_ref',
                'run_opt',
                'm_dim',
                'n_dim',
                'k_dim',
                'b_is_transposed',
                'a_data',
                'b_data',
                'c_data',
            ]
        )

        rng = np.random.default_rng(2026)
        a = rng.integers(-128, 128, self.a_size, dtype=np.int8)
        b = rng.integers(-128, 128, (self.k, self.n), dtype=np.int8)
        bt = np.ascontiguousarray(b.T).reshape(-1)

        await self.fixture.write_word('m_dim', self.m)
        await self.fixture.write_word('n_dim', self.n)
        await self.fixture.write_word('k_dim', self.k)
        await self.fixture.write_word('b_is_transposed', 1)
        await self.fixture.write('a_data', a)
        await self.fixture.write('b_data', bt)
        await self.fixture.write('c_data', np.zeros(self.c_size, dtype=np.int32))

    async def run(self, func_ptr: str, timeout_cycles):
        await self.fixture.write_ptr('impl', func_ptr)
        await self.fixture.write('c_data', np.zeros(self.c_size, dtype=np.int32))
        cycles = await self.fixture.run_to_halt(timeout_cycles=timeout_cycles)
        outputs = (await self.fixture.read('c_data', self.c_size)).view(np.int32)
        return outputs, cycles

    async def test_opt_only(self, opt_target):
        _, opt_cycles = await self.run('run_opt', tolerate(opt_target, tolerance=4.0))
        print(f'opt_cycles={opt_cycles}', flush=True)


@cocotb.test()
async def test_gemm_i8_32x32x32(dut):
    t = GemmTest(m=32, n=32, k=32)
    await t.load_and_populate_input(dut)
    await t.test_opt_only(opt_target=1_500_000)
