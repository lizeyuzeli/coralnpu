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
// LinkAdapter (the AXI-over-link bridge) + an on-chip DMI-over-JTAG
// debug TAP (CoralnpuDmiJtag, see hdl/verilog/dbg/CoralnpuDmiJtag.sv).
// Plan ref: §2/§3/§4.
//
// v1 tape-out target:
//   * JTAG pads (tck/tms/trst_n/td_i/td_o + tdo_oe) are now exposed on the
//     CoreChip boundary. The on-chip dmi_jtag drives the kernel's `dm`
//     port directly so the debug module is fully usable from the chip
//     pads -- no `dm_*` ports leave the die.
//   * `debug` (Ibex-style trace bundle) is still NOT exposed on the chip
//     boundary; the kernel output is left dangling so DC will optimise
//     the trace fanout out of the netlist.

package coralnpu

import chisel3._

import coralnpu.link.LinkFrame.kBeatBits
import coralnpu.link.LinkAdapter
import coralnpu.dbg.CoralnpuDmiJtag

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
    // JTAG pads. See plan §2.1.
    val tck_i     = Input(Clock())
    val tms_i     = Input(Bool())
    val trst_ni   = Input(Bool())
    val td_i      = Input(Bool())
    val td_o      = Output(Bool())
    val tdo_oe_o  = Output(Bool())
    // Inter-die Link.
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

  // ------------------------------------------------------------------------
  // On-chip DMI-over-JTAG (plan §3). Drives kernel.dm directly from the
  // JTAG pads; no dm_* signals leave the die.
  // ------------------------------------------------------------------------
  val dmi = Module(new CoralnpuDmiJtag())
  dmi.io.clk_i   := io.clk_core
  dmi.io.rst_ni  := io.aresetn.asBool
  dmi.io.tck_i   := io.tck_i
  dmi.io.tms_i   := io.tms_i
  dmi.io.trst_ni := io.trst_ni
  dmi.io.td_i    := io.td_i
  io.td_o     := dmi.io.td_o
  io.tdo_oe_o := dmi.io.tdo_oe_o

  // Drive kernel.dm.req from dmi (master) -- kernel side is Flipped so
  // valid/bits are inputs, ready is output.
  kernel.io.dm.req.valid          := dmi.io.dmi_req_valid_o
  kernel.io.dm.req.bits.address   := dmi.io.dmi_req_addr_o
  kernel.io.dm.req.bits.data      := dmi.io.dmi_req_data_o
  kernel.io.dm.req.bits.op        := dmi.io.dmi_req_op_o.asTypeOf(DmReqOp())
  dmi.io.dmi_req_ready_i          := kernel.io.dm.req.ready

  // Pipe kernel.dm.rsp back to dmi.
  dmi.io.dmi_resp_valid_i := kernel.io.dm.rsp.valid
  dmi.io.dmi_resp_data_i  := kernel.io.dm.rsp.bits.data
  dmi.io.dmi_resp_resp_i  := kernel.io.dm.rsp.bits.op.asUInt
  kernel.io.dm.rsp.ready  := dmi.io.dmi_resp_ready_o

  // kernel.io.debug is all-Output; let DC prune its fanout (no consumer).
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
