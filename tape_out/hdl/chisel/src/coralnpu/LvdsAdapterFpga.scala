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

// FPGA-side LVDS adapter (mirror of LvdsAdapterChip).
//
// On the FPGA we host the existing AxiSlave / IBus2Axi / DBus2Axi modules
// and convert their fabric/ibus/dbus interfaces to/from LVDS frames so the
// chip sees no AXI on the wire. Externally this adapter exposes:
//   - `axi_slave`: AXI master that drives the chip's TCM/CSR (slave path).
//   - `axi_master`: AXI master originated by the chip's core (master path),
//     ready to be hooked up to external memory / interconnect.
//
// The FPGA-side TX direction carries S_RD_REQ / S_WR_REQ / M_IBUS_RSP /
// M_DBUS_RD_RSP / M_DBUS_WR_RSP. The FPGA-side RX direction carries
// S_RD_RSP_OK / S_RD_RSP_ERR / S_WR_RSP / M_IBUS_REQ / M_DBUS_RD_REQ /
// M_DBUS_WR_REQ.

package coralnpu

import chisel3._
import chisel3.util._

import bus._
import common._

// RX dispatcher for the FPGA side. Routes opcodes to the appropriate target.
//   S_RD_RSP_*, S_WR_RSP   -> slave agent
//   M_IBUS_REQ             -> ibus agent
//   M_DBUS_RD_REQ, M_DBUS_WR_REQ -> ebus agent
class LvdsRxDispatcherFpga extends Module {
  val io = IO(new Bundle {
    val pop   = Flipped(Decoupled(UInt(LvdsLink.kBeatBits.W)))
    val slave = Decoupled(new LvdsFrameIn)
    val ibus  = Decoupled(new LvdsFrameIn)
    val ebus  = Decoupled(new LvdsFrameIn)
  })

  val sHdr :: sData :: Nil = Enum(2)
  val state = RegInit(sHdr)
  val hdrReg = Reg(new LvdsHeader)

  val popHdr = io.pop.bits.asTypeOf(new LvdsHeader)
  val curHdr = Mux(state === sHdr, popHdr, hdrReg)

  def routeIsSlave(op: LvdsOpcode.Type): Bool =
    op.isOneOf(LvdsOpcode.S_RD_RSP_OK, LvdsOpcode.S_RD_RSP_ERR, LvdsOpcode.S_WR_RSP)
  def routeIsIbus(op: LvdsOpcode.Type): Bool =
    (op === LvdsOpcode.M_IBUS_REQ)
  def routeIsEbus(op: LvdsOpcode.Type): Bool =
    op.isOneOf(LvdsOpcode.M_DBUS_RD_REQ, LvdsOpcode.M_DBUS_WR_REQ)

  val toSlave = routeIsSlave(curHdr.op)
  val toIbus  = routeIsIbus(curHdr.op)
  val toEbus  = routeIsEbus(curHdr.op)
  val targetReady =
    (toSlave && io.slave.ready) ||
    (toIbus  && io.ibus.ready)  ||
    (toEbus  && io.ebus.ready)

  val popHasData = LvdsLink.hasDataBeat(popHdr.op)

  io.pop.ready := false.B
  io.slave.valid := false.B
  io.ibus.valid  := false.B
  io.ebus.valid  := false.B

  val outFrame = Wire(new LvdsFrameIn)
  outFrame.header := curHdr
  outFrame.data   := io.pop.bits
  io.slave.bits := outFrame
  io.ibus.bits  := outFrame
  io.ebus.bits  := outFrame

