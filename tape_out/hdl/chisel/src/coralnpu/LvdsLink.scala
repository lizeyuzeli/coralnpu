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

// =============================================================================
// LvdsLink: shared definitions for the chip <-> FPGA LVDS bridge that replaces
// the AXI master/slave ports of CoreAxi for the tape-out. The chip side does
// not carry full AXI; instead the LVDS link transports "fabric/ibus/ebus
// transactions" directly. Full AXI conversion is reconstructed on the FPGA
// side using the existing AxiSlave / IBus2Axi / DBus2Axi modules.
//
// Wire format:
//   - Each LVDS beat is 128 bits (matches lsuDataBits = axi2DataBits).
//   - A "frame" is either 1 beat (header only) or 2 beats (header + data),
//     determined by the opcode in the header beat. Frames are transmitted
//     in order, so the receiver only needs a 1-bit "expect_data" state.
//   - Headers are packed into 128 bits (see LvdsHeader bundle).
//   - Data beats are raw 128-bit payloads (read data, write data).
//
// Clock domain crossing & flow control:
//   - Three logical channels per direction (`req`, `rsp`, `ctrl`), each
//     with its own buffering and credit pool. Splitting `req` from `rsp`
//     avoids the classical "request blocks response so response can't
//     drain so request can't be processed" virtual-channel deadlock; the
//     `ctrl` channel separates credit-update traffic from data so it
//     cannot be locked out by data back-pressure.
//   - Per-channel buffer split:
//       sender  : AsyncFIFO (CDC only, depth = kAsync*Depth)
//       receiver: AsyncFIFO (CDC only) -> SyncFIFO (credit pool,
//                                                   depth = kSync*Depth)
//     `tx_credit_init = kSync*Depth`. The receiver's async FIFO acts as a
//     small CDC pipeline buffer; correctness only requires
//     `kAsync*Depth + kSync*Depth >= kSync*Depth` (always true), and
//     under credit accounting no path can overflow.
//   - Flow control is PCIe-DLLP style: receiver tracks cumulative pop
//     counters per channel (`kCreditCounterBits` wide, mod 2^N). A single
//     M_CREDIT_UPDATE frame carries the latest req+rsp consumed counts
//     simultaneously, emitted when either channel hits `kCreditThreshold`
//     pending or a `kCreditTimerMax` cycle timer expires with any
//     pending. The receiver applies `delta = (new - last_seen) mod 2^N`
//     to the corresponding tx_credit pool. Cumulative semantics make the
//     protocol robust to dropped/out-of-window updates -- the next frame
//     re-synchronizes both pools.
// =============================================================================

package coralnpu

import chisel3._
import chisel3.util._
import freechips.rocketchip.util.{AsyncQueue, AsyncQueueParams}

object LvdsOpcode extends ChiselEnum {
  // FPGA -> chip (carried on chip's RX direction)
  val S_RD_REQ      = Value(0.U(5.W))  // header only: addr
  val S_WR_REQ      = Value(1.U(5.W))  // header + data: addr+strb, then data
  val M_IBUS_RSP    = Value(2.U(5.W))  // header + data: id+resp, then data
  val M_DBUS_RD_RSP = Value(3.U(5.W))  // header + data: id+resp, then data
  val M_DBUS_WR_RSP = Value(4.U(5.W))  // header only: id+resp

  // chip -> FPGA (carried on chip's TX direction)
  val S_RD_RSP_OK   = Value(5.U(5.W))  // header + data: status, then data
  val S_RD_RSP_ERR  = Value(6.U(5.W))  // header only: status
  val S_WR_RSP      = Value(7.U(5.W))  // header only: status
  val M_IBUS_REQ    = Value(8.U(5.W))  // header only: addr+id
  val M_DBUS_RD_REQ = Value(9.U(5.W))  // header only: addr+id+size
  val M_DBUS_WR_REQ = Value(10.U(5.W)) // header + data: addr+strb+id+size, then data

  // Bidirectional control plane: cumulative credit update. Header-only,
  // payload is `LvdsHeader.id` = local rx_consumed_total (mod 2^N). Travels
  // on a dedicated ctrl FIFO that never shares the data credit pool, so it
  // can always be sent regardless of data-path back-pressure.
  val M_CREDIT_UPDATE = Value(11.U(5.W))
}

object LvdsLink {
  val kBeatBits: Int = 128 // bits per LVDS beat (matches axi2DataBits)

