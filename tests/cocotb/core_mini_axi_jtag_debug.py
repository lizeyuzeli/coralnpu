# Copyright 2026 Li Zeyu <lizeyuzeli000lzy@gmail.com>
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
#
# JTAG-driven debug-module tests for the CoralNPU CoreChip dmi_jtag.
#
# These tests exercise the NEW path:
#   JTAG pads (io_tck/tms/trst_n/td_i/td_o)
#     -> coralnpu_dmi_jtag    (hdl/verilog/dbg/CoralnpuDmiJtag.sv)
#     -> kernel.io.dm         (Decoupled DMI request/response)
#     -> CoralNPURRArbiter    (shared with the AXI-CSR mailbox path)
#     -> DebugModule
#
# The existing CSR-mailbox tests in `core_mini_axi_debug.py` validate
# the DM behaviour itself; this file is here purely to prove that the
# on-chip dmi_jtag + the (newly-written) `dmi_cdc` correctly bridge
# JTAG into the same DM and back.  Same DM operations, different
# transport.
#
# Five scenarios chosen for coverage:
#   * jtag_idcode      : JTAG TAP FSM + IDCODE DR.  No DMI traffic.
#   * jtag_dtmcs       : DTM Control/Status register fields (abits,
#                        version) -- proves dmi_jtag elaborated at
#                        the expected `NumDmiWordAbits=16` param.
#   * jtag_dmactive    : drive dmactive 1->0->1 over JTAG, mirror
#                        of `core_mini_axi_debug_dmactive`.
#   * jtag_probe_impl  : read dmstatus / hartinfo over JTAG; mirror
#                        of `core_mini_axi_debug_probe_impl`.
#   * jtag_halt_resume : full halt + dpc/dcsr access + resume over
#                        JTAG; mirror of `core_mini_axi_debug_halt_resume`.

import cocotb
import random

from cocotb.triggers import ClockCycles
from coralnpu_test_utils.core_mini_axi_interface import (
    CoreMiniAxiInterface,
    DmAddress,
)
from coralnpu_test_utils.jtag_dtm import (
    DmiOp,
    DmiResp,
    JtagDtm,
    JtagIr,
)
from bazel_tools.tools.python.runfiles import runfiles


# Default TCK period in ns.  Picked so TCK ~= aclk/8 (aclk default
# period is 1.25 ns).  This gives the dmi_cdc enough cycles in each
# direction to round-trip while keeping cocotb wall-time short.
JTAG_PERIOD_NS = 10.0


async def _bring_up_dut(dut):
  """Bring up the AXI side (clock + reset) and reset the JTAG TAP."""
  core_mini_axi = CoreMiniAxiInterface(dut)
  await core_mini_axi.init()
  await core_mini_axi.reset()
  cocotb.start_soon(core_mini_axi.clock.start())
  jtag = JtagDtm(dut, period_ns=JTAG_PERIOD_NS)
  await jtag.init()
  return core_mini_axi, jtag


@cocotb.test()
async def core_mini_axi_jtag_idcode(dut):
  """JTAG IDCODE matches the value baked into coralnpu_dmi_jtag.

  The default `IdcodeValue` parameter on the BlackBox is 0x04F5484D.
  This test reproves both the TAP FSM and that the BlackBox parameter
  was forwarded correctly through the Chisel emit.
  """
  core_mini_axi, jtag = await _bring_up_dut(dut)
  expected = 0x04F5484D
  assert jtag.idcode == expected, (
      f"JTAG IDCODE = {jtag.idcode:#010x}, expected {expected:#010x}")
  await jtag.tap.park()
  # Idle a few aclk cycles so all coroutines drain before $finish.
  await ClockCycles(dut.io_aclk, 50)


