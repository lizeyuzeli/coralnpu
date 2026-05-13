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

"""JTAG-DMI debug tests against the Core_Jtag_Chip cocotb top.

Mirror of //tests/cocotb:core_mini_axi_debug.py (the upstream DMI/CSR-based
debug test suite), retargeted at the tape-out chip top:

  - DUT top is `Core_Jtag_Chip` (Core_Chip + LvdsAdapterFpga loopback +
    JTAG pads promoted to the boundary, see
    //tape_out/hdl/chisel/src/coralnpu:Core_Jtag_Chip.scala).
  - DMI access goes through the embedded riscv-dbg dmi_jtag TAP via the
    new `JtagDmiInterface` (//tape_out/coralnpu_test_utils:jtag_dmi_interface),
    not through the legacy `DebugCsrAddr.REQ_*` AXI-CSR shortcut. The
    higher-level `dm_*` helpers from `CoreMiniAxiInterface` are inherited
    unchanged.

The upstream `core_mini_axi_debug_gdbserver` test is intentionally NOT
ported: it relies on `CoreMiniAxiGDBServer` / pyocd which talks to the
target through the same CSR DMI shortcut, and giving pyocd a JTAG transport
through the cocotb DUT is a much larger separate effort. The remaining 10
test cases cover the same DM functionality (probe / dmactive / ndmreset /
halt+resume / hartsel / abstract regs / single-step / breakpoint / scalar
regs) and exercise the dmi_jtag TAP end-to-end.
"""

import cocotb
import random

from cocotb.triggers import ClockCycles
from coralnpu_test_utils.jtag_dmi_interface import JtagDmiInterface
from coralnpu_test_utils.core_mini_axi_interface import DmCmdType, DmRspOp
from bazel_tools.tools.python.runfiles import runfiles


def _runfile(name):
  """Resolve an upstream debug ELF runfile (we do not duplicate the .elf
  binaries here -- the BUILD file references //tests/cocotb:*.elf)."""
  r = runfiles.Create()
  return r.Rlocation(f"coralnpu_hw/tests/cocotb/{name}")


@cocotb.test()
async def core_jtag_chip_debug_dmactive(dut):
  iface = JtagDmiInterface(dut)
  await iface.init()
  await iface.reset()
  await iface.start_clock_and_reset()

  # If we're not active, go ahead and become active.
  dmcontrol = await iface.dm_read(0x10)
  dmactive = dmcontrol & (1 << 0)
  if not dmactive:
    dmcontrol = dmcontrol | 1
    rsp = await iface.dm_write(0x10, dmcontrol)
    assert rsp["op"] == DmRspOp.SUCCESS

  # Set some random value into data0.
  data0_val = random.randint(0, 2**32 - 1)
  rsp = await iface.dm_write(0x4, data0_val)
  assert rsp["op"] == DmRspOp.SUCCESS
  data0_reg = await iface.dm_read(0x4)
  assert (data0_reg == data0_val)

  # Push the debug module into reset.
  dmcontrol = dmcontrol & ~1
  rsp = await iface.dm_write(0x10, dmcontrol)
  assert rsp["op"] == DmRspOp.SUCCESS
  retries = 0
  while True:
    dmcontrol = await iface.dm_read(0x10)
    dmactive = dmcontrol & 1
    if dmactive == 0:
      break
    retries += 1
    if retries == 100:
      assert False, "Failed to clear dmactive"

  # Pull the debug module out of reset.
  dmcontrol = dmcontrol | 1
  rsp = await iface.dm_write(0x10, dmcontrol)
  assert rsp["op"] == DmRspOp.SUCCESS
  retries = 0
  while True:
    dmcontrol = await iface.dm_read(0x10)
    dmactive = dmcontrol & 1
    if dmactive == 1:
      break
    retries += 1
    if retries == 100:
      assert False, "Failed to set dmactive"

  # data0 should be 0 after a debug-module reset.
  data0_reg = await iface.dm_read(0x4)
  assert (data0_reg == 0)


