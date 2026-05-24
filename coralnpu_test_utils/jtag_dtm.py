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
# Cocotb JTAG / DMI driver for the CoralNPU CoreChip on-chip dmi_jtag
# (see hdl/verilog/dbg/CoralnpuDmiJtag.sv).  Provides bit-bang TAP
# control and a high-level DMI read/write API that matches the
# CoreMiniAxiInterface.dm_read/dm_write semantics so JTAG tests can be
# written against the same op/data conventions as the existing
# CSR-mailbox debug tests.
#
# The driver assumes CoreAxi exposes these pads:
#   io_tck      : 1-bit TAP clock (bit-banged from Python).
#   io_tms      : 1-bit TAP mode select.
#   io_trst_n   : 1-bit async TAP reset, active-low.
#   io_td_i     : 1-bit TAP data input.
#   io_td_o     : 1-bit TAP data output (sampled by this driver).
#   io_tdo_oe   : 1-bit TAP TDO output-enable (ignored here).
#
# CoreMiniAxiInterface.__init__ initialises the JTAG pads to a safe
# idle (TCK=0, TMS=1, TRST_N=1, TD_I=0).  This driver toggles them
# directly with `Timer`-based delays; it does NOT use the core's
# `io_aclk`-derived clock so it remains independent of the AXI clock
# domain crossing inside dmi_jtag.

from cocotb.triggers import Timer


# JTAG IR opcodes used by the OpenTitan dmi_jtag (IR length = 5).
class JtagIr:
  BYPASS0   = 0x00
  IDCODE    = 0x01
  DTMCSR    = 0x10
  DMIACCESS = 0x11
  BYPASS1   = 0x1f


# DMI op encoding (matches dm::dtm_op_e and DmReqOp in this codebase).
class DmiOp:
  NOP   = 0
  READ  = 1
  WRITE = 2


# DMI response op encoding (matches dm::dtm_op_status_e and DmRspOp).
class DmiResp:
  SUCCESS = 0
  FAILED  = 2
  BUSY    = 3


# Bit-width of the captured DTMCS DR.
DTMCS_WIDTH = 32
# Bit-width of the captured IDCODE DR.
IDCODE_WIDTH = 32


