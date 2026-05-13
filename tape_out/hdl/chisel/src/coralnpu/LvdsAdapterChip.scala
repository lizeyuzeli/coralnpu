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

// Chip-side LVDS adapter.
//
// Bridges between the core-clock-domain "fabric / ibus / ebus" interfaces
// (which connect into CoreChipKernel) and the LVDS PHY (LVDS Tx 128b
// valid+ready, LVDS Rx 128b valid-only). All "smart" logic (state machines)
// lives in core-clock domain; only the TX/RX async FIFOs and trivial wiring
// touch the lvds-clock domain. Credit-based flow control over a per-direction
// GPIO ensures the LVDS Rx FIFO on the far side never overflows.

package coralnpu

import chisel3._
import chisel3.util._

// Convenience bundle: a frame to be transmitted (header + data). When the
// header opcode does not carry a data beat the `data` field is ignored.
class LvdsFrameOut extends Bundle {
  val header = new LvdsHeader
  val data   = UInt(LvdsLink.kBeatBits.W)
}

// Multi-source TX arbiter: round-robin over Decoupled[LvdsFrameOut] sources;
// for the selected source emit 1 (header only) or 2 (header + data) beats
// into `push`. The source is dequeued only on the LAST beat of the frame.
class LvdsTxArbiter(numSources: Int) extends Module {
  val io = IO(new Bundle {
    val sources = Vec(numSources, Flipped(Decoupled(new LvdsFrameOut)))
    val push    = Decoupled(UInt(LvdsLink.kBeatBits.W))
  })

  val sIdle :: sHeader :: sData :: Nil = Enum(3)
  val state = RegInit(sIdle)
  val sel   = RegInit(0.U(log2Ceil(numSources max 1).W))
  val rrPtr = RegInit(0.U(log2Ceil(numSources max 1).W))
  // Helper: increment with explicit wrap around numSources.
  // Plain `sel + 1.U` truncates to 2-bit when numSources=3 and can leave rrPtr=3
  // (out of range), which corrupts subsequent `% numSources.U` arithmetic.
  def wrapInc(x: UInt): UInt = Mux(x === (numSources - 1).U, 0.U, x + 1.U)

  // Round-robin starting from rrPtr, prefer the next source after the last
  // serviced one.
  val validVec = VecInit(io.sources.map(_.valid))
  val rotated = VecInit((0 until numSources).map(i =>
    validVec(((i.U + rrPtr) % numSources.U)(log2Ceil(numSources max 1) - 1, 0))
  ))
  val rotIdx = PriorityEncoder(rotated)
  val nextSel = ((rotIdx + rrPtr) % numSources.U)(log2Ceil(numSources max 1) - 1, 0)
  val anyValid = validVec.reduce(_ || _)

  // Selected source bits
  val selHdr  = io.sources(sel).bits.header
  val selData = io.sources(sel).bits.data
  val selHasData = LvdsLink.hasDataBeat(selHdr.op)

  // Default outputs
  io.push.valid := false.B
  io.push.bits  := 0.U
  for (i <- 0 until numSources) {
    io.sources(i).ready := false.B
  }

  switch(state) {
    is(sIdle) {
      when(anyValid) {
        sel := nextSel
        state := sHeader
      }
    }
    is(sHeader) {
      io.push.valid := true.B
      io.push.bits  := selHdr.asUInt
      when(io.push.ready) {
        when(selHasData) {
          state := sData
        }.otherwise {
          // Last beat: dequeue source.
          state := sIdle
          rrPtr := wrapInc(sel)
          for (i <- 0 until numSources) {
            when(sel === i.U) { io.sources(i).ready := true.B }
          }
        }
      }
    }
    is(sData) {
      io.push.valid := true.B
      io.push.bits  := selData
      when(io.push.ready) {
        state := sIdle
        rrPtr := wrapInc(sel)
        for (i <- 0 until numSources) {
          when(sel === i.U) { io.sources(i).ready := true.B }
        }
      }
    }
  }
}

// Convenience bundle: a frame received from the link (header + data, where
// data is undefined for header-only opcodes).
class LvdsFrameIn extends Bundle {
  val header = new LvdsHeader
  val data   = UInt(LvdsLink.kBeatBits.W)
}