  // ---- Async (CDC) FIFO depths ----
  // Sized for sustained 1-beat/cycle throughput across the gray-code
  // pointer sync (sync stages = 3 -> round-trip ~ 6 cycles, so depth 8
  // keeps sustained throughput close to 100%). Ctrl traffic is rate-
  // limited and tolerates lower BW, so its async FIFO can be smaller.
  val kAsyncDataDepthLog2: Int = 3   // -> 8 entries
  val kAsyncDataDepth: Int     = 1 << kAsyncDataDepthLog2
  val kAsyncCtrlDepthLog2: Int = 2   // -> 4 entries
  val kAsyncCtrlDepth: Int     = 1 << kAsyncCtrlDepthLog2

  // ---- Sync (credit-pool) FIFO depths ----
  // The receiver's sync FIFO IS the credit pool. tx_credit_init equals
  // the corresponding sync depth. Async FIFOs add transit margin but do
  // not enlarge the credit pool.
  val kSyncReqDepth: Int  = 16
  val kSyncRspDepth: Int  = 16
  val kSyncCtrlDepth: Int = 2

  val kReqCreditInit: Int = kSyncReqDepth
  val kRspCreditInit: Int = kSyncRspDepth

  // Cumulative rx-consumed counter width (mod 2^N). 8 bits comfortably
  // exceeds the maximum possible delta between successive credit updates
  // (bounded by sync FIFO depth + a few cycles of margin).
  val kCreditCounterBits: Int = 8

  // Credit-update emission policy (PCIe DLLP style). Either channel
  // crossing threshold OR a timer expiry with any pending forces a frame.
  val kCreditThreshold: Int = kSyncReqDepth / 2  // 8 (assume req == rsp depth)
  val kCreditTimerMax: Int  = 64

  // ---- Master-side transaction timeout / retry ----
  // Each master-side shim/agent (chip ibus+ebus, FPGA slave) starts a
  // timer when it transitions into a "waiting for response" state. If
  // the response does not arrive within `kTxTimeoutCycles`, it gives up
  // on the outstanding request and either:
  //   * re-sends the same frame (counts as 1 retry), up to
  //     `kTxMaxRetries` retries; or
  //   * if all retries are exhausted, synthesizes a SLVERR-style fault
  //     response to the upstream consumer.
  // A small `pendingDrain` counter in the same module silently absorbs
  // any late-arriving responses that correspond to requests we already
  // gave up on (timed-out previous attempts whose responses still come
  // back over the wire), so they aren't misattributed to a subsequent
  // transaction.
  val kTxTimeoutCycles: Int = 4096
  val kTxMaxRetries: Int   = 3
  val kTxTimerWidth: Int   = log2Ceil(kTxTimeoutCycles + 1)
  val kTxRetryWidth: Int   = log2Ceil(kTxMaxRetries + 2) // +1 for "max" sentinel value, +1 for log2Ceil rounding

  // Width used for tx_credit registers (must hold init value exactly).
  val kReqCreditWidth: Int = log2Ceil(kReqCreditInit + 1)
  val kRspCreditWidth: Int = log2Ceil(kRspCreditInit + 1)

  // Sub-source IDs used inside LvdsHeader.id
  // For master path: 0=ebus/dbus, 1=ibus (matches existing CoreAxi convention)
  val kIdEbus: Int = 0
  val kIdIbus: Int = 1
  // For slave path: id is unused (single in-flight, in-order)

  // Returns true when the given opcode carries a data beat after its header.
  // M_CREDIT_UPDATE is header-only.
  def hasDataBeat(op: LvdsOpcode.Type): Bool = {
    op.isOneOf(
      LvdsOpcode.S_WR_REQ,
      LvdsOpcode.M_IBUS_RSP,
      LvdsOpcode.M_DBUS_RD_RSP,
      LvdsOpcode.S_RD_RSP_OK,
      LvdsOpcode.M_DBUS_WR_REQ,
    )
  }

  // Channel classification for the lvds-domain TX/RX mux/demux.
  def isCreditUpdate(op: LvdsOpcode.Type): Bool =
    op === LvdsOpcode.M_CREDIT_UPDATE