class JtagTap:
  """Low-level JTAG TAP bit-banger.

  Models the standard 16-state JTAG TAP FSM (TestLogicReset, RunTestIdle,
  Select/Capture/Shift/Exit1/Pause/Exit2/Update for DR and IR).  Cycles
  the TCK pin at a configurable period via `cocotb.triggers.Timer`.

  TDO timing inside the OpenTitan dmi_jtag_tap is "updated on negedge
  TCK" -- after we drop TCK from 1 to 0, the new TDO bit is latched.
  This driver therefore samples TDO during the TCK=0 half of each
  shift cycle, drives TDI in the same half, and asserts TCK back high
  to advance the shift register.
  """

  def __init__(self, dut, period_ns: float = 10.0, ir_length: int = 5):
    self.dut = dut
    self.period_ns = period_ns
    # Half-period for symmetric TCK; quarter for the "drive then settle"
    # split inside a single TCK cycle.
    self._t_half = period_ns / 2.0
    self._t_quarter = period_ns / 4.0
    self.ir_length = ir_length

  # ----------------------------------------------------------------
  # Pin-level primitives
  # ----------------------------------------------------------------
  async def reset(self):
    """Issue an async TAP reset and park in RunTestIdle.

    Pulses trst_n low, then walks TMS=1 for 6 cycles to force
    TestLogicReset from any state, then TMS=0 to enter RunTestIdle.
    """
    self.dut.io_trst_n.value = 0
    self.dut.io_tck.value = 0
    self.dut.io_tms.value = 1
    self.dut.io_td_i.value = 0
    await Timer(self.period_ns * 4, unit="ns")
    self.dut.io_trst_n.value = 1
    # 6 TMS=1 cycles guarantees TestLogicReset from any state.
    for _ in range(6):
      await self._cycle(tms=1, tdi=0)
    # One TMS=0 cycle -> RunTestIdle.
    await self._cycle(tms=0, tdi=0)

  async def _cycle(self, tms: int, tdi: int) -> int:
    """One TCK cycle. Returns the TDO bit sampled at TCK=0."""
    # Lower TCK first.  After this negedge, dmi_jtag_tap updates td_o
    # combinationally from the just-shifted DR's LSB.
    self.dut.io_tck.value = 0
    await Timer(self._t_quarter, unit="ns")
    tdo = int(self.dut.io_td_o.value)
    # Now drive TMS/TDI ahead of the upcoming posedge.
    self.dut.io_tms.value = tms
    self.dut.io_td_i.value = tdi
    await Timer(self._t_quarter, unit="ns")
    self.dut.io_tck.value = 1
    await Timer(self._t_half, unit="ns")
    return tdo

  # ----------------------------------------------------------------
  # FSM navigation
  # ----------------------------------------------------------------
  async def _goto_shift_dr(self):
    # Assumes RunTestIdle. RunTestIdle -> SelectDR -> CaptureDR -> ShiftDR.
    await self._cycle(tms=1, tdi=0)  # -> SelectDR
    await self._cycle(tms=0, tdi=0)  # -> CaptureDR
    await self._cycle(tms=0, tdi=0)  # -> ShiftDR  (loads DR with capture)

  async def _goto_shift_ir(self):
    # Assumes RunTestIdle. RunTestIdle -> SelectDR -> SelectIR -> CaptureIR -> ShiftIR.
    await self._cycle(tms=1, tdi=0)  # -> SelectDR
    await self._cycle(tms=1, tdi=0)  # -> SelectIR
    await self._cycle(tms=0, tdi=0)  # -> CaptureIR
    await self._cycle(tms=0, tdi=0)  # -> ShiftIR

  async def _exit_to_idle(self):
    # From Exit1DR/Exit1IR -> Update -> RunTestIdle.
    await self._cycle(tms=1, tdi=0)  # -> UpdateDR/UpdateIR
    await self._cycle(tms=0, tdi=0)  # -> RunTestIdle

  # ----------------------------------------------------------------
  # IR / DR scan primitives
  # ----------------------------------------------------------------
  async def scan_ir(self, value: int):
    """Write `value` into the JTAG IR.  IR length is configurable."""
    await self._goto_shift_ir()
    width = self.ir_length
    for i in range(width):
      bit_in = (value >> i) & 1
      tms = 1 if i == width - 1 else 0
      await self._cycle(tms=tms, tdi=bit_in)  # last bit -> Exit1IR
    await self._exit_to_idle()

  async def scan_dr(self, value: int, width: int) -> int:
    """Shift `value` into the DR (LSB-first) and read out the captured DR.

    Returns the captured DR as an integer, with bit 0 = LSB of captured.
    Round-trips through CaptureDR -> ShiftDR -> Exit1DR -> UpdateDR
    -> RunTestIdle.
    """
    await self._goto_shift_dr()
    out = 0
    for i in range(width):
      bit_in = (value >> i) & 1
      tms = 1 if i == width - 1 else 0
      tdo = await self._cycle(tms=tms, tdi=bit_in)
      out |= tdo << i
    await self._exit_to_idle()
    return out

  async def run_test_idle(self, cycles: int):
    """Spin in RunTestIdle for `cycles` TCK cycles. Used to give the
    on-chip DTM time to complete a DMI transaction across the dmi_cdc
    CDC before scanning the result out.
    """
    for _ in range(cycles):
      await self._cycle(tms=0, tdi=0)

  async def park(self):
    """Park the TAP pads at a quiescent idle (TCK=0, TMS=1, TDI=0).

    Cocotb 2.0 + Verilator can SIGSEGV during `$finish` teardown if any
    inferred clock pin is left high.  Call this at the end of a test
    (or whenever JTAG won't be driven for a long stretch) to ensure
    TCK is low before $finish.
    """
    self.dut.io_tck.value = 0
    self.dut.io_tms.value = 1
    self.dut.io_td_i.value = 0
    # Yield once so the value writes propagate before any cocotb teardown.
    await Timer(self._t_quarter, unit="ns")