@cocotb.test()
async def core_mini_axi_jtag_dtmcs(dut):
  """DTMCS reports the configured DTM version and DMI address width.

  Per the OpenTitan dmi_jtag, after capture:
      dtmcs.version[3:0]  = 1   (debug spec 0.13)
      dtmcs.abits[9:4]    = NumDmiWordAbits = 16
      dtmcs.idle[14:12]   = 1
  """
  core_mini_axi, jtag = await _bring_up_dut(dut)
  dtmcs = jtag.dtmcs
  version = dtmcs & 0xF
  abits = (dtmcs >> 4) & 0x3F
  idle = (dtmcs >> 12) & 0x7
  assert version == 1, f"DTMCS version = {version}, expected 1"
  assert abits == 16, f"DTMCS abits = {abits}, expected 16"
  assert idle == 1, f"DTMCS idle = {idle}, expected 1"
  await jtag.tap.park()
  await ClockCycles(dut.io_aclk, 50)


@cocotb.test()
async def core_mini_axi_jtag_dmactive(dut):
  """Toggle DMCONTROL.dmactive over JTAG and verify the debug module
  acknowledges the bit changes through a separate JTAG read.

  Mirrors `core_mini_axi_debug_dmactive` but transport is JTAG, not
  the AXI CSR mailbox.
  """
  core_mini_axi, jtag = await _bring_up_dut(dut)

  # 1. Make sure dmactive is set.
  dmcontrol = await jtag.dm_read(DmAddress.DMCONTROL)
  if not (dmcontrol & 1):
    rsp = await jtag.dm_write(DmAddress.DMCONTROL, dmcontrol | 1)
    assert rsp["op"] == DmiResp.SUCCESS

  # 2. Stash a random value into data0; it must read back the same.
  data0_val = random.randint(0, 2**32 - 1)
  rsp = await jtag.dm_write(DmAddress.DATA0, data0_val)
  assert rsp["op"] == DmiResp.SUCCESS
  data0_reg = await jtag.dm_read(DmAddress.DATA0)
  assert data0_reg == data0_val, \
      f"data0 readback = {data0_reg:#x}, expected {data0_val:#x}"

  # 3. Drive the DM into reset (dmactive=0).
  dmcontrol = dmcontrol & ~1
  rsp = await jtag.dm_write(DmAddress.DMCONTROL, dmcontrol)
  assert rsp["op"] == DmiResp.SUCCESS
  for _ in range(100):
    dmcontrol = await jtag.dm_read(DmAddress.DMCONTROL)
    if (dmcontrol & 1) == 0:
      break
  else:
    assert False, "Failed to clear dmactive over JTAG"

  # 4. Bring it back out of reset, confirm data0 has been cleared.
  rsp = await jtag.dm_write(DmAddress.DMCONTROL, dmcontrol | 1)
  assert rsp["op"] == DmiResp.SUCCESS
  for _ in range(100):
    dmcontrol = await jtag.dm_read(DmAddress.DMCONTROL)
    if dmcontrol & 1:
      break
  else:
    assert False, "Failed to set dmactive over JTAG"
  data0_reg = await jtag.dm_read(DmAddress.DATA0)
  assert data0_reg == 0, f"data0 after DM reset = {data0_reg:#x}, expected 0"
  await jtag.tap.park()
  await ClockCycles(dut.io_aclk, 50)


@cocotb.test()
async def core_mini_axi_jtag_probe_impl(dut):
  """Read dmstatus / hartinfo over JTAG and check spec-defined fields.

  Mirrors `core_mini_axi_debug_probe_impl`.
  """
  core_mini_axi, jtag = await _bring_up_dut(dut)

  # Ensure dmactive=1, ndmreset=0.
  dmcontrol = await jtag.dm_read(DmAddress.DMCONTROL)
  if (dmcontrol & 1) == 0 or (dmcontrol & 2):
    new = (dmcontrol | 1) & ~(1 << 1)
    for _ in range(100):
      rsp = await jtag.dm_write(DmAddress.DMCONTROL, new)
      assert rsp["op"] == DmiResp.SUCCESS
      dmcontrol = await jtag.dm_read(DmAddress.DMCONTROL)
      if dmcontrol & 1:
        break
    else:
      assert False, "Failed to set dmactive over JTAG"

  dmstatus = await jtag.dm_read(DmAddress.DMSTATUS)
  version = dmstatus & 0xF
  assert version != 0, f"dmstatus.version = 0 ({dmstatus:#010x})"

  hartinfo = await jtag.dm_read(DmAddress.HARTINFO)
  nscratch = (hartinfo >> 20) & 0xF
  assert nscratch == 2, f"hartinfo.nscratch = {nscratch}, expected 2"
  dataaccess = (hartinfo >> 16) & 1
  assert dataaccess == 0
  datasize = (hartinfo >> 12) & 0xF
  assert datasize == 0
  dataaddr = hartinfo & 0xFFF
  assert dataaddr == 0x7B4, f"hartinfo.dataaddr = {dataaddr:#x}, expected 0x7B4"
  await jtag.tap.park()
  await ClockCycles(dut.io_aclk, 50)


