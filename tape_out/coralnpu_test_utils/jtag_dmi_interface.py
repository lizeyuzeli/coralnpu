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

"""JTAG-DMI driver for the Core_Jtag_Chip cocotb verification top.

Background
----------
On the upstream `CoreMiniAxi` / `RvvCoreMiniAxi` cocotb tops the JTAG TAP
lives outside the core, in the SoC test fixture. The cocotb tests therefore
talk DMI through a thin set of CSRs (`DebugCsrAddr.REQ_*` / `RSP_*` /
`STATUS`) -- see `coralnpu_test_utils.core_mini_axi_interface.dm_read` /
`dm_write`.

For tape-out the JTAG TAP (`dmi_jtag` from pulp-platform/riscv-dbg) was
moved INSIDE `Core_Chip`, so the only way to reach the debug module from
outside the chip is via the standard 5-wire JTAG interface
(tck/tms/trst_n/td_i/td_o). The DMI-CSR shortcut no longer exists on the
chip side. This module provides a cocotb-side JTAG TAP bit-banger and a
`JtagDmiInterface` subclass of `CoreMiniAxiInterface` that overrides
`dm_read` / `dm_write` to drive the TAP exactly as a real JTAG dongle would
during a debug session, so all the existing `dm_read_reg` / `dm_request_halt`
/ `dm_*` higher-level helpers (and therefore the upstream debug test cases)
work unmodified.

The TAP is the standard riscv-dbg flavor used by Core_Chip:
  - IR width  : 5
  - IR codes  : BYPASS=0x00, IDCODE=0x01, DTMCS=0x10, DMI=0x11, BYPASS1=0x1F
  - DMI DR    : address[NumDmiWordAbits-1:0] | data[31:0] | op[1:0]
                (riscv-dbg default NumDmiWordAbits=16, matched by the
                 dmi_jtag_wrapper instantiation under
                 tape_out/hdl/verilog/dmi_jtag_wrapper.sv)
  - DTMCS DR  : 32 bits, op[1:0] @[1:0], dmistat[1:0] @[11:10],
                abits[5:0] @[9:4] etc.

References
----------
- RISC-V External Debug Spec v0.13.2, sections 6.1 (JTAG DTM) and 6.1.5
  (DMI access).
- pulp-platform/riscv-dbg `dmi_jtag.sv` / `dmi_jtag_tap.sv` (vendored under
  `tape_out/hdl/verilog/`).
"""

import os

import cocotb
from cocotb.triggers import Timer

from coralnpu_test_utils.core_mini_axi_interface import (
    CoreMiniAxiInterface,
    DmReqOp,
    DmRspOp,
)


# JTAG TAP IR opcodes (5-bit) for the riscv-dbg dmi_jtag TAP.
class JtagIr:
  BYPASS0 = 0x00
  IDCODE  = 0x01
  DTMCS   = 0x10
  DMI     = 0x11
  BYPASS1 = 0x1F


# DMI op encoding (matches riscv-dbg dm_pkg.sv dmi_op_e).
class DmiOp:
  NOP   = 0
  READ  = 1
  WRITE = 2


# DMI response status (the riscv-dbg dmi_jtag returns these in dr[1:0] when
# the DMI DR is captured back into the TAP shift register).
class DmiStatus:
  OK         = 0
  RESERVED   = 1
  FAILED     = 2
  BUSY       = 3


# Core_Jtag_Chip / Core_Chip instantiate dmi_jtag_wrapper with the riscv-dbg
# default NumDmiWordAbits = 16 (see tape_out/hdl/verilog/dmi_jtag_wrapper.sv
# -> dmi_jtag #(IdcodeValue=...) -- relies on the module's default).
DMI_ADDR_WIDTH = 16
DMI_DR_WIDTH   = DMI_ADDR_WIDTH + 32 + 2  # 50

JTAG_IR_WIDTH = 5