// RX dispatcher: pops 128-bit beats from `pop`, reassembles into LvdsFrameIn,
// and routes by opcode to one of the per-target Decoupled outputs.
//
// Routing table (chip-side targets):
//   S_RD_REQ, S_WR_REQ        -> slave
//   M_IBUS_RSP                -> ibus
//   M_DBUS_RD_RSP, M_DBUS_WR_RSP -> ebus
class LvdsRxDispatcher extends Module {
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

  // Compute target route based on opcode
  def routeIsSlave(op: LvdsOpcode.Type): Bool =
    op.isOneOf(LvdsOpcode.S_RD_REQ, LvdsOpcode.S_WR_REQ)
  def routeIsIbus(op: LvdsOpcode.Type): Bool =
    (op === LvdsOpcode.M_IBUS_RSP)
  def routeIsEbus(op: LvdsOpcode.Type): Bool =
    op.isOneOf(LvdsOpcode.M_DBUS_RD_RSP, LvdsOpcode.M_DBUS_WR_RSP)

  val toSlave = routeIsSlave(curHdr.op)
  val toIbus  = routeIsIbus(curHdr.op)
  val toEbus  = routeIsEbus(curHdr.op)
  val targetReady =
    (toSlave && io.slave.ready) ||
    (toIbus  && io.ibus.ready)  ||
    (toEbus  && io.ebus.ready)

  val frameComplete = WireInit(false.B)
  val popHasData    = LvdsLink.hasDataBeat(popHdr.op)

  // Default
  io.pop.ready := false.B
  io.slave.valid := false.B
  io.ibus.valid  := false.B
  io.ebus.valid  := false.B

