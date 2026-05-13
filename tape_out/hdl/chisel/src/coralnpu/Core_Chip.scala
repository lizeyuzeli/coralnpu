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

// Tape-out top: Core_Chip = CoreChipKernel + chip-side LVDS adapter +
// dmi_jtag (instantiated via SV wrapper).
//
// External pins:
//   - core clock / async reset (active low aresetn)
//   - lvds clock / async reset (lvds_aresetn)
//   - 128-bit LVDS Tx (valid + ready + data) and Rx (valid + data only)
//   - 2x credit-return GPIOs (one per direction)
//   - JTAG pads (tck/tms/trst_n/td_i/td_o, td_oe)
//   - test enable (te), interrupts, halted/fault/wfi status
//   - boot_addr is hardcoded inside; not exposed.

package coralnpu

import chisel3._
import chisel3.util._

// Chisel BlackBox for the SV `dmi_jtag_wrapper` (see
// tape_out/hdl/verilog/dmi_jtag_wrapper.sv).
class DmiJtagWrapperBB(idcodeValue: BigInt = 0x04f5484dL) extends BlackBox(Map(
  "IdcodeValue" -> idcodeValue.toLong
)) with HasBlackBoxResource {
  override val desiredName = "dmi_jtag_wrapper"
  val io = IO(new Bundle {
    val clk_i        = Input(Clock())
    val rst_ni       = Input(Bool())
    val testmode_i   = Input(Bool())
    val test_rst_ni  = Input(Bool())

    val dmi_rst_no   = Output(Bool())

    val dmi_req_address_o = Output(UInt(32.W))
    val dmi_req_op_o      = Output(UInt(2.W))
    val dmi_req_data_o    = Output(UInt(32.W))
    val dmi_req_valid_o   = Output(Bool())
    val dmi_req_ready_i   = Input(Bool())

    val dmi_resp_data_i   = Input(UInt(32.W))
    val dmi_resp_op_i     = Input(UInt(2.W))
    val dmi_resp_ready_o  = Output(Bool())
    val dmi_resp_valid_i  = Input(Bool())

    val tck_i    = Input(Bool())
    val tms_i    = Input(Bool())
    val trst_ni  = Input(Bool())
    val td_i     = Input(Bool())
    val td_o     = Output(Bool())
    val tdo_oe_o = Output(Bool())
  })
  addResource("dmi_jtag_wrapper.sv")
}