class JtagTap:
  """Minimal cocotb JTAG TAP bit-banger for the riscv-dbg dmi_jtag TAP.

  Drives `dut.io_tck_i / io_tms_i / io_trst_ni / io_td_i` and samples
  `dut.io_td_o`. Each TCK period is `tck_ns` (default 10 ns; the chip-side
  TAP is asynchronous to the CPU clock, so this is independent of `aclk`).

  The TAP state machine is the standard IEEE 1149.1 16-state graph. We
  drive TMS/TDI on the falling edge of TCK and sample TDO on the rising
  edge, which is what real JTAG controllers do (and what the dmi_jtag TAP
  expects -- it samples TMS/TDI on tck rising and updates TDO on tck
  falling).
  """

  def __init__(self, dut, tck_ns=10.0):
    self.dut = dut
    self.tck_ns = tck_ns
    # Hold idle until reset_tap() is called.
    self.dut.io_tck_i.value = 0
    self.dut.io_tms_i.value = 0
    self.dut.io_td_i.value = 0
    self.dut.io_trst_ni.value = 0  # asserted (active-low)

  async def _half(self):
    await Timer(self.tck_ns / 2, unit="ns")

  async def _tck_pulse(self, tms, tdi):
    """Drive one full TCK period: setup TMS/TDI on falling edge, sample TDO
    on rising edge. Returns the TDO bit sampled at the rising edge."""
    # Falling edge: drive TMS/TDI.
    self.dut.io_tck_i.value = 0
    self.dut.io_tms_i.value = int(tms) & 1
    self.dut.io_td_i.value = int(tdi) & 1
    await self._half()
    # Rising edge: TAP samples our TMS/TDI; we sample its TDO.
    self.dut.io_tck_i.value = 1
    await self._half()
    return int(self.dut.io_td_o.value) & 1

  async def reset_tap(self, n_trst_cycles=8, n_tms_cycles=8):
    """Bring the TAP to Test-Logic-Reset using both trst_n and TMS=1."""
    # Async reset.
    self.dut.io_trst_ni.value = 0
    self.dut.io_tck_i.value = 0
    self.dut.io_tms_i.value = 1
    self.dut.io_td_i.value = 0
    for _ in range(n_trst_cycles):
      await self._tck_pulse(tms=1, tdi=0)
    self.dut.io_trst_ni.value = 1
    # Belt and suspenders: clock TMS=1 a few more times to land in TLR.
    for _ in range(n_tms_cycles):
      await self._tck_pulse(tms=1, tdi=0)
    # TLR -> Run-Test/Idle.
    await self._tck_pulse(tms=0, tdi=0)

  async def _shift(self, bits, num_bits, exit_to_idle=True):
    """Shift `num_bits` LSB-first out of `bits` while in Shift-IR or
    Shift-DR. Returns captured TDO as an integer (LSB-first, same width).
    Raises TMS=1 on the very last bit to exit to Exit1-{IR,DR}, then
    advances to Update-{IR,DR} -> Run-Test/Idle if `exit_to_idle`."""
    captured = 0
    for i in range(num_bits):
      tdi = (bits >> i) & 1
      last = (i == num_bits - 1)
      tms = 1 if last else 0
      tdo = await self._tck_pulse(tms=tms, tdi=tdi)
      captured |= (tdo & 1) << i
    # Now in Exit1-{IR,DR}.
    if exit_to_idle:
      # Exit1 -> Update.
      await self._tck_pulse(tms=1, tdi=0)
      # Update -> Run-Test/Idle.
      await self._tck_pulse(tms=0, tdi=0)
    return captured

  async def shift_ir(self, ir_value, ir_width=JTAG_IR_WIDTH):
    """RTI -> Shift-IR -> shift `ir_width` bits LSB-first -> RTI."""
    # RTI -> Select-DR-Scan.
    await self._tck_pulse(tms=1, tdi=0)
    # Select-DR -> Select-IR-Scan.
    await self._tck_pulse(tms=1, tdi=0)
    # Select-IR -> Capture-IR.
    await self._tck_pulse(tms=0, tdi=0)
    # Capture-IR -> Shift-IR.
    await self._tck_pulse(tms=0, tdi=0)
    return await self._shift(ir_value, ir_width)

  async def shift_dr(self, dr_value, dr_width):
    """RTI -> Shift-DR -> shift `dr_width` bits LSB-first -> RTI."""
    # RTI -> Select-DR-Scan.
    await self._tck_pulse(tms=1, tdi=0)
    # Select-DR -> Capture-DR.
    await self._tck_pulse(tms=0, tdi=0)
    # Capture-DR -> Shift-DR.
    await self._tck_pulse(tms=0, tdi=0)
    return await self._shift(dr_value, dr_width)

  async def run_idle(self, n=1):
    """Stay in Run-Test/Idle for `n` cycles (TMS=0)."""
    for _ in range(n):
      await self._tck_pulse(tms=0, tdi=0)


