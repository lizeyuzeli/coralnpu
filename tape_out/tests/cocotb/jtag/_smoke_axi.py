"""Diagnostic-only: pure AXI smoke against Core_Jtag_Chip.

If this PASSES, the wready stall observed in the JTAG-DMI tests is
JTAG-driver-induced (e.g., X-prop from tied-off-then-driven JTAG inputs).
If this FAILS, Core_Jtag_Chip itself has a structural AXI loopback issue
vs Core_Axi_Chip. Not part of the regular suite -- only run manually.
"""

import cocotb
from cocotb.handle import Immediate
from cocotb.triggers import Timer

from coralnpu_test_utils.core_mini_axi_interface import CoreMiniAxiInterface


@cocotb.test()
async def smoke_axi_write_only(dut):
  # Drive JTAG idle the way Core_Axi_Chip statically ties them. No TAP toggle.
  dut.io_tck_i.value = 0
  dut.io_tms_i.value = 0
  dut.io_td_i.value = 0
  dut.io_trst_ni.value = 1   # mirror Core_Axi_Chip's chip.io.trst_ni := true.B

  iface = CoreMiniAxiInterface(dut)
  await iface.init()
  await iface.reset()
  cocotb.start_soon(iface.clock.start())
  # Tutorial Fixture.Create + load_elf_and_lookup_symbols does an *extra*
  # reset AFTER the clock has started, which is what actually clocks the
  # LVDS link's async FIFOs + credit-tracker out of their power-on state.
  # Skipping it leaves the LVDS link with stuck credits and the AXI slave
  # never asserts wready.
  await iface.reset()

  # First AXI write to TCM. If this returns, AXI loopback is functional.
  await iface.write_word(0x100, 0xDEADBEEF)
  rd = await iface.read_word(0x100)
  assert int(rd.view("<u4")[0]) == 0xDEADBEEF, f"got {rd}"
