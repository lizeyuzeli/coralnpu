// Copyright 2025 Google LLC
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

// Core_Jtag_Chip: a JTAG-aware verification top, sibling to Core_Axi_Chip.
//
// Why a separate top:
//   - The tape-out chip Core_Chip moved the dmi_jtag TAP from the SoC into
//     the chip. The pre-existing Core_Axi_Chip cocotb tests (tutorial / tfmicro
//     etc.) talk to the core through axi_master/axi_slave only and explicitly
//     hold JTAG idle inside Core_Axi_Chip; promoting JTAG to that top would
//     require driving four extra inputs (tck/tms/trst_n/td_i) in every test,
//     and an undriven trst_n would X-propagate through the TAP under VCS.
//   - This module is the JTAG-driven analogue: same AXI loopback wiring as
//     Core_Axi_Chip (Core_Chip + LvdsAdapterFpga), but with the JTAG pads
//     promoted to the top so cocotb can bit-bang the embedded TAP. The dm
//     (DMI) port is intentionally absent here: the only way to talk to the
//     debug module from this top is through JTAG, mirroring real silicon.
//
// External interface:
//   - aclk / aresetn / axi_master / axi_slave / halted / fault / wfi /
//     irq / timer_irq / software_irq / boot_addr (tied off semantically)
//     -- bit-compatible with Core_Axi_Chip so the existing AXI test fixture
//        can be reused for ELF load / execute_from / wait_for_halted.
//   - tck_i / tms_i / trst_ni / td_i / td_o / tdo_oe_o -- direct passthrough
//     to the embedded Core_Chip dmi_jtag pads.
//
// Internal LVDS link is in self-loopback (chip-side <-> fpga-side) inside
// this module, transparent to the testbench.

package coralnpu

import chisel3._

import bus._