class Core_Chip(
    p: Parameters,
    coreModuleName: String,
    bootAddr: BigInt = 0x10000000L,
    // When true, expose the post-RstSync gated core clock and synchronized
    // active-low reset as outputs so that a verification-only wrapper
    // (`Core_Axi_Chip`) can keep its FPGA-side adapter in lockstep with the
    // chip's core domain. MUST be false for the actual tape-out RTL emit so
    // these never appear at the chip pad list.
    exposeVerifyPorts: Boolean = false,
    // When true (default), instantiate `LvdsAsyncFifo` with the rocket-chip
    // `AsyncQueue` (gray-code ptr + multi-stage sync, full-bandwidth);
    // otherwise the SV `AsyncFIFO_RTL` BlackBox (intended for tape-out
    // flows where the dual-port RAM may be swapped for an SRAM IP).
    // See `LvdsAsyncFifo` in LvdsLink.scala.
    useChiselAsyncQueue: Boolean = true,
) extends RawModule {
  override val desiredName = "Core_Chip"

  val io = IO(new Bundle {
    // Core clock/reset
    val aclk = Input(Clock())
    val aresetn = Input(AsyncReset())

    // LVDS clock/reset
    val lvds_clk = Input(Clock())
    val lvds_aresetn = Input(AsyncReset())

    // LVDS PHY pins
    val lvds_tx_valid = Output(Bool())
    val lvds_tx_ready = Input(Bool())
    val lvds_tx_data  = Output(UInt(LvdsLink.kBeatBits.W))
    val lvds_rx_valid = Input(Bool())
    val lvds_rx_data  = Input(UInt(LvdsLink.kBeatBits.W))

    // (Credit-return GPIOs were removed: flow control is now carried
    //  in-band on the LVDS link via M_CREDIT_UPDATE frames; see CreditTracker
    //  in LvdsLink.scala.)

    // JTAG pads
    val tck_i    = Input(Bool())
    val tms_i    = Input(Bool())
    val trst_ni  = Input(Bool())
    val td_i     = Input(Bool())
    val td_o     = Output(Bool())
    val tdo_oe_o = Output(Bool())

    // Test/scan enable
    val te = Input(Bool())

    // Interrupts and status
    val irq          = Input(Bool())
    val timer_irq    = Input(Bool())
    val software_irq = Input(Bool())
    val halted = Output(Bool())
    val fault  = Output(Bool())
    val wfi    = Output(Bool())

    // For verification tops (Core_Axi_Chip): gated clock + sync RstSync
    // release, aligned with fabricMux / LVDS chip adapter. Stripped from
    // the tape-out RTL via `exposeVerifyPorts=false`.
    val core_gated_clk    = if (exposeVerifyPorts) Some(Output(Clock())) else None
    val core_sync_aresetn = if (exposeVerifyPorts) Some(Output(Bool()))  else None
  })
  dontTouch(io)

  // ===========================================================================
  // CoreChipKernel
  // ===========================================================================
  val kernel = Module(new CoreChipKernel(p, coreModuleName, bootAddr))
  kernel.io.aclk    := io.aclk
  kernel.io.aresetn := io.aresetn
  kernel.io.te      := io.te
  io.core_gated_clk    .foreach(_ := kernel.io.gated_clk)
  io.core_sync_aresetn .foreach(_ := kernel.io.adapter_aresetn)
  kernel.io.irq          := io.irq
  kernel.io.timer_irq    := io.timer_irq
  kernel.io.software_irq := io.software_irq
  io.halted := kernel.io.halted
  io.fault  := kernel.io.fault
  io.wfi    := kernel.io.wfi

  // ===========================================================================
  // dmi_jtag wrapper (same clock domain as CSR / fabricMux / DM)
  // ===========================================================================
  val dmi = Module(new DmiJtagWrapperBB)
  dmi.io.clk_i       := kernel.io.gated_clk
  dmi.io.rst_ni      := kernel.io.adapter_aresetn
  dmi.io.testmode_i  := io.te
  dmi.io.test_rst_ni := kernel.io.adapter_aresetn
  dmi.io.tck_i       := io.tck_i
  dmi.io.tms_i       := io.tms_i
  dmi.io.trst_ni     := io.trst_ni
  dmi.io.td_i        := io.td_i
  io.td_o            := dmi.io.td_o
  io.tdo_oe_o        := dmi.io.tdo_oe_o

  // dmi_jtag drives requests; kernel receives.
  kernel.io.dm.req.valid := dmi.io.dmi_req_valid_o
  kernel.io.dm.req.bits.address := dmi.io.dmi_req_address_o
  kernel.io.dm.req.bits.data    := dmi.io.dmi_req_data_o
  val (dmReqOp, _) = DmReqOp.safe(dmi.io.dmi_req_op_o)
  kernel.io.dm.req.bits.op := dmReqOp
  dmi.io.dmi_req_ready_i := kernel.io.dm.req.ready

  dmi.io.dmi_resp_valid_i := kernel.io.dm.rsp.valid
  dmi.io.dmi_resp_data_i  := kernel.io.dm.rsp.bits.data
  dmi.io.dmi_resp_op_i    := kernel.io.dm.rsp.bits.op.asUInt
  kernel.io.dm.rsp.ready  := dmi.io.dmi_resp_ready_o

  // ===========================================================================
  // Chip-side LVDS adapter
  // ===========================================================================
  // Chip-side LVDS adapter MUST use RstSync's gated core clock so fabricSlave
  // is in the same domain as fabricMux / TCM (CoreAxi's AxiSlave lived there
  // too). Raw `aclk` here would CDC-skew fabricSlave vs the kernel.
  val adapter = Module(new LvdsAdapterChip(p, useChiselAsyncQueue))
  adapter.io.core_clk     := kernel.io.gated_clk
  adapter.io.core_aresetn := kernel.io.adapter_aresetn
  adapter.io.lvds_clk     := io.lvds_clk
  adapter.io.lvds_aresetn := io.lvds_aresetn.asBool

  io.lvds_tx_valid       := adapter.io.tx_valid
  io.lvds_tx_data        := adapter.io.tx_data
  adapter.io.tx_ready    := io.lvds_tx_ready
  adapter.io.rx_valid    := io.lvds_rx_valid
  adapter.io.rx_data     := io.lvds_rx_data

  // Plumb adapter <-> kernel
  kernel.io.fabricSlave <> adapter.io.fabricSlave
  kernel.io.ibus        <> adapter.io.ibus
  kernel.io.ebus        <> adapter.io.ebus
}