  // Common payload for outputs
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
          // Just cache header. Always accept.
          io.pop.ready := true.B
          when(io.pop.fire) {
            hdrReg := popHdr
            state := sData
          }
        }.otherwise {
          // Header-only frame: dispatch this cycle.
          // Drive valid based on opcode; accept beat only when target ready.
          when(routeIsSlave(popHdr.op)) { io.slave.valid := true.B }
          when(routeIsIbus(popHdr.op))  { io.ibus.valid  := true.B }
          when(routeIsEbus(popHdr.op))  { io.ebus.valid  := true.B }
          io.pop.ready := targetReady
          frameComplete := io.pop.fire
        }
      }
    }
    is(sData) {
      when(io.pop.valid) {
        // Data beat: dispatch with cached header.
        when(routeIsSlave(hdrReg.op)) { io.slave.valid := true.B }
        when(routeIsIbus(hdrReg.op))  { io.ibus.valid  := true.B }
        when(routeIsEbus(hdrReg.op))  { io.ebus.valid  := true.B }
        io.pop.ready := targetReady
        when(io.pop.fire) {
          state := sHdr
          frameComplete := true.B
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Slave engine (chip-side)
//
// Receives slave commands (S_RD_REQ / S_WR_REQ) on `cmdIn`, drives the
// chip's local fabricMux source, and emits responses on `rspOut`.
// Single in-flight transaction.
// ---------------------------------------------------------------------------
class LvdsSlaveEngine(p: Parameters) extends Module {
  val io = IO(new Bundle {
    val cmdIn  = Flipped(Decoupled(new LvdsFrameIn))
    val rspOut = Decoupled(new LvdsFrameOut)
    val fabric = new FabricIO(p)
  })

  // Default fabric source idle
  io.fabric.readDataAddr.valid  := false.B
  io.fabric.readDataAddr.bits   := 0.U
  io.fabric.writeDataAddr.valid := false.B
  io.fabric.writeDataAddr.bits  := 0.U
  io.fabric.writeDataBits       := 0.U
  io.fabric.writeDataStrb       := 0.U

  val sIdle :: sIssueRead :: sWaitRead :: sIssueWrite :: sPushRsp :: Nil = Enum(5)
  val state = RegInit(sIdle)

  val addrReg = Reg(UInt(p.axi2AddrBits.W))
  val strbReg = Reg(UInt((p.axi2DataBits / 8).W))
  val dataReg = Reg(UInt(p.axi2DataBits.W))
  val isReadReg = Reg(Bool())
  val rspOpReg = Reg(LvdsOpcode())

  io.cmdIn.ready  := (state === sIdle)
  io.rspOut.valid := false.B

  // Build response template
  val rspHdr = Wire(new LvdsHeader)
  rspHdr := 0.U.asTypeOf(new LvdsHeader)
  rspHdr.op := rspOpReg
  rspHdr.id := 0.U
  rspHdr.addr := 0.U
  rspHdr.strb := 0.U
  rspHdr.size := 0.U
  rspHdr.resp := 0.U
  io.rspOut.bits.header := rspHdr
  io.rspOut.bits.data   := dataReg

  switch(state) {
    is(sIdle) {
      when(io.cmdIn.fire) {
        addrReg   := io.cmdIn.bits.header.addr
        strbReg   := io.cmdIn.bits.header.strb(p.axi2DataBits / 8 - 1, 0)
        isReadReg := (io.cmdIn.bits.header.op === LvdsOpcode.S_RD_REQ)
        when(io.cmdIn.bits.header.op === LvdsOpcode.S_RD_REQ) {
          state := sIssueRead
        }.otherwise {
          // S_WR_REQ: data already arrived in same FrameIn (data beat)
          dataReg := io.cmdIn.bits.data
          state := sIssueWrite
        }
      }
    }

    is(sIssueRead) {
      io.fabric.readDataAddr.valid := true.B
      io.fabric.readDataAddr.bits  := addrReg
      // FabricMux's readData lands one cycle later. Move on once we're sure
      // the request was forwarded. We use a simple model: assume the request
      // is accepted in this cycle (FabricMux has no fabricBusy back-pressure
      // for the chip-side single-source case other than when peri is busy
      // -- in that case readData.valid won't fire and we just keep waiting
      // by re-driving from sWaitRead).
      state := sWaitRead
    }

    is(sWaitRead) {
      // Keep the read addr asserted until response valid lands. Re-issuing
      // the same addr is harmless for combinational TCM mux logic.
      io.fabric.readDataAddr.valid := true.B
      io.fabric.readDataAddr.bits  := addrReg
      when(io.fabric.readData.valid) {
        dataReg := io.fabric.readData.bits
        rspOpReg := LvdsOpcode.S_RD_RSP_OK
        state := sPushRsp
      }
    }

    is(sIssueWrite) {
      io.fabric.writeDataAddr.valid := true.B
      io.fabric.writeDataAddr.bits  := addrReg
      io.fabric.writeDataBits       := dataReg
      io.fabric.writeDataStrb       := strbReg
      // writeResp is combinational-ish from fabricMux; we capture it the
      // cycle after asserting the write (registered by the TCMs).
      rspOpReg := LvdsOpcode.S_WR_RSP
      state := sPushRsp
    }

    is(sPushRsp) {
      io.rspOut.valid := true.B
      when(io.rspOut.fire) {
        state := sIdle
      }
    }
  }
}

// ---------------------------------------------------------------------------
// IBus shim: bridges core's IBus to LVDS M_IBUS_REQ / M_IBUS_RSP frames.
// Single in-flight read.
// ---------------------------------------------------------------------------
class LvdsIBusShim(p: Parameters) extends Module {
  val io = IO(new Bundle {
    val ibus   = Flipped(new IBusIO(p))
    val reqOut = Decoupled(new LvdsFrameOut)
    val rspIn  = Flipped(Decoupled(new LvdsFrameIn))
  })

  val linebit = log2Ceil(p.lsuDataBits / 8)
  // Line-aligned address from the core (matches existing IBus2Axi behavior).
  val lineAddr = Cat(io.ibus.addr(31, linebit), 0.U(linebit.W))

  val sIdle :: sReqOut :: sWaitRsp :: sPresent :: Nil = Enum(4)
  val state = RegInit(sIdle)
  val addrReg = Reg(UInt(p.axi2AddrBits.W))
  val dataReg = Reg(UInt(p.axi2DataBits.W))
  val respReg = Reg(UInt(2.W))

  // ---- Timeout / retry bookkeeping ----
  val timer    = RegInit(0.U(LvdsLink.kTxTimerWidth.W))
  val retryCnt = RegInit(0.U(LvdsLink.kTxRetryWidth.W))
  // # of outstanding "abandoned" requests whose responses may still trickle
  // back. We silently drain them here so they don't poison the next
  // transaction.
  val pendingDrain = RegInit(0.U(LvdsLink.kTxRetryWidth.W))
  val timerExpired = timer === (LvdsLink.kTxTimeoutCycles - 1).U
  val retryAvail   = retryCnt < LvdsLink.kTxMaxRetries.U
  val staleResp    = pendingDrain =/= 0.U

  // Defaults
  io.ibus.ready := false.B
  io.ibus.rdata := dataReg
  io.ibus.fault.valid := false.B
  io.ibus.fault.bits.write := false.B
  io.ibus.fault.bits.addr  := addrReg
  io.ibus.fault.bits.epc   := io.ibus.addr

  io.reqOut.valid := false.B
  val reqHdr = Wire(new LvdsHeader)
  reqHdr := 0.U.asTypeOf(new LvdsHeader)
  reqHdr.op := LvdsOpcode.M_IBUS_REQ
  reqHdr.id := LvdsLink.kIdIbus.U
  reqHdr.addr := lineAddr
  reqHdr.strb := 0.U
  reqHdr.size := log2Ceil(p.axi2DataBits / 8).U
  reqHdr.resp := 0.U
  io.reqOut.bits.header := reqHdr
  io.reqOut.bits.data   := 0.U

  // Accept response when we're either actively waiting OR draining a
  // late stale response from a previously-abandoned attempt.
  io.rspIn.ready := (state === sWaitRsp) || staleResp
  // Stale absorption takes precedence: even while in sWaitRsp, the first
  // pendingDrain responses are treated as belonging to the abandoned
  // earlier request, not the current one.
  val drainThisCycle = io.rspIn.fire && staleResp
  when(drainThisCycle) { pendingDrain := pendingDrain - 1.U }

  switch(state) {
    is(sIdle) {
      // pendingDrain decrement already handled above; nothing else here
      // beyond the normal transaction kick-off.
      when(io.ibus.valid) {
        addrReg  := lineAddr
        retryCnt := 0.U
        timer    := 0.U
        state    := sReqOut
      }
    }
    is(sReqOut) {
      io.reqOut.valid := true.B
      // Re-evaluate header addr from the latched value to keep stable bits.
      reqHdr.addr := addrReg
      when(io.reqOut.fire) {
        timer := 0.U
        state := sWaitRsp
      }
    }
    is(sWaitRsp) {
      // Normal in-window response (NOT consumed as stale).
      when(io.rspIn.fire && !staleResp) {
        dataReg := io.rspIn.bits.data
        respReg := io.rspIn.bits.header.resp
        state   := sPresent
      } .otherwise {
        // Tick the timer; on expiry, retry or give up.
        when(timerExpired) {
          // The just-abandoned outstanding request might still respond
          // later -- bump pendingDrain so its eventual response is
          // silently consumed. (drainThisCycle in the same cycle would
          // also decrement; net effect handled by the muxed update.)
          val drainInc = pendingDrain + 1.U
          val drainNext = Mux(drainThisCycle, drainInc - 1.U, drainInc)
          pendingDrain := drainNext
          timer := 0.U
          when(retryAvail) {
            retryCnt := retryCnt + 1.U
            state    := sReqOut       // re-send same frame
          } .otherwise {
            // Max retries hit -> synthesize a SLVERR response to core.
            dataReg := 0.U
            respReg := 2.U            // AXI SLVERR
            state   := sPresent
          }
        } .otherwise {
          timer := timer + 1.U
        }
      }
    }
    is(sPresent) {
      io.ibus.ready := io.ibus.valid &&
                       (Cat(io.ibus.addr(31, linebit), 0.U(linebit.W)) === addrReg)
      io.ibus.fault.valid := io.ibus.ready && (respReg =/= 0.U)
      // Per IBusIO protocol: once `valid` is asserted, `addr` must remain
      // constant until `ready` fires. Catch any violation that would cause
      // us to silently drop the response data we already accepted.
      assert(!(io.ibus.valid &&
               (Cat(io.ibus.addr(31, linebit), 0.U(linebit.W)) =/= addrReg)),
             "LvdsIBusShim: core changed ibus.addr before ready fired " +
             "(protocol violation)")
      when(io.ibus.ready || !io.ibus.valid) {
        // Move back to idle once we serve (or core dropped the request).
        state := sIdle
      }
    }
  }
}

// ---------------------------------------------------------------------------
// EBus (DBus) shim: bridges core's EBus to LVDS M_DBUS_RD_REQ / M_DBUS_WR_REQ
// and consumes M_DBUS_RD_RSP / M_DBUS_WR_RSP. Single in-flight transaction.
// ---------------------------------------------------------------------------
class LvdsEBusShim(p: Parameters) extends Module {
  val io = IO(new Bundle {
    val ebus   = Flipped(new EBusIO(p))
    val reqOut = Decoupled(new LvdsFrameOut)
    val rspIn  = Flipped(Decoupled(new LvdsFrameIn))
  })

  val sIdle :: sReqRead :: sWaitRead :: sReqWrite :: sWaitWrite :: sDone :: Nil = Enum(6)
  val state = RegInit(sIdle)
  val addrReg = Reg(UInt(p.axi2AddrBits.W))
  val dataReg = Reg(UInt(p.axi2DataBits.W))
  val strbReg = Reg(UInt((p.axi2DataBits / 8).W))
  val sizeReg = Reg(UInt(3.W))
  val isWriteReg = Reg(Bool())
  val respReg = Reg(UInt(2.W))

  // ---- Timeout / retry bookkeeping (shared across read/write since the
  //      shim is single-in-flight). ----
  val timer    = RegInit(0.U(LvdsLink.kTxTimerWidth.W))
  val retryCnt = RegInit(0.U(LvdsLink.kTxRetryWidth.W))
  val pendingDrain = RegInit(0.U(LvdsLink.kTxRetryWidth.W))
  val timerExpired = timer === (LvdsLink.kTxTimeoutCycles - 1).U
  val retryAvail   = retryCnt < LvdsLink.kTxMaxRetries.U
  val staleResp    = pendingDrain =/= 0.U
  val inWait       = (state === sWaitRead) || (state === sWaitWrite)

  // Defaults
  io.ebus.dbus.ready := false.B
  io.ebus.dbus.rdata := dataReg
  io.ebus.fault.valid := false.B
  io.ebus.fault.bits.write := isWriteReg
  io.ebus.fault.bits.addr  := addrReg
  io.ebus.fault.bits.epc   := io.ebus.dbus.pc

  // Build request frame
  val reqHdr = Wire(new LvdsHeader)
  reqHdr := 0.U.asTypeOf(new LvdsHeader)
  reqHdr.id := LvdsLink.kIdEbus.U
  reqHdr.addr := addrReg
  reqHdr.strb := strbReg
  reqHdr.size := sizeReg
  reqHdr.resp := 0.U
  reqHdr.op := Mux(isWriteReg, LvdsOpcode.M_DBUS_WR_REQ, LvdsOpcode.M_DBUS_RD_REQ)
  io.reqOut.valid := false.B
  io.reqOut.bits.header := reqHdr
  io.reqOut.bits.data   := dataReg

  // Drain late stale responses in ANY state by also asserting ready when
  // pendingDrain > 0.
  io.rspIn.ready := inWait || staleResp
  val drainThisCycle = io.rspIn.fire && staleResp
  when(drainThisCycle) { pendingDrain := pendingDrain - 1.U }

  // Single helper to handle timeout-retry from either wait state.
  def onTimeoutGiveUp(): Unit = {
    val drainInc  = pendingDrain + 1.U
    val drainNext = Mux(drainThisCycle, drainInc - 1.U, drainInc)
    pendingDrain := drainNext
    timer        := 0.U
    when(retryAvail) {
      retryCnt := retryCnt + 1.U
      state    := Mux(isWriteReg, sReqWrite, sReqRead)
    } .otherwise {
      respReg := 2.U   // AXI SLVERR
      dataReg := 0.U
      state   := sDone
    }
  }

  switch(state) {
    is(sIdle) {
      when(io.ebus.dbus.valid) {
        addrReg    := io.ebus.dbus.addr
        strbReg    := io.ebus.dbus.wmask
        sizeReg    := Ctz(io.ebus.dbus.size)
        dataReg    := io.ebus.dbus.wdata
        isWriteReg := io.ebus.dbus.write
        retryCnt   := 0.U
        timer      := 0.U
        state := Mux(io.ebus.dbus.write, sReqWrite, sReqRead)
      }
    }
    is(sReqRead) {
      io.reqOut.valid := true.B
      when(io.reqOut.fire) {
        timer := 0.U
        state := sWaitRead
      }
    }
    is(sWaitRead) {
      when(io.rspIn.fire && !staleResp) {
        dataReg := io.rspIn.bits.data
        respReg := io.rspIn.bits.header.resp
        state   := sDone
      } .otherwise {
        when(timerExpired) { onTimeoutGiveUp() }
        .otherwise         { timer := timer + 1.U }
      }
    }
    is(sReqWrite) {
      io.reqOut.valid := true.B
      when(io.reqOut.fire) {
        timer := 0.U
        state := sWaitWrite
      }
    }
    is(sWaitWrite) {
      when(io.rspIn.fire && !staleResp) {
        respReg := io.rspIn.bits.header.resp
        state   := sDone
      } .otherwise {
        when(timerExpired) { onTimeoutGiveUp() }
        .otherwise         { timer := timer + 1.U }
      }
    }
    is(sDone) {
      // Present ready / fault to core for one cycle.
      io.ebus.dbus.ready := true.B
      io.ebus.fault.valid := (respReg =/= 0.U)
      state := sIdle
    }
  }
}

// ---------------------------------------------------------------------------
// Top-level chip-side LVDS adapter.
// ---------------------------------------------------------------------------
class LvdsAdapterChip(
    p: Parameters,
    useChiselAsyncQueue: Boolean = true,
) extends RawModule {
  val io = IO(new Bundle {
    // Clocks & active-low resets
    val core_clk     = Input(Clock())
    val core_aresetn = Input(Bool())
    val lvds_clk     = Input(Clock())
    val lvds_aresetn = Input(Bool())

    // LVDS PHY (lvds_clk domain)
    val tx_valid = Output(Bool())
    val tx_ready = Input(Bool())
    val tx_data  = Output(UInt(LvdsLink.kBeatBits.W))
    val rx_valid = Input(Bool())
    val rx_data  = Input(UInt(LvdsLink.kBeatBits.W))

    // Internal interfaces (core_clk domain)
    val fabricSlave = new FabricIO(p)
    val ibus        = Flipped(new IBusIO(p))
    val ebus        = Flipped(new EBusIO(p))
  })

  // ---------------------------------------------------------------------------
  // Three logical channels per direction (req / rsp / ctrl), each with its
  // own AsyncFIFO for CDC. The receive side has an additional SyncFIFO in
  // core_clk that serves as the credit pool buffer; the credit pool depth
  // (= tx_credit_init at the far side) equals the sync FIFO depth, which
  // is decoupled from the async FIFO depth (sized only for CDC throughput).
  // ---------------------------------------------------------------------------
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

  // ===========================================================================
  // LVDS clock domain: 3-way TX mux (ctrl strict > RR(req,rsp), frame-aligned)
  // and 3-way RX demux (by opcode, with per-data-channel expect-data state).
  // ===========================================================================
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
    // Per-data-channel mid-frame state: each 2-beat frame must be sent
    // contiguously (header then data) so the receiver's expect-data state
    // stays aligned. Ctrl frames are always 1 beat.
    val reqMid = RegInit(false.B)
    val rspMid = RegInit(false.B)
    val reqOp  = txAsyncReq.io.deq.bits.asTypeOf(new LvdsHeader).op
    val rspOp  = txAsyncRsp.io.deq.bits.asTypeOf(new LvdsHeader).op

    // RR pointer between {req, rsp}. 0 = req has priority next idle slot,
    // 1 = rsp has priority.
    val rrPick = RegInit(false.B) // false -> req first, true -> rsp first

    // Eligibility: a data channel can be picked if it's valid AND either
    // it's already mid-frame (must finish) or both data channels are at
    // a frame boundary (no one is mid-frame).
    val anyMid   = reqMid || rspMid
    val reqElig  = txAsyncReq.io.deq.valid && (reqMid || !anyMid)
    val rspElig  = txAsyncRsp.io.deq.valid && (rspMid || !anyMid)

    // Ctrl wins only when no data channel is mid-frame. Otherwise it
    // would interleave a 1-beat ctrl frame between header and data of a
    // data frame, breaking the receiver's expect-data state machine.
    val ctrlSel  = txAsyncCtrl.io.deq.valid && !anyMid

    // RR among data channels.
    val dataReqSel =  reqElig && (rrPick === false.B || !rspElig)
    val dataRspSel =  rspElig && (rrPick === true.B  || !reqElig)

    txSelCtrl := ctrlSel
    txSelReq  := !ctrlSel && dataReqSel
    txSelRsp  := !ctrlSel && !dataReqSel && dataRspSel

    // Update mid-frame state on data fire.
    when(txAsyncReq.io.deq.fire) {
      when(reqMid) { reqMid := false.B }
      .otherwise   { reqMid := LvdsLink.hasDataBeat(reqOp) }
    }
    when(txAsyncRsp.io.deq.fire) {
      when(rspMid) { rspMid := false.B }
      .otherwise   { rspMid := LvdsLink.hasDataBeat(rspOp) }
    }
    // Update RR pointer: flip after a complete data frame on the chosen
    // channel.
    val reqFrameDone = txAsyncReq.io.deq.fire &&
      (reqMid || !LvdsLink.hasDataBeat(reqOp))
    val rspFrameDone = txAsyncRsp.io.deq.fire &&
      (rspMid || !LvdsLink.hasDataBeat(rspOp))
    when(reqFrameDone) { rrPick := true.B  }   // next time prefer rsp
    .elsewhen(rspFrameDone) { rrPick := false.B } // next time prefer req
  }

  // RX 3-way demux. Track expect-data per data channel so a 2-beat frame
  // is delivered to the same channel for both beats. The opcode field is
  // only meaningful on header beats; data beats are raw 128-bit payloads.
  // Ctrl is always 1 beat so doesn't need expect-data.
  //
  // The LVDS RX has no `ready` line; under correct credit accounting the
  // receiver async + sync FIFO chain on each channel never overflows.
  val rxIsReq  = Wire(Bool())
  val rxIsRsp  = Wire(Bool())
  val rxIsCtrl = Wire(Bool())
  withClockAndReset(io.lvds_clk, (!io.lvds_aresetn).asAsyncReset) {
    // expect-data tracks "the next beat is a data beat for channel X".
    val reqExpectData = RegInit(false.B)
    val rspExpectData = RegInit(false.B)
    val rxHdr = io.rx_data.asTypeOf(new LvdsHeader)
    val isHdr = io.rx_valid && !reqExpectData && !rspExpectData

    val isCreditHdr = isHdr && LvdsLink.isCreditUpdate(rxHdr.op)
    val isReqHdr    = isHdr && LvdsLink.isReqOpcode(rxHdr.op)
    val isRspHdr    = isHdr && LvdsLink.isRspOpcode(rxHdr.op)

    // Route current beat. Headers on either data channel and following
    // data beats on the same channel.
    rxIsCtrl := isCreditHdr
    rxIsReq  := isReqHdr || (io.rx_valid && reqExpectData)
    rxIsRsp  := isRspHdr || (io.rx_valid && rspExpectData)

    when(io.rx_valid) {
      when(reqExpectData) {
        reqExpectData := false.B
      } .elsewhen(rspExpectData) {
        rspExpectData := false.B
      } .otherwise {
        // Header beat: arm the right channel's expect-data if it's a
        // 2-beat frame.
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

  // ===========================================================================
  // Core clock domain: sync FIFOs (credit pool) + dispatchers + engines +
  // CreditTracker.
  // ===========================================================================
  withClockAndReset(io.core_clk, (!io.core_aresetn).asAsyncReset) {

    // ---- Receiver-side sync FIFOs serve as the credit pools. They sit
    // ---- between the async FIFO (CDC pipeline buffer) and the per-channel
    // ---- consumer. tx_credit_init at the far side equals these depths.
    val rxSyncReq  = Module(new Queue(UInt(LvdsLink.kBeatBits.W),
                                      LvdsLink.kSyncReqDepth, flow = true))
    val rxSyncRsp  = Module(new Queue(UInt(LvdsLink.kBeatBits.W),
                                      LvdsLink.kSyncRspDepth, flow = true))
    val rxSyncCtrl = Module(new Queue(UInt(LvdsLink.kBeatBits.W),
                                      LvdsLink.kSyncCtrlDepth, flow = true))
    rxSyncReq.io.enq  <> rxAsyncReq.io.deq
    rxSyncRsp.io.enq  <> rxAsyncRsp.io.deq
    rxSyncCtrl.io.enq <> rxAsyncCtrl.io.deq

    // -------------------------------------------------------------------------
    // RX dispatchers: one per data channel.
    //   * req channel for the chip side carries S_RD_REQ / S_WR_REQ only;
    //     consumer = slaveEng.
    //   * rsp channel carries M_IBUS_RSP / M_DBUS_*_RSP; consumers = ibus
    //     and ebus shims. The unused `slave` output of the rsp dispatcher
    //     is tied off (its routing function never matches an opcode that
    //     appears on the rsp channel).
    // -------------------------------------------------------------------------
    val reqDisp = Module(new LvdsRxDispatcher)
    val rspDisp = Module(new LvdsRxDispatcher)
    reqDisp.io.pop <> rxSyncReq.io.deq
    rspDisp.io.pop <> rxSyncRsp.io.deq

    // -------------------------------------------------------------------------
    // Engines
    // -------------------------------------------------------------------------
    val slaveEng = Module(new LvdsSlaveEngine(p))
    val ibusShim = Module(new LvdsIBusShim(p))
    val ebusShim = Module(new LvdsEBusShim(p))

    // Wire dispatcher outputs. Unused outputs are explicitly tied off to
    // ready=true with an assertion that valid never asserts.
    reqDisp.io.slave <> slaveEng.io.cmdIn
    reqDisp.io.ibus.ready := true.B
    reqDisp.io.ebus.ready := true.B
    assert(!reqDisp.io.ibus.valid,
      "M_IBUS_RSP arrived on chip RX req channel (routing bug)")
    assert(!reqDisp.io.ebus.valid,
      "M_DBUS_*_RSP arrived on chip RX req channel (routing bug)")

    rspDisp.io.ibus  <> ibusShim.io.rspIn
    rspDisp.io.ebus  <> ebusShim.io.rspIn
    rspDisp.io.slave.ready := true.B
    assert(!rspDisp.io.slave.valid,
      "S_*_REQ arrived on chip RX rsp channel (routing bug)")

    slaveEng.io.fabric <> io.fabricSlave
    ibusShim.io.ibus   <> io.ibus
    ebusShim.io.ebus   <> io.ebus

    // -------------------------------------------------------------------------
    // TX arbiters: separate req-arb and rsp-arb in core_clk. Each produces
    // a 128-bit beat stream consumed by its dedicated async TX FIFO. Beat
    // streams from req/rsp/ctrl are then arbitrated again at the lvds-side
    // TX mux above.
    // -------------------------------------------------------------------------
    // Chip TX-req sources: ibus-req + ebus-req (chip is master).
    val reqArb = Module(new LvdsTxArbiter(numSources = 2))
    reqArb.io.sources(0) <> ibusShim.io.reqOut
    reqArb.io.sources(1) <> ebusShim.io.reqOut
    // Chip TX-rsp sources: slave-rsp (chip is slave to far-side master).
    val rspArb = Module(new LvdsTxArbiter(numSources = 1))
    rspArb.io.sources(0) <> slaveEng.io.rspOut

    // -------------------------------------------------------------------------
    // CreditTracker (dual-pool, combined credit-update frame).
    // -------------------------------------------------------------------------
    val ct = Module(new CreditTracker)
    ct.io.rxReqPop := rxSyncReq.io.deq.fire
    ct.io.rxRspPop := rxSyncRsp.io.deq.fire

    // Outgoing credit-update -> ctrl async FIFO. Independent of data
    // credit pools.
    txAsyncCtrl.io.enq <> ct.io.creditPktOut

    // Push req/rsp into their async TX FIFOs under per-channel credit.
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

    // Incoming credit-update -> CreditTracker. Sync FIFO drained 1/cycle.
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