@cocotb.test()
async def core_jtag_chip_debug_probe_impl(dut):
  # See Debug Spec 3.13 Version Detection.
  iface = JtagDmiInterface(dut)
  await iface.init()
  await iface.reset()
  await iface.start_clock_and_reset()

  dmcontrol = await iface.dm_read(0x10)
  dmactive = dmcontrol & (1 << 0)
  ndmreset = dmcontrol & (1 << 1)
  if dmactive == 0 or ndmreset == 1:
    retries = 0
    while True:
      new_dmcontrol = dmcontrol | 1 & ~(1 << 1)
      rsp = await iface.dm_write(0x10, new_dmcontrol)
      assert rsp["op"] == DmRspOp.SUCCESS
      dmcontrol = await iface.dm_read(0x10)
      dmactive = dmcontrol & (1 << 0)
      if dmactive == 1:
        break
      retries += 1
      if retries == 100:
        assert False, "Failed to set dmactive"
  dmstatus = await iface.dm_read(0x11)
  version = dmstatus & (2 << 0)
  # TODO: Don't care about the concrete version, just non-zero.
  assert (version != 0)

  hartinfo = await iface.dm_read(0x12)
  nscratch = (hartinfo >> 20) & 0xF
  assert (nscratch == 2)
  dataaccess = (hartinfo >> 16) & 1
  assert (dataaccess == 0)
  datasize = (hartinfo >> 12) & 0xF
  assert (datasize == 0)
  dataaddr = hartinfo & 0xFFF
  assert (dataaddr == 0x7B4)


@cocotb.test()
async def core_jtag_chip_debug_ndmreset(dut):
  iface = JtagDmiInterface(dut)
  await iface.init()
  await iface.reset()
  await iface.start_clock_and_reset()

  dmcontrol = await iface.dm_read(0x10)
  dmcontrol = dmcontrol | (1 << 1)
  rsp = await iface.dm_write(0x10, dmcontrol)
  assert rsp["op"] == DmRspOp.SUCCESS

  with open(_runfile("noop.elf"), "rb") as f:
    entry_point = await iface.load_elf(f)
    await iface.execute_from(entry_point)
    wait_for_halted_asserted = False
    try:
      await iface.wait_for_halted()
    except:
      wait_for_halted_asserted = True
    assert wait_for_halted_asserted
    dmcontrol = dmcontrol & ~(1 << 1)
    rsp = await iface.dm_write(0x10, dmcontrol)
    assert rsp["op"] == DmRspOp.SUCCESS
    await iface.wait_for_halted()


@cocotb.test()
async def core_jtag_chip_debug_halt_resume(dut):
  iface = JtagDmiInterface(dut)
  await iface.init()
  await iface.reset()
  await iface.start_clock_and_reset()

  with open(_runfile("noop.elf"), "rb") as f:
    entry_point = await iface.load_elf(f)
    await iface.dm_request_halt()
    await iface.execute_from(entry_point)

    await iface.dm_wait_for_halted()
    dcsr = await iface.dm_read_reg(0x7B0)
    dcsr_cause = (dcsr >> 6) & 0b111
    assert (dcsr_cause == 3)

    await ClockCycles(iface.dut.io_aclk, 1000)
    assert iface.dut.io_halted.value == 0

    await iface.dm_request_resume()
    await iface.dm_wait_for_resumed()
    await iface.wait_for_halted()


@cocotb.test()
async def core_jtag_chip_debug_hartsel(dut):
  iface = JtagDmiInterface(dut)
  await iface.init()
  await iface.reset()
  await iface.start_clock_and_reset()

  dmcontrol = await iface.dm_read(0x10)
  dmcontrol = dmcontrol | (0xFFFFF << 6)
  rsp = await iface.dm_write(0x10, dmcontrol)
  assert rsp["op"] == DmRspOp.SUCCESS
  dmcontrol = await iface.dm_read(0x10)
  hartsel = (dmcontrol >> 6) & 0xFFFFF
  assert (hartsel == 1)


@cocotb.test()
async def core_jtag_chip_debug_abstract_access_registers(dut):
  iface = JtagDmiInterface(dut)
  await iface.init()
  await iface.reset()
  await iface.start_clock_and_reset()

  with open(_runfile("noop.elf"), "rb") as f:
    entry_point = await iface.load_elf(f)
    await iface.dm_request_halt()
    await iface.execute_from(entry_point)

    await iface.dm_wait_for_halted()
    dcsr = await iface.dm_read_reg(0x7B0)
    dcsr_cause = (dcsr >> 6) & 0b111
    assert (dcsr_cause == 3)

    mvendorid = await iface.dm_read_reg(0xF11)
    assert (mvendorid == 0x426)

    regs = [
        0x7B2,  # dscratch0
        0x100a, # a0
        0x1030, # f10
    ]
    for reg in regs:
      new_val = random.randint(0, 2**32 - 1)
      await iface.dm_write_reg(reg, new_val)
      await iface.dm_write(0x04, 0)
      readback = await iface.dm_read_reg(reg)
      assert (readback == new_val)


@cocotb.test()
async def core_jtag_chip_debug_abstract_access_nonexistent_register(dut):
  iface = JtagDmiInterface(dut)
  await iface.init()
  await iface.reset()
  await iface.start_clock_and_reset()

  with open(_runfile("noop.elf"), "rb") as f:
    entry_point = await iface.load_elf(f)
    await iface.dm_request_halt()
    await iface.execute_from(entry_point)
    await iface.dm_wait_for_halted()
    await iface.dm_read_reg(0xDEAD, DmRspOp.FAILED)


