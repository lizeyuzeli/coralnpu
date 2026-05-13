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

"""Cocotb driver for `extdata_diag_test.cc`.

Pinpoints which leg of the Core_Axi_Chip LVDS master path corrupts data.
Each test loads the same byte pattern into `src_buf` (EXTMEM, written via
slave path), checks the slave-path readback, then runs one kernel and
diff's its DTCM output against the pattern.
"""

import cocotb
import numpy as np

from bazel_tools.tools.python.runfiles import runfiles
from coralnpu_test_utils.sim_test_fixture import Fixture


BUF_SIZE = 256


def _pattern():
    # 0,1,2,...,255 viewed as int8 (so it spans the full int8 range).
    return np.arange(BUF_SIZE, dtype=np.uint8).view(np.int8)


def _dump_diff(label, expected, actual):
    diff = np.where(expected != actual)[0]
    if len(diff) == 0:
        return False
    print(f'{label}: mismatch_count={len(diff)}/{BUF_SIZE}', flush=True)
    print(f'{label}: first_idx={diff[:32].tolist()}', flush=True)
    print(f'{label}: exp={expected[diff[:32]].tolist()}', flush=True)
    print(f'{label}: got={actual[diff[:32]].tolist()}', flush=True)
    return True


class ExtdataDiagTest:
    def __init__(self):
        r = runfiles.Create()
        self.elf_file = r.Rlocation(
            'coralnpu_hw/tape_out/tests/cocotb/tutorial/tfmicro/'
            'extdata_diag_test.elf')
        self.fixture = None

    async def setup(self, dut):
        self.fixture = await Fixture.Create(dut, highmem=True)
        await self.fixture.load_elf_and_lookup_symbols(
            self.elf_file,
            [
                'impl',
                'run_scalar_read',
                'run_vector_read',
                'run_scratch_round_trip',
                'src_buf',
                'dst_scalar',
                'dst_vector',
                'dst_scratch',
                'scratch_buf',
            ],
        )
        pattern = _pattern()
        # Initialize DTCM destinations to a sentinel so we don't accept
        # leftover ELF .data zero values as a "match".
        sentinel = np.full(BUF_SIZE, 0x55, dtype=np.uint8).view(np.int8)
        await self.fixture.write('dst_scalar', sentinel)
        await self.fixture.write('dst_vector', sentinel)
        await self.fixture.write('dst_scratch', sentinel)
        # Pattern goes into EXTMEM via the slave path.
        await self.fixture.write('src_buf', pattern)
        # Slave-path readback sanity check (must always pass; if this
        # fails, the bug is on slave path, not the master EBus path).
        sb = (await self.fixture.read('src_buf', BUF_SIZE)).view(np.int8)
        if _dump_diff('SLAVE_PATH_READBACK', pattern, sb):
            raise AssertionError('slave-path src_buf readback corrupted')
        return pattern

    async def run(self, func_name, timeout=2_000_000):
        await self.fixture.write_ptr('impl', func_name)
        return await self.fixture.run_to_halt(timeout_cycles=timeout)


@cocotb.test()
async def test_extdata_scalar_read(dut):
    """Scalar 1-byte loads from EXTMEM (single-beat AXI reads)."""
    t = ExtdataDiagTest()
    expected = await t.setup(dut)
    cycles = await t.run('run_scalar_read')
    print(f'scalar_read cycles={cycles}', flush=True)
    out = (await t.fixture.read('dst_scalar', BUF_SIZE)).view(np.int8)
    fail = _dump_diff('SCALAR', expected, out)
    assert not fail, 'scalar load via LVDS corrupted'


@cocotb.test()
async def test_extdata_vector_read(dut):
    """16-byte RVV vector loads from EXTMEM (one full beat per load)."""
    t = ExtdataDiagTest()
    expected = await t.setup(dut)
    cycles = await t.run('run_vector_read')
    print(f'vector_read cycles={cycles}', flush=True)
    out = (await t.fixture.read('dst_vector', BUF_SIZE)).view(np.int8)
    fail = _dump_diff('VECTOR', expected, out)
    assert not fail, 'vector load via LVDS corrupted'


@cocotb.test()
async def test_extdata_scratch_round_trip(dut):
    """Vector store to scratch in EXTMEM then vector load it back."""
    t = ExtdataDiagTest()
    expected = await t.setup(dut)
    cycles = await t.run('run_scratch_round_trip')
    print(f'round_trip cycles={cycles}', flush=True)
    # Inspect scratch_buf via slave path (was written by core via master).
    sb = (await t.fixture.read('scratch_buf', BUF_SIZE)).view(np.int8)
    write_side_bad = _dump_diff('SCRATCH_WRITE_SIDE', expected, sb)
    out = (await t.fixture.read('dst_scratch', BUF_SIZE)).view(np.int8)
    read_side_bad = _dump_diff('SCRATCH_ROUND_TRIP', expected, out)
    assert not (write_side_bad or read_side_bad), \
        'extdata write-then-read round trip corrupted'