class JtagDtm:
  """High-level DMI driver: scans DMI requests through the JTAG TAP.

  Mirrors the `dm_read(addr) -> int` / `dm_write(addr, data) -> rsp`
  API of `CoreMiniAxiInterface` so tests reading/writing debug-module
  registers over JTAG look identical to the existing CSR-mailbox tests.

  DMI register layout (matches dm_pkg::dmi_t in
  hdl/verilog/dbg/CoralnpuDmiJtag.sv):
      bits [1:0]                 = op       (LSB)
      bits [33:2]                = data
      bits [33+abits : 34]       = address  (MSB)
  Total DR width = `abits` + 32 + 2.

  `abits` is read out of the DTMCS register on `init()`; the OpenTitan
  dmi_jtag advertises `abits = NumDmiWordAbits = 16` by default.
  """

  def __init__(self, dut, period_ns: float = 10.0):
    self.tap = JtagTap(dut, period_ns=period_ns)
    self.dut = dut
    self.abits = None  # populated by init()
    self.idle_cycles = 8  # spins between DR scans to drain dmi_cdc.

  async def init(self):
    """Reset the TAP, sanity-check IDCODE and DTMCS, and cache `abits`."""
    await self.tap.reset()
    # After TAP reset, IR defaults to IDCODE; just shift the DR.
    idcode = await self.tap.scan_dr(0, IDCODE_WIDTH)
    if idcode == 0 or (idcode & 1) == 0:
      # JTAG spec: IDCODE LSB must be 1. A 0 here usually means TDO is
      # stuck because reset/TCK setup is broken.
      raise AssertionError(f"JTAG IDCODE looks invalid: {idcode:#010x}")
    self.idcode = idcode
    # Read DTMCS to extract `abits`.
    await self.tap.scan_ir(JtagIr.DTMCSR)
    dtmcs = await self.tap.scan_dr(0, DTMCS_WIDTH)
    self.dtmcs = dtmcs
    abits = (dtmcs >> 4) & 0x3F
    if abits == 0:
      raise AssertionError(f"DTMCS abits is 0; dtmcs={dtmcs:#010x}")
    self.abits = abits
    # Park in DMIACCESS so subsequent DMI operations don't re-scan IR.
    await self.tap.scan_ir(JtagIr.DMIACCESS)

  # ----------------------------------------------------------------
  # DTMCS helpers
  # ----------------------------------------------------------------
  async def read_dtmcs(self) -> int:
    await self.tap.scan_ir(JtagIr.DTMCSR)
    dtmcs = await self.tap.scan_dr(0, DTMCS_WIDTH)
    # Restore DMIACCESS for callers in the middle of a DMI session.
    await self.tap.scan_ir(JtagIr.DMIACCESS)
    return dtmcs

  async def dmireset(self):
    """Pulse dtmcs.dmireset (bit 16) to clear sticky DMI busy errors."""
    await self.tap.scan_ir(JtagIr.DTMCSR)
    await self.tap.scan_dr(1 << 16, DTMCS_WIDTH)
    await self.tap.scan_ir(JtagIr.DMIACCESS)

  async def dmihardreset(self):
    """Pulse dtmcs.dmihardreset (bit 17) to fully reset the DTM/DMI CDC.

    Per the OpenTitan dmi_jtag, this also drops `dmi_rst_no` so the
    downstream debug module sees a reset on its DMI side. Callers
    typically follow this with a small `run_test_idle` to let the CDC
    re-synchronise before issuing more DMI traffic.
    """
    await self.tap.scan_ir(JtagIr.DTMCSR)
    await self.tap.scan_dr(1 << 17, DTMCS_WIDTH)
    await self.tap.scan_ir(JtagIr.DMIACCESS)
    await self.tap.run_test_idle(self.idle_cycles)

  # ----------------------------------------------------------------
  # DMI read / write
  # ----------------------------------------------------------------
  def _pack_dmi(self, addr: int, data: int, op: int) -> int:
    """Pack a {address, data, op} request into a single integer."""
    return ((addr & ((1 << self.abits) - 1)) << (32 + 2)) | \
           ((data & 0xFFFFFFFF) << 2) | (op & 0x3)

  def _unpack_dmi(self, raw: int) -> dict:
    op = raw & 0x3
    data = (raw >> 2) & 0xFFFFFFFF
    addr = (raw >> 34) & ((1 << self.abits) - 1)
    return {"op": op, "data": data, "addr": addr}

  async def _dmi_xact(self, addr: int, data: int, op: int) -> dict:
    """Issue one DMI transaction and capture the response.

    Two scans:
      1. {addr, data, op}: drives the DTM into Read/Write state and
         issues the DMI request to the downstream debug module.
      2. {0, 0, NOP}: captures the result of the prior request into the
         DR.  If the dmi_cdc round-trip hasn't completed yet, the DTM
         FSM returns BUSY (op=3) -- we retry up to `max_retries` times,
         clearing sticky busy via `dmireset` between retries.
    """
    width = 2 + 32 + self.abits
    await self.tap.scan_dr(self._pack_dmi(addr, data, op), width)
    # Drain the CDC.  abits=16 -> ~50 TCK per scan, plus 2-3 dmi_cdc
    # round-trip TCK cycles is more than enough at any reasonable
    # tck:aclk ratio.
    await self.tap.run_test_idle(self.idle_cycles)

    max_retries = 8
    for _ in range(max_retries):
      raw = await self.tap.scan_dr(self._pack_dmi(0, 0, DmiOp.NOP), width)
      rsp = self._unpack_dmi(raw)
      if rsp["op"] != DmiResp.BUSY:
        return rsp
      # Sticky busy: per the spec, before we can read the actual result
      # we have to clear it via dmireset, then re-poll.
      await self.dmireset()
      await self.tap.run_test_idle(self.idle_cycles)
    raise TimeoutError(
        f"DMI transaction stuck in BUSY (addr={addr:#x} op={op})")

  async def dm_read(self, addr: int) -> int:
    """Read a 32-bit debug-module register at DMI address `addr`."""
    rsp = await self._dmi_xact(addr, 0, DmiOp.READ)
    assert rsp["op"] == DmiResp.SUCCESS, \
        f"DMI read of {addr:#x} returned op={rsp['op']}"
    return rsp["data"]

  async def dm_write(self, addr: int, data: int) -> dict:
    """Write `data` to debug-module register at DMI address `addr`."""
    return await self._dmi_xact(addr, data, DmiOp.WRITE)
