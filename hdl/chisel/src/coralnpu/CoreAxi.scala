// Copyright 2024 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// CoreAxi: verification top. IO is byte-compatible with the previous
// monolithic CoreAxi class so the existing cocotb framework
// (`io_aclk`, `io_axi_*`, `io_dm_*`, `io_debug_*`, `io_boot_addr`, ...)
// continues to bind without changes. Internally:
//
//   io.axi_slave/io.axi_master  ── linkFpga ─── (link wires) ─── CoreChip ── kernel
//
// Plan ref: §2/§2.2. v1 simplifications:
//   * No PLL/LinkClk stubs: aclk drives clk_core/link_tx_clk/link_rx_clk
//     directly. Plan-required `PllStub`/`LinkClkStub` BlackBoxes are deferred.
//   * No TX/RX pipeline stubs: link wires are zero-delay.
//   * boot_addr is reduced internally to a 1-bit boot_sel
//     (`==0x10000000`).
//   * dm / debug are NOT exposed by CoreChip (so DC will optimise the
//     unused debug paths out of the tape-out netlist).  CoreChip
//     instantiates an on-chip dmi_jtag driving the kernel's dm port,
//     and CoreAxi now exposes JTAG pads (tck/tms/trst_n/td_i/td_o/
//     tdo_oe) so cocotb tests can reach the debug module through
//     standard JTAG instead of the legacy parallel `dm_*` interface.
//     The pre-existing `dm_*` / `debug_*` IO is retained tied off for
//     byte-compat with the existing cocotb framework binders.

package coralnpu

import chisel3._
import chisel3.util.{ShiftRegister, log2Ceil}

import bus._
import coralnpu.link.LinkAdapter

class CoreAxi(p: Parameters, coreModuleName: String) extends RawModule {
  override val desiredName = coreModuleName + "Axi"
  val memoryRegions = p.m
  val io = IO(new Bundle {
    // AXI
    val aclk = Input(Clock())
    val aresetn = Input(AsyncReset())
    // ITCM, DTCM, CSR
    val axi_slave = Flipped(new AxiMasterIO(p.axi2AddrBits, p.axi2DataBits, p.axi2IdBits))
    val axi_master = new AxiMasterIO(p.axi2AddrBits, p.axi2DataBits, p.axi2IdBits)
    // Core status interrupts
    val halted = Output(Bool())
    val fault = Output(Bool())
    val wfi = Output(Bool())
    val irq = Input(Bool())
    // Boot address (loaded into pcStartReg on reset)
    val boot_addr = Input(UInt(p.fetchAddrBits.W))
    val timer_irq = Input(Bool())
    val software_irq = Input(Bool())
    // Debug data interface
    val debug = new DebugIO(p)
    val dm = new DebugModuleIO(p)
    val te = Input(Bool())
    // JTAG pads (new in v1: on-chip dmi_jtag in CoreChip drives the
    // debug module). Exposed here so cocotb can drive them; existing
    // io.dm/io.debug tie-offs below stay for byte-compat with the
    // previous CoreAxi IO surface.
    val tck       = Input(Clock())
    val tms       = Input(Bool())
    val trst_n    = Input(Bool())
    val td_i      = Input(Bool())
    val td_o      = Output(Bool())
    val tdo_oe    = Output(Bool())
  })
  dontTouch(io)

  // ------------------------------------------------------------------------
  // Local (non-IO) link-modelling knobs. Kept as locals so the constructor
  // signature stays byte-compatible with the rest of the codebase (Core.scala,
  // CoreTlul.scala etc. all instantiate `new CoreAxi(p, name)`). Tune here.
  //   linkClkDiv     -- integer divide ratio for the inter-die link clock
  //                     (link_clk_freq = aclk_freq / linkClkDiv). 1 bypasses
  //                     the divider entirely.
  //   linkPipeStages -- flop stages inserted on each direction of the link
  //                     wires (linkFpga.tx -> chip.rx and chip.tx ->
  //                     linkFpga.rx), clocked by the divided link clock.
  // If you bump these, also revisit `_LINK_OVERHEAD` in the cocotb tfmicro
  // tests (e.g. tests/cocotb/tutorial/tfmicro/cocotb_conv2d.py).
  // ------------------------------------------------------------------------
  private val linkClkDiv: Int = 2
  private val linkPipeStages: Int = 2
  require(linkClkDiv >= 1, s"linkClkDiv must be >= 1 (got $linkClkDiv)")
  require(linkPipeStages >= 0, s"linkPipeStages must be >= 0 (got $linkPipeStages)")