@cocotb.test()
async def core_jtag_chip_debug_single_step(dut):
  iface = JtagDmiInterface(dut)
  await iface.init()
  await iface.reset()
  await iface.start_clock_and_reset()

  with open(_runfile("noop.elf"), "rb") as f:
    entry_point = await iface.load_elf(f)
    await iface.dm_request_halt()
    await iface.execute_from(entry_point)

    await iface.dm_wait_for_halted()
    dcsr = await iface.dm_read_reg(0x7B0)
    dcsr_cause = (dcsr >> 6) & 0b111
    assert (dcsr_cause == 3)

    dcsr = await iface.dm_read_reg(0x7B0)
    dcsr = dcsr | (1 << 2)
    await iface.dm_write_reg(0x7B0, dcsr)

    dpc = await iface.dm_read_reg(0x7B1)
    for _ in range(0, 3):
      await iface.dm_request_resume()
      await iface.dm_wait_for_halted()
      dcsr = await iface.dm_read_reg(0x7B0)
      dcsr_cause = (dcsr >> 6) & 0b111
      assert (dcsr_cause == 4)
      new_dpc = await iface.dm_read_reg(0x7B1)
      assert (new_dpc == (dpc + 4))
      dpc = new_dpc

    dcsr = await iface.dm_read_reg(0x7B0)
    dcsr = dcsr & ~(1 << 2)
    await iface.dm_write_reg(0x7B0, dcsr)

    await iface.dm_request_resume()
    await iface.wait_for_halted()


@cocotb.test()
async def core_jtag_chip_debug_breakpoint(dut):
  iface = JtagDmiInterface(dut)
  await iface.init()
  await iface.reset()
  await iface.start_clock_and_reset()

  with open(_runfile("noop.elf"), "rb") as f:
    entry_point = await iface.load_elf(f)
    await iface.dm_request_halt()
    await iface.execute_from(entry_point)

    await iface.dm_wait_for_halted()
    dcsr = await iface.dm_read_reg(0x7B0)
    dcsr_cause = (dcsr >> 6) & 0b111
    assert (dcsr_cause == 3)

    main = iface.lookup_symbol(f, "main")

    await iface.dm_write_reg(0x7A0, 0)

    tinfo = await iface.dm_read_reg(0x7A4)
    assert tinfo == 0x01000040

    await iface.dm_write_reg(0x7A1, 0)
    tdata1 = await iface.dm_read_reg(0x7A1)
    assert (tdata1 & 0x60000000) == 0x60000000

    await iface.dm_write_reg(0x7A2, main)
    desired_tdata1 = 0x68001044
    await iface.dm_write_reg(0x7A1, desired_tdata1)
    tdata1 = await iface.dm_read_reg(0x7A1)
    assert tdata1 == desired_tdata1

    await iface.dm_request_resume()

    await iface.dm_wait_for_halted()
    dcsr = await iface.dm_read_reg(0x7B0)
    dcsr_cause = (dcsr >> 6) & 0b111
    assert (dcsr_cause == 2)

    new_dpc = await iface.dm_read_reg(0x7B1)
    assert (new_dpc == main)

    await ClockCycles(iface.dut.io_aclk, 100)
    await iface.dm_wait_for_halted()

    await iface.dm_write_reg(0x7A0, 0)
    await iface.dm_write_reg(0x7A1, 0)
    await iface.dm_write_reg(0x7A2, 0)

    await iface.dm_request_resume()
    await iface.wait_for_halted()


@cocotb.test()
async def core_jtag_chip_debug_scalar_registers(dut):
  iface = JtagDmiInterface(dut)
  await iface.init()
  await iface.reset()
  await iface.start_clock_and_reset()

  with open(_runfile("registers.elf"), "rb") as f:
    entry_point = await iface.load_elf(f)
    await iface.execute_from(entry_point)
    await iface.wait_for_wfi()

    await iface.dm_request_halt()
    await iface.dm_wait_for_halted()

    for i in range(1, 32):
      scalar = await iface.dm_read_reg(i + 0x1000)
      expected_val = (1 << i)
      assert (scalar == expected_val)

    flt = await iface.dm_read_reg(0x1020)
    assert (flt == 0)
    for i in range(1, 32):
      flt = await iface.dm_read_reg(i + 0x1020)
      expected_val = (1 << i)
      assert (flt == expected_val)

    await iface.dm_write_reg(0x101e, 0xdeadbeef)
    await iface.dm_write_reg(0x101f, 0xdeadbeef)
    await iface.dm_request_resume()

    await iface.wait_for_halted()
    assert iface.dut.io_fault.value == 0