  switch(state) {
    is(sHdr) {
      when(io.pop.valid) {
        when(popHasData) {
          io.pop.ready := true.B
          when(io.pop.fire) {
            hdrReg := popHdr
            state := sData
          }
        }.otherwise {
          when(routeIsSlave(popHdr.op)) { io.slave.valid := true.B }
          when(routeIsIbus(popHdr.op))  { io.ibus.valid  := true.B }
          when(routeIsEbus(popHdr.op))  { io.ebus.valid  := true.B }
          io.pop.ready := targetReady
        }
      }
    }
    is(sData) {
      when(io.pop.valid) {
        when(routeIsSlave(hdrReg.op)) { io.slave.valid := true.B }
        when(routeIsIbus(hdrReg.op))  { io.ibus.valid  := true.B }
        when(routeIsEbus(hdrReg.op))  { io.ebus.valid  := true.B }
        io.pop.ready := targetReady
        when(io.pop.fire) {
          state := sHdr
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// FPGA-side slave agent.
//
// Sits between AxiSlave's fabric port (we are the receiver of fabric
// commands) and the LVDS link. Forwards each fabric command as a S_RD_REQ
// or S_WR_REQ frame, waits for the chip's response, and drives readData /
// writeResp back to AxiSlave.
//
// We hold periBusy=true on AxiSlave while a transaction is in flight so
// AxiSlave doesn't issue concurrent commands. When the response arrives:
//   - For reads: drop periBusy on cycle K (AxiSlave's issueRead fires for
//     this beat); drive fabric.readData.valid+bits on cycle K+1 (matching
//     TCM 1-cycle latency expectation in AxiSlave).
//   - For writes: drop periBusy AND drive fabric.writeResp this cycle so
//     AxiSlave's writeData.fire latches the resp.
// ---------------------------------------------------------------------------
class LvdsFpgaSlaveAgent(p: Parameters) extends Module {
  val io = IO(new Bundle {
    val fabric  = Flipped(new FabricIO(p))   // AxiSlave's fabric (we receive)
    val periBusy = Output(Bool())
    val reqOut  = Decoupled(new LvdsFrameOut)
    val rspIn   = Flipped(Decoupled(new LvdsFrameIn))
  })

  val sIdle :: sSendReq :: sWaitRsp :: sRespRdIssue :: sRespRdData :: sRespWr :: Nil = Enum(6)
  val state = RegInit(sIdle)

  val isReadReg = Reg(Bool())
  val addrReg   = Reg(UInt(p.axi2AddrBits.W))
  val strbReg   = Reg(UInt((p.axi2DataBits / 8).W))
  val wdataReg  = Reg(UInt(p.axi2DataBits.W))
  val rdataReg  = Reg(UInt(p.axi2DataBits.W))
  val readOk    = Reg(Bool())  // false => SLVERR

  // ---- Timeout / retry bookkeeping ----
  val timer    = RegInit(0.U(LvdsLink.kTxTimerWidth.W))
  val retryCnt = RegInit(0.U(LvdsLink.kTxRetryWidth.W))
  val pendingDrain = RegInit(0.U(LvdsLink.kTxRetryWidth.W))
  val timerExpired = timer === (LvdsLink.kTxTimeoutCycles - 1).U
  val retryAvail   = retryCnt < LvdsLink.kTxMaxRetries.U
  val staleResp    = pendingDrain =/= 0.U

  // Defaults toward AxiSlave's fabric
  io.fabric.readData.valid  := false.B
  io.fabric.readData.bits   := rdataReg
  io.fabric.writeResp       := false.B

  // Hold periBusy in all states except the cycles where we want AxiSlave to
  // fire its read/write transaction.
  io.periBusy := !(state === sRespRdIssue || state === sRespWr)

  // Default for outgoing request frame
  val reqHdr = Wire(new LvdsHeader)
  reqHdr := 0.U.asTypeOf(new LvdsHeader)
  reqHdr.op   := Mux(isReadReg, LvdsOpcode.S_RD_REQ, LvdsOpcode.S_WR_REQ)
  reqHdr.id   := 0.U
  reqHdr.addr := addrReg
  reqHdr.strb := strbReg
  reqHdr.size := log2Ceil(p.axi2DataBits / 8).U
  reqHdr.resp := 0.U
  io.reqOut.valid := false.B
  io.reqOut.bits.header := reqHdr
  io.reqOut.bits.data   := wdataReg

  io.rspIn.ready := (state === sWaitRsp) || staleResp
  val drainThisCycle = io.rspIn.fire && staleResp
  when(drainThisCycle) { pendingDrain := pendingDrain - 1.U }

  switch(state) {
    is(sIdle) {
      // periBusy=true here; AxiSlave can drive valid but won't fire.
      // Capture the next command. Read takes priority if both happen to be
      // valid (AxiSlave's logic ensures only one is at a time anyway).
      when(io.fabric.readDataAddr.valid) {
        isReadReg := true.B
        addrReg   := io.fabric.readDataAddr.bits
        strbReg   := 0.U
        wdataReg  := 0.U
        retryCnt  := 0.U
        timer     := 0.U
        state     := sSendReq
      }.elsewhen(io.fabric.writeDataAddr.valid) {
        isReadReg := false.B
        addrReg   := io.fabric.writeDataAddr.bits
        strbReg   := io.fabric.writeDataStrb
        wdataReg  := io.fabric.writeDataBits
        retryCnt  := 0.U
        timer     := 0.U
        state     := sSendReq
      }
    }

    is(sSendReq) {
      io.reqOut.valid := true.B
      when(io.reqOut.fire) {
        timer := 0.U
        state := sWaitRsp
      }
    }

    is(sWaitRsp) {
      when(io.rspIn.fire && !staleResp) {
        when(isReadReg) {
          rdataReg := io.rspIn.bits.data
          readOk   := (io.rspIn.bits.header.op === LvdsOpcode.S_RD_RSP_OK)
          state    := sRespRdIssue
        }.otherwise {
          // S_WR_RSP carries 0 in resp on success
          readOk := (io.rspIn.bits.header.resp === 0.U)
          state  := sRespWr
        }
      } .otherwise {
        when(timerExpired) {
          val drainInc  = pendingDrain + 1.U
          val drainNext = Mux(drainThisCycle, drainInc - 1.U, drainInc)
          pendingDrain := drainNext
          timer        := 0.U
          when(retryAvail) {
            retryCnt := retryCnt + 1.U
            state    := sSendReq    // re-send same request frame
          } .otherwise {
            // Synthesize a SLVERR-style failure to AxiSlave.
            readOk   := false.B
            rdataReg := 0.U
            state    := Mux(isReadReg, sRespRdIssue, sRespWr)
          }
        } .otherwise {
          timer := timer + 1.U
        }
      }
    }

    is(sRespRdIssue) {
      // periBusy=false this cycle. AxiSlave fires issueRead. Next cycle we
      // drive readData.valid+bits.
      state := sRespRdData
    }

    is(sRespRdData) {
      io.fabric.readData.valid := readOk
      io.fabric.readData.bits  := rdataReg
      // periBusy=true this cycle (state != Issue/Wr). Done.
      state := sIdle
    }

    is(sRespWr) {
      io.fabric.writeResp := readOk
      // periBusy=false this cycle, AxiSlave's writeData fires.
      state := sIdle
    }
  }
}

// ---------------------------------------------------------------------------
// FPGA-side IBus agent.
// Receives M_IBUS_REQ frames; drives a synthesized IBusIO toward IBus2Axi.
// When IBus2Axi returns ready+rdata+fault, packs a M_IBUS_RSP frame.
// ---------------------------------------------------------------------------
class LvdsFpgaIBusAgent(p: Parameters) extends Module {
  val io = IO(new Bundle {
    val reqIn  = Flipped(Decoupled(new LvdsFrameIn))
    val rspOut = Decoupled(new LvdsFrameOut)
    val ibus   = new IBusIO(p)  // we drive valid+addr; IBus2Axi drives ready+rdata+fault
  })

  val sIdle :: sDriveBus :: sSendRsp :: Nil = Enum(3)
  val state = RegInit(sIdle)

  val addrReg = Reg(UInt(p.axi2AddrBits.W))
  val dataReg = Reg(UInt(p.axi2DataBits.W))
  val faultReg = Reg(Bool())

  // Defaults
  io.ibus.valid := (state === sDriveBus)
  io.ibus.addr  := addrReg

  io.reqIn.ready := (state === sIdle)

  val rspHdr = Wire(new LvdsHeader)
  rspHdr := 0.U.asTypeOf(new LvdsHeader)
  rspHdr.op := LvdsOpcode.M_IBUS_RSP
  rspHdr.id := LvdsLink.kIdIbus.U
  rspHdr.resp := Mux(faultReg, 2.U, 0.U)  // 2 = SLVERR per AxiResponseType
  io.rspOut.valid := (state === sSendRsp)
  io.rspOut.bits.header := rspHdr
  io.rspOut.bits.data   := dataReg

  switch(state) {
    is(sIdle) {
      when(io.reqIn.fire) {
        addrReg := io.reqIn.bits.header.addr
        state := sDriveBus
      }
    }
    is(sDriveBus) {
      when(io.ibus.ready) {
        dataReg  := io.ibus.rdata
        faultReg := io.ibus.fault.valid
        state := sSendRsp
      }
    }
    is(sSendRsp) {
      when(io.rspOut.fire) { state := sIdle }
    }
  }
}

// ---------------------------------------------------------------------------
// FPGA-side EBus (DBus) agent. Single in-flight read or write.
// ---------------------------------------------------------------------------
class LvdsFpgaEBusAgent(p: Parameters) extends Module {
  val io = IO(new Bundle {
    val reqIn  = Flipped(Decoupled(new LvdsFrameIn))
    val rspOut = Decoupled(new LvdsFrameOut)
    val dbus   = new DBusIO(p)  // we drive valid/write/addr/size/wdata/wmask; DBus2Axi drives ready/rdata
    val fault  = Flipped(Valid(new FaultInfo(p)))
  })

  // sLatchRdata exists so we sample `io.dbus.rdata` ONE CYCLE AFTER
  // `io.dbus.ready` fires for reads. DBus2Axi (which is what we drive
  // here) routes its read return through a 1-deep delay register
  // (`readNext`) -- the comment in DBus2Axi.scala calls this out as
  // "Insert delay register to match dbus interface expecations,
  // changing on fire". So at the cycle of `ready=1` `dbus.rdata` is
  // still showing the PREVIOUS transaction's data; the actual response
  // appears on the next cycle. Latching on `ready` (the natural-looking
  // thing to do) was returning stale data and shifting every read by
  // one beat, which was visible end-to-end as conv2d's `run_opt`
  // saturating: `extdata` reads through the LVDS chain were all
  // off-by-one beats vs what the kernel issued.
  val sIdle :: sDriveBus :: sLatchRdata :: sSendRsp :: Nil = Enum(4)
  val state = RegInit(sIdle)

  val addrReg     = Reg(UInt(p.axi2AddrBits.W))
  val sizeReg     = Reg(UInt(3.W))
  val strbReg     = Reg(UInt((p.axi2DataBits / 8).W))
  val wdataReg    = Reg(UInt(p.axi2DataBits.W))
  val rdataReg    = Reg(UInt(p.axi2DataBits.W))
  val isWriteReg  = Reg(Bool())
  val faultReg    = Reg(Bool())

  // Drive synthesized DBus toward DBus2Axi
  io.dbus.valid := (state === sDriveBus)
  io.dbus.write := isWriteReg
  io.dbus.pc    := 0.U
  io.dbus.addr  := addrReg
  io.dbus.adrx  := addrReg
  // size in DBus is a one-hot byte mask (PopCount==1). Convert from log2.
  io.dbus.size  := (1.U << sizeReg)
  io.dbus.wdata := wdataReg
  io.dbus.wmask := strbReg

  io.reqIn.ready := (state === sIdle)

  val rspHdr = Wire(new LvdsHeader)
  rspHdr := 0.U.asTypeOf(new LvdsHeader)
  rspHdr.op := Mux(isWriteReg, LvdsOpcode.M_DBUS_WR_RSP, LvdsOpcode.M_DBUS_RD_RSP)
  rspHdr.id := LvdsLink.kIdEbus.U
  rspHdr.resp := Mux(faultReg, 2.U, 0.U)
  io.rspOut.valid := (state === sSendRsp)
  io.rspOut.bits.header := rspHdr
  io.rspOut.bits.data   := rdataReg

  switch(state) {
    is(sIdle) {
      when(io.reqIn.fire) {
        val hdr = io.reqIn.bits.header
        addrReg    := hdr.addr
        sizeReg    := hdr.size
        strbReg    := hdr.strb(p.axi2DataBits / 8 - 1, 0)
        wdataReg   := io.reqIn.bits.data
        isWriteReg := (hdr.op === LvdsOpcode.M_DBUS_WR_REQ)
        state := sDriveBus
      }
    }
    is(sDriveBus) {
      when(io.dbus.ready) {
        // `io.fault.valid` is from the AXI response side and is valid
        // in the SAME cycle as `dbus.ready` (matches DBus2Axi).
        faultReg := io.fault.valid
        // Writes have no rdata; jump directly to send-response.
        // Reads MUST wait one extra cycle to observe the actual rdata
        // -- see the comment on the state Enum above.
        state := Mux(isWriteReg, sSendRsp, sLatchRdata)
      }
    }
    is(sLatchRdata) {
      // dbus.valid is now low (state != sDriveBus), so DBus2Axi's
      // `readNext` register holds steady at the latest read result.
      rdataReg := io.dbus.rdata
      state    := sSendRsp
    }
    is(sSendRsp) {
      when(io.rspOut.fire) { state := sIdle }
    }
  }
}

// ---------------------------------------------------------------------------
// FPGA-side LVDS adapter top.
// ---------------------------------------------------------------------------
class LvdsAdapterFpga(
    p: Parameters,
    useChiselAsyncQueue: Boolean = true,
) extends RawModule {
  val io = IO(new Bundle {
    val core_clk     = Input(Clock())
    val core_aresetn = Input(Bool())
    val lvds_clk     = Input(Clock())
    val lvds_aresetn = Input(Bool())

    // LVDS PHY (note: this is the FPGA side, so what the chip TX sends is
    // what we receive here, and vice versa).
    val tx_valid = Output(Bool())
    val tx_ready = Input(Bool())
    val tx_data  = Output(UInt(LvdsLink.kBeatBits.W))
    val rx_valid = Input(Bool())
    val rx_data  = Input(UInt(LvdsLink.kBeatBits.W))

    // External AXI interfaces (core_clk domain)
    val axi_slave  = Flipped(new AxiMasterIO(p.axi2AddrBits, p.axi2DataBits, p.axi2IdBits))
    val axi_master = new AxiMasterIO(p.axi2AddrBits, p.axi2DataBits, p.axi2IdBits)
  })

  // Three physical AsyncFIFOs per direction (req / rsp / ctrl). See the
  // matching commentary in LvdsAdapterChip.scala for rationale.
  val txAsyncReq = Module(new LvdsAsyncFifo(useChiselAsyncQueue,
    LvdsLink.kBeatBits, LvdsLink.kAsyncDataDepthLog2))
  val txAsyncRsp = Module(new LvdsAsyncFifo(useChiselAsyncQueue,
    LvdsLink.kBeatBits, LvdsLink.kAsyncDataDepthLog2))
  val txAsyncCtrl = Module(new LvdsAsyncFifo(useChiselAsyncQueue,
    LvdsLink.kBeatBits, LvdsLink.kAsyncCtrlDepthLog2))
  val rxAsyncReq = Module(new LvdsAsyncFifo(useChiselAsyncQueue,
    LvdsLink.kBeatBits, LvdsLink.kAsyncDataDepthLog2))
  val rxAsyncRsp = Module(new LvdsAsyncFifo(useChiselAsyncQueue,
    LvdsLink.kBeatBits, LvdsLink.kAsyncDataDepthLog2))
  val rxAsyncCtrl = Module(new LvdsAsyncFifo(useChiselAsyncQueue,
    LvdsLink.kBeatBits, LvdsLink.kAsyncCtrlDepthLog2))

  for (f <- Seq(txAsyncReq, txAsyncRsp, txAsyncCtrl)) {
    f.io.wrclk   := io.core_clk
    f.io.wr_rstn := io.core_aresetn
    f.io.rdclk   := io.lvds_clk
    f.io.rd_rstn := io.lvds_aresetn
  }
  for (f <- Seq(rxAsyncReq, rxAsyncRsp, rxAsyncCtrl)) {
    f.io.wrclk   := io.lvds_clk
    f.io.wr_rstn := io.lvds_aresetn
    f.io.rdclk   := io.core_clk
    f.io.rd_rstn := io.core_aresetn
  }

  // LVDS-domain TX 3-way mux (ctrl strict > RR(req,rsp), frame-aligned).
  val txSelCtrl = Wire(Bool())
  val txSelReq  = Wire(Bool())
  val txSelRsp  = Wire(Bool())
  io.tx_valid := txAsyncCtrl.io.deq.valid || txAsyncReq.io.deq.valid ||
                 txAsyncRsp.io.deq.valid
  io.tx_data  := Mux(txSelCtrl, txAsyncCtrl.io.deq.bits,
                  Mux(txSelReq,  txAsyncReq.io.deq.bits,
                                 txAsyncRsp.io.deq.bits))
  txAsyncCtrl.io.deq.ready := io.tx_ready && txSelCtrl
  txAsyncReq.io.deq.ready  := io.tx_ready && txSelReq
  txAsyncRsp.io.deq.ready  := io.tx_ready && txSelRsp

  withClockAndReset(io.lvds_clk, (!io.lvds_aresetn).asAsyncReset) {
    val reqMid = RegInit(false.B)
    val rspMid = RegInit(false.B)
    val reqOp  = txAsyncReq.io.deq.bits.asTypeOf(new LvdsHeader).op
    val rspOp  = txAsyncRsp.io.deq.bits.asTypeOf(new LvdsHeader).op
    val rrPick = RegInit(false.B)
    val anyMid   = reqMid || rspMid
    val reqElig  = txAsyncReq.io.deq.valid && (reqMid || !anyMid)
    val rspElig  = txAsyncRsp.io.deq.valid && (rspMid || !anyMid)
    val ctrlSel  = txAsyncCtrl.io.deq.valid && !anyMid
    val dataReqSel =  reqElig && (rrPick === false.B || !rspElig)
    val dataRspSel =  rspElig && (rrPick === true.B  || !reqElig)
    txSelCtrl := ctrlSel
    txSelReq  := !ctrlSel && dataReqSel
    txSelRsp  := !ctrlSel && !dataReqSel && dataRspSel
    when(txAsyncReq.io.deq.fire) {
      when(reqMid) { reqMid := false.B }
      .otherwise   { reqMid := LvdsLink.hasDataBeat(reqOp) }
    }
    when(txAsyncRsp.io.deq.fire) {
      when(rspMid) { rspMid := false.B }
      .otherwise   { rspMid := LvdsLink.hasDataBeat(rspOp) }
    }
    val reqFrameDone = txAsyncReq.io.deq.fire &&
      (reqMid || !LvdsLink.hasDataBeat(reqOp))
    val rspFrameDone = txAsyncRsp.io.deq.fire &&
      (rspMid || !LvdsLink.hasDataBeat(rspOp))
    when(reqFrameDone) { rrPick := true.B }
    .elsewhen(rspFrameDone) { rrPick := false.B }
  }

  // LVDS-domain RX 3-way demux (by opcode + per-channel expect-data state).
  val rxIsReq  = Wire(Bool())
  val rxIsRsp  = Wire(Bool())
  val rxIsCtrl = Wire(Bool())
  withClockAndReset(io.lvds_clk, (!io.lvds_aresetn).asAsyncReset) {
    val reqExpectData = RegInit(false.B)
    val rspExpectData = RegInit(false.B)
    val rxHdr = io.rx_data.asTypeOf(new LvdsHeader)
    val isHdr = io.rx_valid && !reqExpectData && !rspExpectData
    val isCreditHdr = isHdr && LvdsLink.isCreditUpdate(rxHdr.op)
    val isReqHdr    = isHdr && LvdsLink.isReqOpcode(rxHdr.op)
    val isRspHdr    = isHdr && LvdsLink.isRspOpcode(rxHdr.op)
    rxIsCtrl := isCreditHdr
    rxIsReq  := isReqHdr || (io.rx_valid && reqExpectData)
    rxIsRsp  := isRspHdr || (io.rx_valid && rspExpectData)
    when(io.rx_valid) {
      when(reqExpectData) {
        reqExpectData := false.B
      } .elsewhen(rspExpectData) {
        rspExpectData := false.B
      } .otherwise {
        when(isReqHdr && LvdsLink.hasDataBeat(rxHdr.op)) {
          reqExpectData := true.B
        }
        when(isRspHdr && LvdsLink.hasDataBeat(rxHdr.op)) {
          rspExpectData := true.B
        }
      }
    }
  }
  rxAsyncCtrl.io.enq.valid := rxIsCtrl
  rxAsyncCtrl.io.enq.bits  := io.rx_data
  rxAsyncReq.io.enq.valid  := rxIsReq
  rxAsyncReq.io.enq.bits   := io.rx_data
  rxAsyncRsp.io.enq.valid  := rxIsRsp
  rxAsyncRsp.io.enq.bits   := io.rx_data

  withClockAndReset(io.core_clk, (!io.core_aresetn).asAsyncReset) {

    // ---- Sync FIFOs (credit pools) ----
    val rxSyncReq  = Module(new Queue(UInt(LvdsLink.kBeatBits.W),
                                      LvdsLink.kSyncReqDepth, flow = true))
    val rxSyncRsp  = Module(new Queue(UInt(LvdsLink.kBeatBits.W),
                                      LvdsLink.kSyncRspDepth, flow = true))
    val rxSyncCtrl = Module(new Queue(UInt(LvdsLink.kBeatBits.W),
                                      LvdsLink.kSyncCtrlDepth, flow = true))
    rxSyncReq.io.enq  <> rxAsyncReq.io.deq
    rxSyncRsp.io.enq  <> rxAsyncRsp.io.deq
    rxSyncCtrl.io.enq <> rxAsyncCtrl.io.deq

    // ---- Per-channel RX dispatchers ----
    // FPGA RX req carries M_IBUS_REQ / M_DBUS_*_REQ -> ibusAg / ebusAg.
    // FPGA RX rsp carries S_*_RSP                   -> slaveAg.
    val reqDisp = Module(new LvdsRxDispatcherFpga)
    val rspDisp = Module(new LvdsRxDispatcherFpga)
    reqDisp.io.pop <> rxSyncReq.io.deq
    rspDisp.io.pop <> rxSyncRsp.io.deq

    // -------------------------------------------------------------------------
    // Slave path: AxiSlave + agent
    // -------------------------------------------------------------------------
    val axiSlave = Module(new AxiSlave(p))
    val slaveAg = Module(new LvdsFpgaSlaveAgent(p))

    axiSlave.io.fabric <> slaveAg.io.fabric
    axiSlave.io.periBusy := slaveAg.io.periBusy

    axiSlave.io.axi.write.addr <> io.axi_slave.write.addr
    axiSlave.io.axi.write.data <> io.axi_slave.write.data
    io.axi_slave.write.resp    <> axiSlave.io.axi.write.resp
    axiSlave.io.axi.read.addr  <> io.axi_slave.read.addr
    io.axi_slave.read.data     <> axiSlave.io.axi.read.data

    // -------------------------------------------------------------------------
    // Master path
    // -------------------------------------------------------------------------
    val ibusAg  = Module(new LvdsFpgaIBusAgent(p))
    val ebusAg  = Module(new LvdsFpgaEBusAgent(p))
    val ibus2axi = IBus2Axi(p, id = LvdsLink.kIdIbus)
    val ebus2axi = DBus2Axi(p, id = LvdsLink.kIdEbus)

    // Hook dispatcher outputs to the right channel's consumers; tie unused
    // outputs and assert they never fire.
    reqDisp.io.ibus  <> ibusAg.io.reqIn
    reqDisp.io.ebus  <> ebusAg.io.reqIn
    reqDisp.io.slave.ready := true.B
    assert(!reqDisp.io.slave.valid,
      "S_*_RSP arrived on FPGA RX req channel (routing bug)")

    rspDisp.io.slave <> slaveAg.io.rspIn
    rspDisp.io.ibus.ready := true.B
    rspDisp.io.ebus.ready := true.B
    assert(!rspDisp.io.ibus.valid,
      "M_IBUS_REQ arrived on FPGA RX rsp channel (routing bug)")
    assert(!rspDisp.io.ebus.valid,
      "M_DBUS_*_REQ arrived on FPGA RX rsp channel (routing bug)")

    ibus2axi.io.ibus  <> ibusAg.io.ibus
    ebus2axi.io.dbus  <> ebusAg.io.dbus
    ebusAg.io.fault   <> ebus2axi.io.fault

    io.axi_master.write <> ebus2axi.io.axi.write

    val readAddrArb = Module(new CoralNPURRArbiter(
      new AxiAddress(p.axi2AddrBits, p.axi2DataBits, p.axi2IdBits), 2))
    readAddrArb.io.in(0) <> ebus2axi.io.axi.read.addr
    readAddrArb.io.in(1) <> ibus2axi.io.axi.addr
    io.axi_master.read.addr <> readAddrArb.io.out

    ebus2axi.io.axi.read.data.valid := io.axi_master.read.data.valid &&
        (io.axi_master.read.data.bits.id === LvdsLink.kIdEbus.U)
    ebus2axi.io.axi.read.data.bits := io.axi_master.read.data.bits
    ibus2axi.io.axi.data.valid := io.axi_master.read.data.valid &&
        (io.axi_master.read.data.bits.id === LvdsLink.kIdIbus.U)
    ibus2axi.io.axi.data.bits := io.axi_master.read.data.bits
    io.axi_master.read.data.ready := Mux(
      io.axi_master.read.data.bits.id === LvdsLink.kIdIbus.U,
      ibus2axi.io.axi.data.ready,
      ebus2axi.io.axi.read.data.ready)

    // -------------------------------------------------------------------------
    // TX arbiters split by channel:
    //   * req channel: slaveAg.reqOut (FPGA is master via external AXI slave)
    //   * rsp channel: ibusAg.rspOut + ebusAg.rspOut (responses to chip)
    // -------------------------------------------------------------------------
    val reqArb = Module(new LvdsTxArbiter(numSources = 1))
    reqArb.io.sources(0) <> slaveAg.io.reqOut
    val rspArb = Module(new LvdsTxArbiter(numSources = 2))
    rspArb.io.sources(0) <> ibusAg.io.rspOut
    rspArb.io.sources(1) <> ebusAg.io.rspOut

    // -------------------------------------------------------------------------
    // CreditTracker (dual-pool).
    // -------------------------------------------------------------------------
    val ct = Module(new CreditTracker)
    ct.io.rxReqPop := rxSyncReq.io.deq.fire
    ct.io.rxRspPop := rxSyncRsp.io.deq.fire

    txAsyncCtrl.io.enq <> ct.io.creditPktOut

    val reqCanPush  = txAsyncReq.io.enq.ready && ct.io.txReqCreditAvail
    val reqPushFire = reqArb.io.push.valid && reqCanPush
    reqArb.io.push.ready := reqCanPush
    txAsyncReq.io.enq.valid := reqPushFire
    txAsyncReq.io.enq.bits  := reqArb.io.push.bits
    ct.io.txReqPush := reqPushFire

    val rspCanPush  = txAsyncRsp.io.enq.ready && ct.io.txRspCreditAvail
    val rspPushFire = rspArb.io.push.valid && rspCanPush
    rspArb.io.push.ready := rspCanPush
    txAsyncRsp.io.enq.valid := rspPushFire
    txAsyncRsp.io.enq.bits  := rspArb.io.push.bits
    ct.io.txRspPush := rspPushFire

    val rxCtrlBeat = rxSyncCtrl.io.deq.bits
    val rxCtrlHdr  = rxCtrlBeat.asTypeOf(new LvdsHeader)
    rxSyncCtrl.io.deq.ready := true.B
    ct.io.creditUpdIn.valid := rxSyncCtrl.io.deq.fire
    ct.io.creditUpdIn.bits.reqConsumed :=
      rxCtrlHdr.id(LvdsLink.kCreditCounterBits - 1, 0)
    ct.io.creditUpdIn.bits.rspConsumed :=
      rxCtrlHdr.addr(LvdsLink.kCreditCounterBits - 1, 0)
  }
}
