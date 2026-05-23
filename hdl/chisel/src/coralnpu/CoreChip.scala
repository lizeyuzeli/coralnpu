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
// CoreChip: physical-die top wrapping CoreAxiKernel + the chip-side
// LinkAdapter (the AXI-over-link bridge). Plan ref: §2/§4.
//
// v1 tape-out target: dm and debug are NOT exposed on CoreChip's external
// IO. The kernel's `dm` request side is tied off (no debug-module activity)
// and its `debug` output bundle is consumed locally and left dangling, so
// DC will optimise the entire DebugModule + debug-trace fanout out of the
// netlist. A future revision will introduce on-chip dmi_jtag + JTAG pads
// on this same boundary.

package coralnpu

import chisel3._

import coralnpu.link.LinkFrame.kBeatBits
import coralnpu.link.LinkAdapter

class CoreChip(p: Parameters, coreModuleName: String) extends RawModule {
  override val desiredName = coreModuleName + "Chip"

  val io = IO(new Bundle {
    // Clock / reset
    val clk_core    = Input(Clock())
    val aresetn     = Input(AsyncReset())
    // Status / interrupts
    val halted        = Output(Bool())
    val fault         = Output(Bool())
    val wfi           = Output(Bool())
    val irq           = Input(Bool())
    val timer_irq     = Input(Bool())
    val software_irq  = Input(Bool())
    // Test enable / boot select
    val te        = Input(Bool())
    val boot_sel  = Input(Bool())
    // Inter-die Link.
    // (dm and debug intentionally NOT exposed -- see header comment.)
    val link_tx_clk    = Input(Clock())
    val link_tx_rstn   = Input(AsyncReset())
    val link_tx_valid  = Output(Bool())
    val link_tx_ready  = Input(Bool())
    val link_tx_data   = Output(UInt(kBeatBits.W))
    val link_rx_clk    = Input(Clock())
    val link_rx_rstn   = Input(AsyncReset())
    val link_rx_valid  = Input(Bool())
    val link_rx_data   = Input(UInt(kBeatBits.W))
    val link_err       = Output(Bool())
  })
  dontTouch(io)

  // Inner AXI kernel.
  val kernel = Module(new CoreAxiKernel(p, coreModuleName))
  kernel.io.aclk         := io.clk_core
  kernel.io.aresetn      := io.aresetn
  kernel.io.irq          := io.irq
  kernel.io.timer_irq    := io.timer_irq
  kernel.io.software_irq := io.software_irq
  kernel.io.te           := io.te
  kernel.io.boot_sel     := io.boot_sel
  io.halted := kernel.io.halted
  io.fault := kernel.io.fault
  io.wfi := kernel.io.wfi

  // Tie off kernel.dm: no debug-module requests in v1 tape-out.
  kernel.io.dm.req.valid := false.B
  kernel.io.dm.req.bits := DontCare
  kernel.io.dm.rsp.ready := true.B
  // kernel.io.debug is all-Output; let DC prune its fanout (no consumer).
  // Keep a sink so Chisel doesn't drop the connection during elaboration.
  dontTouch(kernel.io.debug)

  // Chip-side LinkAdapter.
  val link = Module(new LinkAdapter(p))
  link.io.core_clk     := io.clk_core
  link.io.core_rstn    := io.aresetn
  link.io.link_tx_clk  := io.link_tx_clk
  link.io.link_tx_rstn := io.link_tx_rstn
  link.io.link_rx_clk  := io.link_rx_clk
  link.io.link_rx_rstn := io.link_rx_rstn

  // axi_m receives kernel.axi_master (output master); axi_s drives kernel.axi_slave.
  link.io.axi_m <> kernel.io.axi_master
  link.io.axi_s <> kernel.io.axi_slave

  io.link_tx_valid := link.io.link_tx.valid
  io.link_tx_data  := link.io.link_tx.bits
  link.io.link_tx.ready := io.link_tx_ready
  link.io.link_rx.valid := io.link_rx_valid
  link.io.link_rx.bits  := io.link_rx_data
  io.link_err := link.io.link_err_o
}