  // Request-class opcodes (from local masters to remote slaves).
  def isReqOpcode(op: LvdsOpcode.Type): Bool = op.isOneOf(
    LvdsOpcode.S_RD_REQ,
    LvdsOpcode.S_WR_REQ,
    LvdsOpcode.M_IBUS_REQ,
    LvdsOpcode.M_DBUS_RD_REQ,
    LvdsOpcode.M_DBUS_WR_REQ,
  )

  // Response-class opcodes (from local slaves to remote masters).
  def isRspOpcode(op: LvdsOpcode.Type): Bool = op.isOneOf(
    LvdsOpcode.S_RD_RSP_OK,
    LvdsOpcode.S_RD_RSP_ERR,
    LvdsOpcode.S_WR_RSP,
    LvdsOpcode.M_IBUS_RSP,
    LvdsOpcode.M_DBUS_RD_RSP,
    LvdsOpcode.M_DBUS_WR_RSP,
  )
}

// Header beat layout (packed into 128 bits). Reserved bits ensure the full
// width is 128 even when fewer fields are used.
class LvdsHeader extends Bundle {
  val resv  = UInt(62.W)
  val resp  = UInt(2.W)   // For RSP frames: 0=OK, otherwise error code (axi resp).
  val size  = UInt(3.W)   // For dbus: log2(num bytes)
  val strb  = UInt(16.W)  // Byte strobe for 128-bit data
  val addr  = UInt(32.W)
  val id    = UInt(8.W)   // 0=ebus, 1=ibus, else slave (unused)
  val op    = LvdsOpcode()  // 5 bits
}

object LvdsHeader {
  def width: Int = 128
  // The bundle width must be exactly 128 bits.
  // 5+8+32+16+3+2+62 = 128. Verified.
}

// BlackBox wrapping the AsyncFIFO_RTL provided in
// tape_out/hdl/verilog/asyn_fifo.sv. Width and depth are compile-time params.
//
// Important interface note: this is a *standard* (non-FWFT) FIFO. `rd_data`
// reflects the entry indexed by `rdptr` only AFTER an `rd_en` pulse: the
// consumer asserts `rd_en` for one cycle and the popped data appears on
// `rd_data` on the FOLLOWING cycle. Drivers that need first-word-fall-
// through semantics (data and valid simultaneous on the same cycle) should
// wrap this with `AsyncFIFOFwft` below, which provides a 1-deep prefetch
// skid register on the read side.
class AsyncFIFOBB(dataWidth: Int, addrWidth: Int) extends BlackBox(Map(
  "DATA_WIDTH" -> dataWidth,
  "ADDR_WIDTH" -> addrWidth,
)) with HasBlackBoxResource {
  override val desiredName = "AsyncFIFO_RTL"
  val io = IO(new Bundle {
    val wrclk      = Input(Clock())
    val rstn_wrclk = Input(Bool())
    val wr_data    = Input(UInt(dataWidth.W))
    val wr_en      = Input(Bool())
    val full       = Output(Bool())

    val rdclk      = Input(Clock())
    val rstn_rdclk = Input(Bool())
    val rd_data    = Output(UInt(dataWidth.W))
    val rd_en      = Input(Bool())
    val empty      = Output(Bool())
  })
  addResource("asyn_fifo.sv")
  addResource("dp_sram.sv")
}

// First-word-fall-through (FWFT) wrapper around AsyncFIFOBB. Exposes a
// Decoupled enq port in the wrclk domain and a Decoupled deq port in the
// rdclk domain whose `valid` and `bits` are presented simultaneously.
//
// The underlying AsyncFIFO is a "standard" FIFO whose `rd_data` is only
// updated the cycle AFTER `rd_en` is pulsed; we therefore prefetch one
// entry into a local register (`buf`) so that the consumer can see valid
// data the same cycle `valid` rises. Back-to-back dequeues then take 2
// cycles per beat (1 to ack the outgoing entry, 1 to refill `buf`), which
// is fine for 128b LVDS link rates.
class AsyncFIFOFwft(dataWidth: Int, addrWidth: Int) extends RawModule {
  val io = IO(new Bundle {
    val wrclk   = Input(Clock())
    val wr_rstn = Input(Bool())
    val rdclk   = Input(Clock())
    val rd_rstn = Input(Bool())

    val enq = Flipped(Decoupled(UInt(dataWidth.W)))
    val deq = Decoupled(UInt(dataWidth.W))
  })

  val raw = Module(new AsyncFIFOBB(dataWidth, addrWidth))
  raw.io.wrclk      := io.wrclk
  raw.io.rstn_wrclk := io.wr_rstn
  raw.io.rdclk      := io.rdclk
  raw.io.rstn_rdclk := io.rd_rstn

