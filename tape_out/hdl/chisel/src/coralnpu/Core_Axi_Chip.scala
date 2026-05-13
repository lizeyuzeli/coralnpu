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

// Core_Axi_Chip: verification top with the SAME external interface as
// CoreAxi (same ports, same widths, same names).
//
// Internally:
//   - Instantiate Core_Chip (the tape-out top, with chip-side LVDS adapter
//     + dmi_jtag wrapper + CoreChipKernel inside).
//   - Instantiate LvdsAdapterFpga which contains AxiSlave / IBus2Axi /
//     DBus2Axi and produces axi_slave / axi_master back to AXI.
//   - Tie the chip TX to FPGA RX and vice versa; lvds_clk = aclk.
//   - "Politely" disable the ports that no longer have an effect on the
//     tape-out core but must still exist for the existing test bench:
//       * boot_addr: input is dontTouch'd; the kernel uses the hardcoded
//         value baked into Core_Chip (matches CoreAxi's effective default
//         used in tape-out).
//       * debug: output bundle driven to all zeros (constant).
//       * dm: req.ready tied false, rsp.valid tied false. Tests that drive
//         dm requests will hang on ready=false; the user picks tests that
//         don't exercise this path.
//       * JTAG: tck=tms=td_i=0, trst_n=1 (held in reset and idle).

package coralnpu

import chisel3._

import bus._

class Core_Axi_Chip(
    p: Parameters,
    coreModuleName: String,
    bootAddr: BigInt = 0x10000000L,
    // Generates an internal `lvds_clk` from `aclk` divided by `2^lvdsClkDivLog2`
    // so the LVDS async FIFOs are exercised on truly asynchronous clocks
    // during cocotb verification. 0 = same clock (legacy behavior).
    lvdsClkDivLog2: Int = 1,
    // Forwarded to `Core_Chip` (and through to `LvdsAdapterChip`) and used
    // for the on-board FPGA-side `LvdsAdapterFpga` instance below.
    useChiselAsyncQueue: Boolean = true,
) extends RawModule {
  require(lvdsClkDivLog2 >= 0, s"lvdsClkDivLog2 must be >= 0, got $lvdsClkDivLog2")
  override val desiredName = "Core_Axi_Chip"

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
    val debug = new DebugIO(p)
    val dm = new DebugModuleIO(p)
    val te = Input(Bool())
  })
  dontTouch(io)

  // ---- LVDS clock divider (verification-only) -------------------------------
  // Drives both the chip-side and FPGA-side adapters of the simulated link
  // so the two AsyncFIFOs (per direction, per side) cross genuinely
  // asynchronous clocks. lvdsClkDivLog2=0 collapses back to aclk.
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
    // Resync aresetn into the divided clock domain (async assert, sync deassert).
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

  // JTAG pins: tied off so the dmi_jtag stays idle. (td_o / tdo_oe_o are
  // outputs of the chip module and are deliberately left dangling.)
  chip.io.tck_i   := false.B
  chip.io.tms_i   := false.B
  chip.io.trst_ni := true.B
  chip.io.td_i    := false.B

  // ---- FPGA-side ------------------------------------------------------------
  val fpga = Module(new LvdsAdapterFpga(p, useChiselAsyncQueue))
  fpga.io.core_clk     := io.aclk
  fpga.io.core_aresetn := chip.io.core_sync_aresetn.get
  fpga.io.lvds_clk     := lvdsClk
  fpga.io.lvds_aresetn := lvdsAresetnSync

  // Cross-connect LVDS PHY. There is no ready going across the wire on the
  // RX direction (the LVDS IP only provides valid+data on Rx); credit GPIOs
  // handle backpressure end-to-end. In simulation we tie both PHY tx_ready
  // signals high (the ready in real silicon is from the local LVDS Tx IP,
  // not from the far side -- so it'd be true unless the PHY itself stalls,
  // which we don't model).
  fpga.io.rx_valid     := chip.io.lvds_tx_valid
  fpga.io.rx_data      := chip.io.lvds_tx_data
  chip.io.lvds_tx_ready := true.B

  chip.io.lvds_rx_valid := fpga.io.tx_valid
  chip.io.lvds_rx_data  := fpga.io.tx_data
  fpga.io.tx_ready      := true.B

  // (Credit-return GPIOs removed: credit flow control is now in-band over
  //  the LVDS data wires via M_CREDIT_UPDATE frames; see CreditTracker.)

  // ---- AXI passthrough ------------------------------------------------------
  io.axi_slave  <> fpga.io.axi_slave
  io.axi_master <> fpga.io.axi_master

  // ---- Polite no-op for unused-but-present ports ----------------------------
  // boot_addr: input is unused; the actual boot address is hardcoded inside
  // CoreChipKernel via the `bootAddr` Scala parameter. dontTouch(io) at the
  // top keeps the port alive for tooling.

  // debug: drive constant zeros.
  io.debug := 0.U.asTypeOf(io.debug)

  // dm: present a "never ready / never valid" facade. Tests that don't
  // touch dm will be unaffected; tests that drive dm.req will see ready=0.
  io.dm.req.ready := false.B
  io.dm.rsp.valid := false.B
  io.dm.rsp.bits  := 0.U.asTypeOf(io.dm.rsp.bits)
}