@cocotb.test()
async def core_mini_axi_jtag_halt_resume(dut):
  """Halt the core via JTAG, read dcsr/dpc, then resume.  Mirrors
  `core_mini_axi_debug_halt_resume`.

  This is the end-to-end test of the JTAG->DM round trip: JTAG writes
  to DMCONTROL.haltreq, the DM signals haltreq to the core, the core
  enters debug mode, and JTAG reads dcsr to confirm the debug cause.
  """
  core_mini_axi, jtag = await _bring_up_dut(dut)

  r = runfiles.Create()
  with open(r.Rlocation("coralnpu_hw/tests/cocotb/noop.elf"), "rb") as f:
    entry_point = await core_mini_axi.load_elf(f)

    # Request halt: set dmcontrol.haltreq | dmactive.
    dmcontrol = await jtag.dm_read(DmAddress.DMCONTROL)
    rsp = await jtag.dm_write(
        DmAddress.DMCONTROL, dmcontrol | 1 | (1 << 31))
    assert rsp["op"] == DmiResp.SUCCESS

    # Kick the core off so it actually enters the halt.
    await core_mini_axi.execute_from(entry_point)

    # Poll dmstatus.allhalted (bit 9).
    for _ in range(200):
      dmstatus = await jtag.dm_read(DmAddress.DMSTATUS)
      if dmstatus & (1 << 9):
        break
    else:
      assert False, "Core did not halt within retry budget"

    # Read dcsr via abstract command: write COMMAND with regno=0x7B0,
    # transfer=1, write=0 (read), aarsize=2 (32-bit).
    # Layout per Debug Spec 0.13: data = {cmdtype[31:24]=0,
    #                                      ctrl[23:0]} where for
    # access_register: ctrl = {aarsize[2:0]<<20, ..., transfer<<17,
    #                          write<<16, regno[15:0]}.
    DCSR = 0x7B0
    command = (0 << 24) | (2 << 20) | (1 << 17) | (0 << 16) | DCSR
    rsp = await jtag.dm_write(DmAddress.COMMAND, command)
    assert rsp["op"] == DmiResp.SUCCESS
    # Wait until abstractcs.busy == 0.
    for _ in range(50):
      abstractcs = await jtag.dm_read(DmAddress.ABSTRACTCS)
      if (abstractcs & (1 << 12)) == 0:
        break
    else:
      assert False, "abstractcs stuck busy after dcsr read"
    dcsr = await jtag.dm_read(DmAddress.DATA0)
    dcsr_cause = (dcsr >> 6) & 0b111
    assert dcsr_cause == 3, (
        f"dcsr.cause = {dcsr_cause}, expected 3 (halt-request); "
        f"dcsr = {dcsr:#010x}")

    # Tick a while -- the core must stay halted.
    await ClockCycles(dut.io_aclk, 1000)
    assert dut.io_halted.value == 0  # halted-via-debug != WFI

    # Resume: clear haltreq, set resumereq.
    dmcontrol = await jtag.dm_read(DmAddress.DMCONTROL)
    dmcontrol = (dmcontrol & ~(1 << 31)) | (1 << 30)
    rsp = await jtag.dm_write(DmAddress.DMCONTROL, dmcontrol)
    assert rsp["op"] == DmiResp.SUCCESS

    # Poll dmstatus.allresumeack (bit 17).
    for _ in range(200):
      dmstatus = await jtag.dm_read(DmAddress.DMSTATUS)
      if dmstatus & (1 << 17):
        break
    else:
      assert False, "Core did not ack resume within retry budget"

    # Program should now run to its tohost halt.
    await core_mini_axi.wait_for_halted()
    await jtag.tap.park()
    await ClockCycles(dut.io_aclk, 50)