  // Enq side: trivial wiring in the wrclk domain.
  io.enq.ready := !raw.io.full
  raw.io.wr_en   := io.enq.fire
  raw.io.wr_data := io.enq.bits

  // Deq side: 1-deep prefetch skid register.
  withClockAndReset(io.rdclk, (!io.rd_rstn).asAsyncReset) {
    val hasBuf     = RegInit(false.B)
    val popPending = RegInit(false.B)
    val buf        = Reg(UInt(dataWidth.W))

    val bufFireNow     = io.deq.fire
    val bufWillBeEmpty = !hasBuf || bufFireNow
    val canPop         = bufWillBeEmpty && !raw.io.empty && !popPending
    raw.io.rd_en := canPop

    when(canPop) {
      popPending := true.B
    }

    when(popPending) {
      // The cycle after rd_en, raw.rd_data has the popped value.
      buf        := raw.io.rd_data
      hasBuf     := true.B
      popPending := false.B
    } .elsewhen(bufFireNow) {
      hasBuf := false.B
    }

    io.deq.valid := hasBuf
    io.deq.bits  := buf
  }
}

// Selectable async FIFO with the same external IO as `AsyncFIFOFwft`.
// When `useChisel=false` (default), wraps the SV BlackBox `AsyncFIFOFwft`
// (intended for tape-out where the underlying RAM may eventually be replaced
// with a dual-port SRAM IP). When `useChisel=true`, wraps rocket-chip's
// `AsyncQueue` (gray-coded ptr + 2FF sync, safe=true). The latter avoids
// dependency on the hand-written SV FIFO and is preferred when no SRAM IP
// substitution is needed (e.g. FPGA, or chip-area-permissive flows).
class LvdsAsyncFifo(useChisel: Boolean, dataWidth: Int, addrWidth: Int)
    extends RawModule {
  override val desiredName =
    if (useChisel) s"LvdsAsyncFifoQ_${dataWidth}_${addrWidth}"
    else s"LvdsAsyncFifoBB_${dataWidth}_${addrWidth}"

  val io = IO(new Bundle {
    val wrclk   = Input(Clock())
    val wr_rstn = Input(Bool())
    val rdclk   = Input(Clock())
    val rd_rstn = Input(Bool())
    val enq = Flipped(Decoupled(UInt(dataWidth.W)))
    val deq = Decoupled(UInt(dataWidth.W))
  })

  if (useChisel) {
    // rocket-chip AsyncQueue. depth must be >=2 in safe mode; matches our
    // BB FIFO's depth = 1 << addrWidth.
    val q = Module(new AsyncQueue(
      UInt(dataWidth.W),
      AsyncQueueParams(depth = 1 << addrWidth, sync = 3, safe = true),
    ))
    q.io.enq_clock := io.wrclk
    q.io.enq_reset := !io.wr_rstn
    q.io.deq_clock := io.rdclk
    q.io.deq_reset := !io.rd_rstn
    q.io.enq      <> io.enq
    io.deq        <> q.io.deq
  } else {
    val raw = Module(new AsyncFIFOFwft(dataWidth, addrWidth))
    raw.io.wrclk   := io.wrclk
    raw.io.wr_rstn := io.wr_rstn
    raw.io.rdclk   := io.rdclk
    raw.io.rd_rstn := io.rd_rstn
    raw.io.enq    <> io.enq
    io.deq        <> raw.io.deq
  }
}

// Toggle + 2-FF synchronizer for sending a 1-bit pulse across clock domains.
// Sender pulses `pulse_in` for one src-clk cycle; receiver gets `pulse_out`
// for one dst-clk cycle.
class PulseSync extends RawModule {
  val io = IO(new Bundle {
    val src_clk  = Input(Clock())
    val src_rstn = Input(AsyncReset())
    val pulse_in = Input(Bool())
    val dst_clk  = Input(Clock())
    val dst_rstn = Input(AsyncReset())
    val pulse_out = Output(Bool())
  })

  val toggleReg = withClockAndReset(io.src_clk, io.src_rstn) {
    RegInit(false.B)
  }
  withClockAndReset(io.src_clk, io.src_rstn) {
    when (io.pulse_in) { toggleReg := !toggleReg }
  }