class Core_Jtag_Chip(
    p: Parameters,
    coreModuleName: String,
    bootAddr: BigInt = 0x10000000L,
    // See Core_Axi_Chip.lvdsClkDivLog2: 0 = lvds_clk == aclk, otherwise the
    // LVDS link is exercised on a divided async clock.
    lvdsClkDivLog2: Int = 1,
    useChiselAsyncQueue: Boolean = true,
) extends RawModule {
  require(lvdsClkDivLog2 >= 0, s"lvdsClkDivLog2 must be >= 0, got $lvdsClkDivLog2")
  override val desiredName = "Core_Jtag_Chip"

  val memoryRegions = p.m
  val io = IO(new Bundle {
    val aclk = Input(Clock())
    val aresetn = Input(AsyncReset())
    val axi_slave  = Flipped(new AxiMasterIO(p.axi2AddrBits, p.axi2DataBits, p.axi2IdBits))
    val axi_master = new AxiMasterIO(p.axi2AddrBits, p.axi2DataBits, p.axi2IdBits)
    val halted = Output(Bool())
    val fault  = Output(Bool())
    val wfi    = Output(Bool())
    val irq          = Input(Bool())
    val boot_addr    = Input(UInt(p.fetchAddrBits.W))
    val timer_irq    = Input(Bool())
    val software_irq = Input(Bool())
    val te = Input(Bool())

    // Dead-but-present facades so that the existing CoreMiniAxiInterface
    // testbench __init__ (which unconditionally pokes io.dm.req.valid /
    // io.dm.rsp.ready and reads io.debug.* via cocotb signal lookup) can
    // bind to this top exactly like it does to Core_Axi_Chip. The DMI path
    // here is JTAG; the chip-level io.dm bundle does not affect anything.
    val debug = new DebugIO(p)
    val dm    = new DebugModuleIO(p)

    // JTAG pads (promoted from Core_Chip).
    val tck_i    = Input(Bool())
    val tms_i    = Input(Bool())
    val trst_ni  = Input(Bool())
    val td_i     = Input(Bool())
    val td_o     = Output(Bool())
    val tdo_oe_o = Output(Bool())
  })
  dontTouch(io)

  // ---- LVDS clock divider (verification-only) -------------------------------
  // Mirror of Core_Axi_Chip's divider: lvdsClkDivLog2=0 collapses lvds_clk to
  // aclk, otherwise drive the link on a 2^N divided async clock.
  val (lvdsClk, lvdsAresetnSync) = if (lvdsClkDivLog2 == 0) {
    (io.aclk, io.aresetn.asBool)
  } else {
    val genClk = Wire(Clock())
    val genRstn = Wire(Bool())
    withClockAndReset(io.aclk, (!io.aresetn.asBool).asAsyncReset) {
      val cnt = RegInit(0.U(lvdsClkDivLog2.W))
      cnt := cnt + 1.U
      genClk := cnt(lvdsClkDivLog2 - 1).asClock
    }
    withClockAndReset(genClk, (!io.aresetn.asBool).asAsyncReset) {
      val r0 = RegNext(true.B, false.B)
      val r1 = RegNext(r0,    false.B)
      genRstn := r1
    }
    (genClk, genRstn)
  }

  // ---- Chip-side ------------------------------------------------------------
  val chip = Module(new Core_Chip(
    p, coreModuleName, bootAddr,
    exposeVerifyPorts = true,
    useChiselAsyncQueue = useChiselAsyncQueue,
  ))
  chip.io.aclk    := io.aclk
  chip.io.aresetn := io.aresetn
  chip.io.te      := io.te

  chip.io.lvds_clk     := lvdsClk
  chip.io.lvds_aresetn := lvdsAresetnSync.asAsyncReset

  chip.io.irq          := io.irq
  chip.io.timer_irq    := io.timer_irq
  chip.io.software_irq := io.software_irq
  io.halted := chip.io.halted
  io.fault  := chip.io.fault
  io.wfi    := chip.io.wfi

  // JTAG passthrough.
  chip.io.tck_i   := io.tck_i
  chip.io.tms_i   := io.tms_i
  chip.io.trst_ni := io.trst_ni
  chip.io.td_i    := io.td_i
  io.td_o         := chip.io.td_o
  io.tdo_oe_o     := chip.io.tdo_oe_o

  // ---- FPGA-side ------------------------------------------------------------
  val fpga = Module(new LvdsAdapterFpga(p, useChiselAsyncQueue))
  fpga.io.core_clk     := io.aclk
  fpga.io.core_aresetn := chip.io.core_sync_aresetn.get
  fpga.io.lvds_clk     := lvdsClk
  fpga.io.lvds_aresetn := lvdsAresetnSync

  // LVDS PHY self-loopback (same as Core_Axi_Chip).
  fpga.io.rx_valid     := chip.io.lvds_tx_valid
  fpga.io.rx_data      := chip.io.lvds_tx_data
  chip.io.lvds_tx_ready := true.B

  chip.io.lvds_rx_valid := fpga.io.tx_valid
  chip.io.lvds_rx_data  := fpga.io.tx_data
  fpga.io.tx_ready      := true.B

  // ---- AXI passthrough ------------------------------------------------------
  io.axi_slave  <> fpga.io.axi_slave
  io.axi_master <> fpga.io.axi_master

  // boot_addr: dontTouch'd input (matches Core_Axi_Chip semantics: actual
  // boot address comes from the `bootAddr` Scala param baked into the
  // kernel, not from the runtime port).

  // ---- Polite no-op for unused-but-present ports ---------------------------
  // debug: drive constant zeros (output bundle).
  io.debug := 0.U.asTypeOf(io.debug)
  // dm: never-ready / never-valid facade. The actual DMI path is JTAG.
  io.dm.req.ready := false.B
  io.dm.rsp.valid := false.B
  io.dm.rsp.bits  := 0.U.asTypeOf(io.dm.rsp.bits)
}