  // boot_addr -> boot_sel (1 -> 0x10000000, 0 -> 0x0).
  val boot_sel = io.boot_addr === 0x10000000.U(p.fetchAddrBits.W)

  // CoreChip (DUT).
  val chip = Module(new CoreChip(p, coreModuleName))
  chip.io.clk_core     := io.aclk
  chip.io.aresetn      := io.aresetn
  chip.io.irq          := io.irq
  chip.io.timer_irq    := io.timer_irq
  chip.io.software_irq := io.software_irq
  chip.io.te           := io.te
  chip.io.boot_sel     := boot_sel
  io.halted := chip.io.halted
  io.fault := chip.io.fault
  io.wfi := chip.io.wfi

  // JTAG pads route straight through to the on-chip dmi_jtag.
  chip.io.tck_i    := io.tck
  chip.io.tms_i    := io.tms
  chip.io.trst_ni  := io.trst_n
  chip.io.td_i     := io.td_i
  io.td_o   := chip.io.td_o
  io.tdo_oe := chip.io.tdo_oe_o

  // dm/debug ports are NOT exposed by CoreChip; on-chip dmi_jtag inside
  // CoreChip drives the debug module from the JTAG pads above. The
  // verif-top dm/debug IO are kept for byte-compat with the previous
  // CoreAxi surface and are tied off here so cocotb's framework still
  // binds. Tests that need debug access should drive the new JTAG pads.
  io.dm.req.ready := false.B
  io.dm.rsp.valid := false.B
  io.dm.rsp.bits := DontCare
  io.debug := DontCare

  // ------------------------------------------------------------------------
  // Link-clock divider (aclk -> linkClk).
  //
  // For linkClkDiv == 1 the divider is bypassed and linkClk == aclk.
  // For linkClkDiv >= 2 we generate a symmetric divided clock by toggling a
  // register every `linkClkDiv/2` aclk cycles. (Odd ratios get a
  // duty-cycle-skewed clock; acceptable for simulation only.)
  // ------------------------------------------------------------------------
  private val aresetHigh = (!io.aresetn.asBool).asAsyncReset
  val linkClk: Clock = if (linkClkDiv <= 1) io.aclk else {
    withClockAndReset(io.aclk, aresetHigh) {
      val halfMinus1 = ((linkClkDiv + 1) / 2 - 1).max(0)
      val cnt = RegInit(0.U(log2Ceil(linkClkDiv + 1).W))
      val clkReg = RegInit(false.B)
      when(cnt === halfMinus1.U) {
        cnt := 0.U
        clkReg := !clkReg
      }.otherwise {
        cnt := cnt + 1.U
      }
      clkReg.asClock
    }
  }

  // FPGA-side LinkAdapter (core domain = aclk; link domain = linkClk).
  val linkFpga = Module(new LinkAdapter(p))
  linkFpga.io.core_clk     := io.aclk
  linkFpga.io.core_rstn    := io.aresetn
  linkFpga.io.link_tx_clk  := linkClk
  linkFpga.io.link_tx_rstn := io.aresetn
  linkFpga.io.link_rx_clk  := linkClk
  linkFpga.io.link_rx_rstn := io.aresetn

  // External AXI <-> linkFpga.
  linkFpga.io.axi_m <> io.axi_slave  // accepts master traffic from external testbench
  linkFpga.io.axi_s <> io.axi_master  // drives master traffic outward

  // CoreChip link clocks/resets share the divided link clock.
  chip.io.link_tx_clk := linkClk
  chip.io.link_tx_rstn := io.aresetn
  chip.io.link_rx_clk := linkClk
  chip.io.link_rx_rstn := io.aresetn

  // Cross-link wires with `linkPipeStages` flop stages in the link-clock
  // domain to model board/package latency. linkFpga.tx is always-ready
  // (link.tx.ready := true.B from the chip side too); .link_rx is a Valid
  // bundle so no back-pressure is required on the pipe.
  withClockAndReset(linkClk, aresetHigh) {
    chip.io.link_rx_valid :=
        ShiftRegister(linkFpga.io.link_tx.valid, linkPipeStages, false.B, true.B)
    chip.io.link_rx_data :=
        ShiftRegister(linkFpga.io.link_tx.bits, linkPipeStages)
    linkFpga.io.link_rx.valid :=
        ShiftRegister(chip.io.link_tx_valid, linkPipeStages, false.B, true.B)
    linkFpga.io.link_rx.bits :=
        ShiftRegister(chip.io.link_tx_data, linkPipeStages)
  }
  linkFpga.io.link_tx.ready := true.B
  chip.io.link_tx_ready := true.B
}