  val sync0 = withClockAndReset(io.dst_clk, io.dst_rstn) { RegNext(toggleReg, false.B) }
  val sync1 = withClockAndReset(io.dst_clk, io.dst_rstn) { RegNext(sync0, false.B) }
  val sync2 = withClockAndReset(io.dst_clk, io.dst_rstn) { RegNext(sync1, false.B) }

  io.pulse_out := sync1 ^ sync2
}

// ---------------------------------------------------------------------------
// Bundle representing a parsed M_CREDIT_UPDATE payload (extracted from a
// LvdsHeader). Carries cumulative consumed counts for both the request and
// response channels simultaneously.
//   - reqConsumed: packed in LvdsHeader.id (8 bits)
//   - rspConsumed: packed in low 8 bits of LvdsHeader.addr
// ---------------------------------------------------------------------------
class CreditUpdatePayload extends Bundle {
  val reqConsumed = UInt(LvdsLink.kCreditCounterBits.W)
  val rspConsumed = UInt(LvdsLink.kCreditCounterBits.W)
}

// ---------------------------------------------------------------------------
// CreditTracker: PCIe-DLLP-style cumulative-counter credit flow control.
// Dual-pool variant: independently tracks the request and response channels
// and emits a single combined M_CREDIT_UPDATE frame carrying both counters.
//
// Runs entirely in the local core_clk domain. Used identically on both the
// chip side and the FPGA side of the LVDS link.
//
// Responsibilities (per channel: one for req, one for rsp):
//   * Track `rxConsumedTotal_X`: free-running mod-2^N counter of beats
//     popped from the local channel-X data sync FIFO (one pulse per pop).
//   * Maintain `txCredit_X`: number of free entries in the far-side
//     channel-X RX sync FIFO. Decrement by 1 per local enq fire; expose
//     `txCreditAvail_X = (txCredit_X != 0)` to gate the data TX path.
//   * Apply incoming credit updates: `delta_X = (newConsumed_X - lastSeen_X)
//     mod 2^N`, add to txCredit_X.
//
// Emit policy (combined frame):
//   * Fires when EITHER channel's pendingDelta >= kCreditThreshold,
//     OR when a kCreditTimerMax-cycle timer expires with EITHER channel
//     having pending > 0.
//   * The frame carries BOTH channels' rxConsumedTotal values, so a single
//     update simultaneously refreshes both pools at the receiver.
//
// Key invariant: credit frames travel on a dedicated ctrl channel (caller's
// responsibility), so they never consume the data credit pool. This avoids
// the "0 credit -> cannot return credit -> deadlock" trap.
// ---------------------------------------------------------------------------
class CreditTracker extends Module {
  val io = IO(new Bundle {
    // Local rx-pop pulses (1 cycle per beat popped from local sync FIFO).
    val rxReqPop = Input(Bool())
    val rxRspPop = Input(Bool())

    // Incoming combined credit-update payload from far side. `valid` is
    // asserted for the single cycle we pop a M_CREDIT_UPDATE frame from
    // the local ctrl sync FIFO.
    val creditUpdIn = Flipped(Valid(new CreditUpdatePayload))

    // Local enq-fire pulses on the local data TX async FIFO (decrement
    // the corresponding tx_credit pool by 1).
    val txReqPush = Input(Bool())
    val txRspPush = Input(Bool())

    // Per-channel credit-available gate exposed to the TX path.
    val txReqCreditAvail = Output(Bool())
    val txRspCreditAvail = Output(Bool())

    // Outgoing combined credit-update frame, fed into the local ctrl-TX
    // async FIFO. The ctrl path MUST be physically separate from the
    // data credit pools.
    val creditPktOut = Decoupled(UInt(LvdsLink.kBeatBits.W))
  })

  // ---------------------------------------------------------------------
  // Local rxConsumed counters (one per data channel).
  // ---------------------------------------------------------------------
  val rxReqConsumed = RegInit(0.U(LvdsLink.kCreditCounterBits.W))
  val rxRspConsumed = RegInit(0.U(LvdsLink.kCreditCounterBits.W))
  when(io.rxReqPop) { rxReqConsumed := rxReqConsumed + 1.U }
  when(io.rxRspPop) { rxRspConsumed := rxRspConsumed + 1.U }