class JtagDmiInterface(CoreMiniAxiInterface):
  """CoreMiniAxiInterface variant that talks DMI via JTAG instead of the
  on-chip CSR DMI bridge.

  Use this against `Core_Jtag_Chip`; against `CoreMiniAxi` / `RvvCoreMiniAxi`
  / `Core_Axi_Chip` continue to use plain `CoreMiniAxiInterface` (those
  tops do not expose the dmi_jtag pads at the verification boundary).

  Only `dm_read` / `dm_write` and the small status poll helper they share
  (`_poll_dm_status`) are overridden. All higher-level helpers
  (`dm_read_reg`, `dm_request_halt`, `dm_wait_for_halted`, ...) are
  inherited unchanged: they only depend on `dm_read` / `dm_write`.
  """

  def __init__(self, dut, tck_ns=10.0, **kwargs):
    # Core_Jtag_Chip exposes a dead-but-present `io.dm` bundle (req.ready
    # tied false, rsp.valid tied false) so the upstream
    # CoreMiniAxiInterface.__init__ can probe `io_dm_req_valid` /
    # `io_dm_rsp_ready` exactly like it does on Core_Axi_Chip. The actual
    # DMI path on Core_Jtag_Chip is JTAG; the chip-level `io.dm` bundle is
    # functionally inert.
    super().__init__(dut, **kwargs)

    # Drive JTAG idle right away. self.dut is set by the parent.
    self.dut.io_tck_i.value = 0
    self.dut.io_tms_i.value = 0
    self.dut.io_td_i.value = 0
    self.dut.io_trst_ni.value = 0  # held in reset until reset() runs

    self.tap = JtagTap(self.dut, tck_ns=tck_ns)
    self._dmi_initialized = False

  async def reset(self):
    """Run the standard CoreMiniAxiInterface reset, then bring the JTAG
    TAP out of trst_n in parallel and pump enough TCK cycles to land in
    Run-Test/Idle. After this returns the TAP is ready for IR/DR scans."""
    await super().reset()
    await self.tap.reset_tap()
    self._dmi_initialized = True

  async def start_clock_and_reset(self):
    """Convenience: launch the aclk generator and then issue a second
    `super().reset()` while the clock is ticking. The second reset is what
    actually clocks the LVDS link's RstSync / AsyncFIFO / credit-tracker
    state out of power-on; skipping it leaves the LVDS slave path stuck
    and the AXI slave's wready never asserts (see
    `coralnpu_test_utils.sim_test_fixture.Fixture.Create` for the exact
    same dance against `Core_Axi_Chip`). Always call this immediately
    after `await self.reset()` in tests."""
    cocotb.start_soon(self.clock.start())
    await super().reset()

  # --------------------------------------------------------------------- DMI
  # The riscv-dbg dmi_jtag exposes a single 50-bit DMI DR register layed
  # out as { address[15:0], data[31:0], op[1:0] }. A standard "shift-DR with
  # update" scan does TWO things atomically per scan:
  #
  #   - Capture-DR (start of scan) latches the *previous* completed
  #     transaction's result into the shift register: { addr_q, data_q,
  #     dmi_status_q }. Shifting it out gives the caller back the result of
  #     the prior request.
  #   - Update-DR (end of scan) latches the freshly shifted-in value into
  #     dmi_jtag's address_q/data_q (always, even for op=NOP -- see
  #     dmi_jtag.sv:160-170 where the assigns are unconditional on op kind)
  #     and, if op != NOP, kicks the FSM into Read/Write.
  #
  # Therefore the canonical pattern is "scan-N+1 retrieves the result of
  # request scan-N":
  #
  #   read(addr)  : scan(addr, 0, READ) ; spin idle ; (data, st) = scan(NOP)
  #   write(addr) : scan(addr, x, WRITE); spin idle ; (_,    st) = scan(NOP)
  #
  # The NOP scan must shift in addr=0/data=0 because Update-DR will
  # overwrite address_q/data_q regardless; we choose 0 to avoid confusing
  # any subsequent capture. status==BUSY is sticky and is cleared via
  # dtmcs.dmireset (bit 16).

  async def _dmi_scan(self, addr, data, op):
    """Single 50-bit DMI shift-DR. Returns the captured (data, status) from
    the *previous* completed transaction (Capture-DR), and at end-of-scan
    issues a new transaction with the given (addr, data, op).

    Caller is responsible for sequencing: most transactions need a NOP
    scan after the kicker scan + a few idle TCKs so the FSM can complete
    before Capture-DR latches the result.
    """
    dr_in = ((addr & ((1 << DMI_ADDR_WIDTH) - 1)) << 34) | \
            ((data & 0xFFFFFFFF) << 2) | \
            (op & 0x3)
    captured = await self.tap.shift_dr(dr_in, DMI_DR_WIDTH)
    status = captured & 0x3
    rdata  = (captured >> 2) & 0xFFFFFFFF
    return rdata, status

  async def _clear_dmi_busy(self):
    """Clear sticky DMI error (BUSY/FAILED) via dtmcs.dmireset (bit 16)."""
    await self.tap.shift_ir(JtagIr.DTMCS)
    await self.tap.shift_dr(1 << 16, 32)
    await self.tap.shift_ir(JtagIr.DMI)

  async def _dmi_request(self, addr, data, op, retries=64,
                         idle_tcks=64):
    """Issue a (READ|WRITE) request and return its (data, status). Handles
    BUSY by clearing dtmcs.dmireset and re-driving the same request.
    `op` must not be NOP.

    `idle_tcks` is the number of Run-Test/Idle TCKs inserted between the
    kicker scan and the result-capture scan; the DM needs at least that
    much wall-time to publish dmi_resp_valid before the next scan. The
    riscv-dbg dmi_jtag wraps a CDC handshake (tck domain <-> kernel
    gated_clk domain), so this needs to cover round-trip resync (4-6
    aclk) PLUS the longest abstract-command latency (CSR access via
    program-buffer-less abstract path is ~tens of aclk cycles, but the
    real worst case is that the kernel's gated_clk briefly stalls during
    halt-mode entry/exit). 64 TCKs * 10ns = 640ns ≈ 512 aclk cycles is a
    comfortable upper bound for our setup; on real BUSY we additionally
    keep clearing `dtmcs.dmireset` and doubling the wait."""
    assert self._dmi_initialized, \
        "JtagDmiInterface.reset() must be awaited before any DMI access"
    assert op in (DmiOp.READ, DmiOp.WRITE)
    # Ensure DMI is the selected DR.
    await self.tap.shift_ir(JtagIr.DMI)

    wait = idle_tcks
    for _ in range(retries):
      # Kicker scan: discards prior captured value, kicks new transaction.
      await self._dmi_scan(addr, data, op)
      # Wait for the dmi_jtag FSM to complete the round-trip with the DM.
      await self.tap.run_idle(wait)
      # Result scan: NOP, captures the kicker's outcome.
      rdata, status = await self._dmi_scan(0, 0, DmiOp.NOP)
      if status == DmiStatus.BUSY:
        # Sticky-busy clear, then back off (cap at ~16k tcks ≈ 160us).
        await self._clear_dmi_busy()
        wait = min(wait * 2, 16384)
        continue
      return rdata, status
    raise AssertionError("DMI transaction stuck in BUSY")

  # Set this to True from a test (or via env var DMI_VIA_CSR=1) to bypass the
  # JTAG TAP and route dm_read/dm_write through the upstream CSR-over-AXI
  # path. Useful as a triage knob: if the failing halt-then-CSR-access tests
  # PASS in this mode, the bug is localized to the dmi_jtag <-> DM bridge
  # (CDC + req/rsp arbitration), not to the DM behaviour itself or to the
  # halt/clock-gate logic. The kernel exposes both DMI sources via a
  # round-robin arbiter (`dmReqArbiter` in `CoreChipKernel.scala`), so both
  # paths target the exact same DebugModule instance.
  _force_dmi_via_csr = bool(int(os.environ.get("DMI_VIA_CSR", "0")))

  async def dm_read(self, addr):
    if self._force_dmi_via_csr:
      return await super().dm_read(addr)
    rdata, status = await self._dmi_request(addr, 0, DmiOp.READ)
    assert status == DmiStatus.OK, \
        f"dm_read(addr=0x{addr:x}) returned status={status}"
    return rdata

  async def dm_write(self, addr, data):
    if self._force_dmi_via_csr:
      return await super().dm_write(addr, data)
    _, status = await self._dmi_request(addr, data, DmiOp.WRITE)
    rsp = dict()
    # Match upstream dm_write() return shape: { "data": int, "op": DmRspOp }.
    rsp["data"] = 0
    rsp["op"] = DmRspOp.SUCCESS if status == DmiStatus.OK \
        else (DmRspOp.FAILED if status == DmiStatus.FAILED else DmRspOp.BUSY)
    if status == DmiStatus.FAILED:
      # Clear sticky error so subsequent transactions see a clean slate.
      await self._clear_dmi_busy()
    return rsp