  // ---------------------------------------------------------------------
  // Pending-since-last-sent (mod-2^N).
  // ---------------------------------------------------------------------
  val lastSentReq = RegInit(0.U(LvdsLink.kCreditCounterBits.W))
  val lastSentRsp = RegInit(0.U(LvdsLink.kCreditCounterBits.W))
  val reqPending = rxReqConsumed - lastSentReq
  val rspPending = rxRspConsumed - lastSentRsp
  val anyPending = (reqPending =/= 0.U) || (rspPending =/= 0.U)
  val thresholdHit = (reqPending >= LvdsLink.kCreditThreshold.U) ||
                     (rspPending >= LvdsLink.kCreditThreshold.U)

  // ---------------------------------------------------------------------
  // Single timer covering both channels: ticks while anything is pending
  // and resets on each emitted update.
  // ---------------------------------------------------------------------
  val timerWidth = log2Ceil(LvdsLink.kCreditTimerMax + 1)
  val timer = RegInit(0.U(timerWidth.W))
  val timerExpired = timer === (LvdsLink.kCreditTimerMax - 1).U

  val wantSend = anyPending && (thresholdHit || timerExpired)

  // Pack the header for the credit-update frame.
  val hdr = Wire(new LvdsHeader)
  hdr.resv := 0.U
  hdr.resp := 0.U
  hdr.size := 0.U
  hdr.strb := 0.U
  // Pack rspConsumed in the low 8 bits of addr; the rest of addr is unused
  // for credit frames so we zero it for clean simulation traces.
  hdr.addr := rxRspConsumed
  hdr.id   := rxReqConsumed
  hdr.op   := LvdsOpcode.M_CREDIT_UPDATE
  io.creditPktOut.valid := wantSend
  io.creditPktOut.bits  := hdr.asUInt

  when(io.creditPktOut.fire) {
    lastSentReq := rxReqConsumed
    lastSentRsp := rxRspConsumed
    timer       := 0.U
  } .elsewhen(anyPending) {
    when(!timerExpired) { timer := timer + 1.U }
  } .otherwise {
    timer := 0.U
  }

  // ---------------------------------------------------------------------
  // Per-channel txCredit pool: incoming update adds the wraparound delta;
  // local push subtracts 1. Combined update + push in the same cycle is
  // handled via a (width+1)-bit signed-extension delta. Truncating add
  // gives mod-2^width arithmetic; under correct credit accounting the
  // pool stays within [0, kInit] so no wrap is observed.
  // ---------------------------------------------------------------------
  val txReqCredit  = RegInit(LvdsLink.kReqCreditInit.U(LvdsLink.kReqCreditWidth.W))
  val lastSeenReq  = RegInit(0.U(LvdsLink.kCreditCounterBits.W))
  val txRspCredit  = RegInit(LvdsLink.kRspCreditInit.U(LvdsLink.kRspCreditWidth.W))
  val lastSeenRsp  = RegInit(0.U(LvdsLink.kCreditCounterBits.W))

  val updFire = io.creditUpdIn.valid

  def applyDelta(
      pool: UInt, width: Int,
      newConsumed: UInt, lastSeen: UInt,
      pushFire: Bool, updIn: Bool,
  ): UInt = {
    val freedFull = newConsumed - lastSeen
    val freed     = freedFull(width - 1, 0)
    val deltaWide = WireDefault(0.U((width + 1).W))
    when(updIn && pushFire) {
      deltaWide := (Cat(0.U(1.W), freed) - 1.U)
    } .elsewhen(updIn) {
      deltaWide := Cat(0.U(1.W), freed)
    } .elsewhen(pushFire) {
      deltaWide := ((BigInt(1) << (width + 1)) - 1).U
    }
    (Cat(0.U(1.W), pool) + deltaWide)(width - 1, 0)
  }

  txReqCredit := applyDelta(txReqCredit, LvdsLink.kReqCreditWidth,
                            io.creditUpdIn.bits.reqConsumed, lastSeenReq,
                            io.txReqPush, updFire)
  txRspCredit := applyDelta(txRspCredit, LvdsLink.kRspCreditWidth,
                            io.creditUpdIn.bits.rspConsumed, lastSeenRsp,
                            io.txRspPush, updFire)
  when(updFire) {
    lastSeenReq := io.creditUpdIn.bits.reqConsumed
    lastSeenRsp := io.creditUpdIn.bits.rspConsumed
  }

  io.txReqCreditAvail := txReqCredit =/= 0.U
  io.txRspCreditAvail := txRspCredit =/= 0.U
}

