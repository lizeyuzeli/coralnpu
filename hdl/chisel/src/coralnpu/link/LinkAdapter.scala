// Copyright 2026 Li Zeyu <lizeyuzeli000lzy@gmail.com>
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
// LinkAdapter: bridges a pair of AXI4 endpoints (one master, one slave) over
// a 128-bit narrow inter-die link with valid/ready TX and valid-only RX.
//
// Architecture overview (see /home/lizeyu/.windsurf/plans/...c8a834.md):
//   * TX side has no per-channel sync FIFO; AXI channel `ready` is gated by
//     credit availability + arbitration grant + AsyncQueue enq.ready. AXI
//     valid/ready provides natural back-pressure.
//   * RX side has 5 sync FIFOs (one per credit-channel = AW/W/AR/B/R), each
//     of depth `syncDepth`. RX FIFO `pop` triggers a credit return.
//   * One AsyncQueue per direction (TX path: core->link_tx; RX path:
//     link_rx->core), depth `asyncDepth`. AsyncQueues do NOT participate in
//     credit accounting; their deq side is guaranteed to drain (credit
//     ensures sync FIFO has room, credit frames bypass to CreditRxApply).
//   * TX framing:
//       - AW / AR / B / CREDIT: 1 frame each.
//       - W / R: 2 frames (HEAD + TAIL); arbiter holds grant for 2 cycles.
//     CREDIT has highest TX priority to prevent credit starvation.
//   * Credit format: absolute cumulative-released counter per channel
//     (PCIe-style). Single CREDIT frame carries all 5 channels' counters,
//     so occasional CREDIT loss is self-healing.

package coralnpu.link

import chisel3._
import chisel3.util._
import freechips.rocketchip.util.{AsyncQueue, AsyncQueueParams}

import bus._
import coralnpu.Parameters
import coralnpu.link.LinkFrame._

class LinkAdapter(
    p: Parameters,
    val asyncDepth: Int = 8,
    val syncDepth: Int = 16,
) extends RawModule {
  // --- derived parameters -------------------------------------------------
  val addrW = p.axi2AddrBits
  val dataW = p.axi2DataBits
  val idW = p.axi2IdBits
  // Credit width sized to safely represent 2*Mc differences using mod 2^N
  // arithmetic. Mc=16 -> 5 bits.
  val creditWidth = log2Ceil(2 * syncDepth)
  // Initial credit = Mc; sender starts with this many slots assumed free.
  val initCredit = syncDepth
  // Watchdog & threshold for opportunistic credit packing.
  val creditUpdateThresh = 4
  val creditWatchdog = 64

  // --- top-level IO -------------------------------------------------------
  val io = IO(new Bundle {
    val core_clk = Input(Clock())
    val core_rstn = Input(AsyncReset())
    val link_tx_clk = Input(Clock())
    val link_tx_rstn = Input(AsyncReset())
    val link_rx_clk = Input(Clock())
    val link_rx_rstn = Input(AsyncReset())

    // Local AXI master endpoint port: connects to upstream's master interface;
    // here we act as the slave of the master, hence Flipped.
    //   axi_m.write.addr is Flipped(Decoupled) (we receive AW from upstream)
    //   axi_m.write.resp is Decoupled       (we return B to upstream)
    val axi_m = Flipped(new AxiMasterIO(addrW, dataW, idW))
    // Local AXI slave endpoint port: connects to upstream's slave interface;
    // here we act as the master driving into the slave, hence un-Flipped.
    //   axi_s.write.addr is Decoupled       (we issue AW to upstream)
    //   axi_s.write.resp is Flipped(Decoupled) (we receive B from upstream)
    val axi_s = new AxiMasterIO(addrW, dataW, idW)

    val link_tx = Decoupled(UInt(kBeatBits.W))
    val link_rx = Flipped(Valid(UInt(kBeatBits.W)))

    val link_err_o = Output(Bool())
  })

  // Local active-high core reset for withClockAndReset blocks below.
  private val coreRstHigh = !io.core_rstn.asBool

  // ========================================================================
  // Asynchronous FIFOs (frame transport across clock domains)
  // ========================================================================
  // safe=true gives robust reset coordination across the two clock domains.
  // depth >= 4 is recommended.
  val txAsync = Module(new AsyncQueue(
      UInt(kBeatBits.W), AsyncQueueParams(depth = asyncDepth, safe = true)))
  val rxAsync = Module(new AsyncQueue(
      UInt(kBeatBits.W), AsyncQueueParams(depth = asyncDepth, safe = true)))

  txAsync.io.enq_clock := io.core_clk
  txAsync.io.enq_reset := coreRstHigh
  txAsync.io.deq_clock := io.link_tx_clk
  txAsync.io.deq_reset := !io.link_tx_rstn.asBool

  rxAsync.io.enq_clock := io.link_rx_clk
  rxAsync.io.enq_reset := !io.link_rx_rstn.asBool
  rxAsync.io.deq_clock := io.core_clk
  rxAsync.io.deq_reset := coreRstHigh

  // Direct hookup of AsyncQueue link sides to top IO.
  io.link_tx <> txAsync.io.deq
  rxAsync.io.enq.valid := io.link_rx.valid
  rxAsync.io.enq.bits := io.link_rx.bits
  // RX has no back-pressure: enq.ready is asserted but unused.
  // (Plan §6.3: AsyncQueue depth is sized to absorb rate mismatch only,
  // never overflows under correct credit accounting.)
  // We still wire it; debug-only over-run is silent.

  // ========================================================================
  // Core-clock domain: TX/RX logic
  // ========================================================================
  withClockAndReset(io.core_clk, coreRstHigh.asAsyncReset) {
    // ---- Credit accounting (TX-sender side) ----------------------------
    // avail[c] = init + cum_acked[c] - cum_sent_local[c] (mod 2^creditWidth).
    val cumSent = RegInit(VecInit(Seq.fill(kNumCc)(0.U(creditWidth.W))))
    val cumAcked = RegInit(VecInit(Seq.fill(kNumCc)(0.U(creditWidth.W))))
    val initCreditU = initCredit.U(creditWidth.W)
    val avail = VecInit(Seq.tabulate(kNumCc) { c =>
      // Modular subtract so wrap is safe.
      val diff = (cumSent(c) - cumAcked(c))(creditWidth - 1, 0)
      val ok = diff =/= initCreditU // avail > 0 iff diff < init
      // We expose a 1-bit "has credit" flag; arbiter only needs > 0.
      ok
    })

    // ---- Credit accounting (RX-receiver side) --------------------------
    val cumReleased = RegInit(VecInit(Seq.fill(kNumCc)(0.U(creditWidth.W))))
    val lastSentCum = RegInit(VecInit(Seq.fill(kNumCc)(0.U(creditWidth.W))))
    val watchdog = RegInit(0.U(log2Ceil(creditWatchdog + 1).W))

    // ---- TX path -------------------------------------------------------
    // AXI-channel inputs, broken out for clarity. The "tx_*" channels are the
    // local-side outputs (M.AW/W/AR + S.B/S.R), which we transmit to the peer.
    val axiM = io.axi_m
    val axiS = io.axi_s
    // Local outgoing AXI channels (we generate -> peer consumes):
    //   tx_aw (M.AW), tx_w (M.W), tx_ar (M.AR), tx_b (S.B), tx_r (S.R)
    val txAw = axiM.write.addr  // Decoupled (output from master)
    val txW = axiM.write.data
    val txAr = axiM.read.addr
    val txB = axiS.write.resp   // Decoupled (S.B is output of local slave)
    val txR = axiS.read.data    // Decoupled (S.R is output of local slave)

    // ---- Frame request signals from each TX-channel packer -------------
    // Each producer drives valid+payload+flags+is_dual; arbiter picks one.
    val numProd = kNumCc
    val prodValid = Wire(Vec(numProd, Bool()))
    val prodPayload = Wire(Vec(numProd, UInt(kPayloadBits.W)))
    val prodFlags = Wire(Vec(numProd, UInt(4.W)))
    // For dual-frame channels (W=1, R=4), provide tail payload directly; the
    // arbiter sequences head/tail.
    val prodTailPayload = Wire(Vec(numProd, UInt(kPayloadBits.W)))
    val prodIsDual = Wire(Vec(numProd, Bool()))
    val prodChIdHead = Wire(Vec(numProd, UInt(ChId.WIDTH.W)))
    val prodChIdTail = Wire(Vec(numProd, UInt(ChId.WIDTH.W)))
    // AXI-side fire ack from arbiter (one per channel): asserts on the cycle
    // the AXI consumer should be ready; for dual-frame channels, this is the
    // cycle the TAIL frame enters AsyncQueue.
    val prodFire = Wire(Vec(numProd, Bool()))

    // === AW (single frame) ==============================================
    prodValid(CcId.AW) := txAw.valid && avail(CcId.AW)
    prodPayload(CcId.AW) := AwArPack.pack(txAw.bits, addrW, idW)
    prodFlags(CcId.AW) := 0.U
    prodTailPayload(CcId.AW) := 0.U
    prodIsDual(CcId.AW) := false.B
    prodChIdHead(CcId.AW) := ChId.AW.U
    prodChIdTail(CcId.AW) := ChId.AW.U
    txAw.ready := prodFire(CcId.AW)

    // === AR (single frame) ==============================================
    prodValid(CcId.AR) := txAr.valid && avail(CcId.AR)
    prodPayload(CcId.AR) := AwArPack.pack(txAr.bits, addrW, idW)
    prodFlags(CcId.AR) := 0.U
    prodTailPayload(CcId.AR) := 0.U
    prodIsDual(CcId.AR) := false.B
    prodChIdHead(CcId.AR) := ChId.AR.U
    prodChIdTail(CcId.AR) := ChId.AR.U
    txAr.ready := prodFire(CcId.AR)

    // === B (single frame) ===============================================
    prodValid(CcId.B) := txB.valid && avail(CcId.B)
    prodPayload(CcId.B) := BPack.pack(txB.bits, idW)
    prodFlags(CcId.B) := 0.U
    prodTailPayload(CcId.B) := 0.U
    prodIsDual(CcId.B) := false.B
    prodChIdHead(CcId.B) := ChId.B.U
    prodChIdTail(CcId.B) := ChId.B.U
    txB.ready := prodFire(CcId.B)

    // === W (dual frame) =================================================
    prodValid(CcId.W) := txW.valid && avail(CcId.W)
    prodPayload(CcId.W) := WPack.headPayload(txW.bits, dataW)
    prodFlags(CcId.W) := WPack.headFlags(txW.bits)
    prodTailPayload(CcId.W) := WPack.tailPayload(txW.bits, dataW)
    prodIsDual(CcId.W) := true.B
    prodChIdHead(CcId.W) := ChId.W_HEAD.U
    prodChIdTail(CcId.W) := ChId.W_TAIL.U
    txW.ready := prodFire(CcId.W)

    // === R (dual frame) =================================================
    prodValid(CcId.R) := txR.valid && avail(CcId.R)
    prodPayload(CcId.R) := RPack.headPayload(txR.bits, dataW, idW)
    prodFlags(CcId.R) := RPack.headFlags(txR.bits)
    prodTailPayload(CcId.R) := RPack.tailPayload(txR.bits, dataW)
    prodIsDual(CcId.R) := true.B
    prodChIdHead(CcId.R) := ChId.R_HEAD.U
    prodChIdTail(CcId.R) := ChId.R_TAIL.U
    txR.ready := prodFire(CcId.R)

    // ---- TxArbiter -----------------------------------------------------
    // States: IDLE -> (data: SEND_HEAD -> [optionally SEND_TAIL]) | (credit: SEND_CREDIT)
    // CREDIT has highest priority. Round-robin across data producers.

    // Credit-frame request: pending if any channel has cum_released ahead of
    // last_sent_cum by >= K, OR watchdog expired with any channel ahead.
    val anyAhead = VecInit((0 until kNumCc).map(c =>
      cumReleased(c) =/= lastSentCum(c)
    )).asUInt.orR
    val anyOverThresh = VecInit((0 until kNumCc).map { c =>
      val d = (cumReleased(c) - lastSentCum(c))(creditWidth - 1, 0)
      d >= creditUpdateThresh.U
    }).asUInt.orR
    val watchdogExpired = anyAhead && (watchdog === creditWatchdog.U)
    val creditPending = anyOverThresh || watchdogExpired

    // RR pointer for data producers. Pick first valid producer starting from
    // rrPtr (round-robin). Use a rotated valid vector + PriorityEncoder.
    val rrPtr = RegInit(0.U(log2Ceil(numProd).W))
    // NOTE: use +& (carry-preserving add) so the modulo doesn't see a
    // truncated sum. With numProd=5 (3-bit rrPtr/rrOffset), a plain `+`
    // would silently truncate 4+4=8 to 0, producing the wrong winner when
    // the round-robin pointer wraps past the highest-numbered producer.
    val rotatedValid = VecInit((0 until numProd).map { i =>
      prodValid(((rrPtr +& i.U) % numProd.U)(log2Ceil(numProd) - 1, 0))
    })
    val rrValid = rotatedValid.asUInt.orR
    val rrOffset = PriorityEncoder(rotatedValid)
    val rrCandidate = ((rrPtr +& rrOffset) % numProd.U)(log2Ceil(numProd) - 1, 0)

    // Arbiter FSM.
    object S extends ChiselEnum {
      val sIdle, sSendTail, sSendCredit = Value
    }
    val state = RegInit(S.sIdle)
    val grantCh = RegInit(0.U(log2Ceil(numProd).W))

    // Default outputs.
    txAsync.io.enq.valid := false.B
    txAsync.io.enq.bits := 0.U
    for (i <- 0 until numProd) prodFire(i) := false.B

    // ---- Frame assembly helpers (combinational) -----------------------
    def assembleFrame(chId: UInt, flags: UInt, payload: UInt): UInt = {
      val crc = Crc8.compute(crcInput(chId, flags, payload))
      LinkFrame.assemble(chId, flags, crc, payload)
    }

    // CREDIT frame payload (built from current cumReleased snapshot).
    val creditValidMask = VecInit((0 until kNumCc).map(c =>
      cumReleased(c) =/= lastSentCum(c)
    )).asUInt
    val creditPayload = CreditPack.packPayload(cumReleased, creditValidMask, creditWidth)
    val creditFrame = assembleFrame(ChId.CREDIT.U, 0.U(4.W), creditPayload)

    // Helper: try to enqueue a frame this cycle. Returns whether it fired.
    def tryEnq(frame: UInt): Bool = {
      txAsync.io.enq.valid := true.B
      txAsync.io.enq.bits := frame
      txAsync.io.enq.fire
    }

    switch(state) {
      is(S.sIdle) {
        when(creditPending) {
          // Send CREDIT (highest priority).
          val fired = tryEnq(creditFrame)
          when(fired) {
            for (c <- 0 until kNumCc) lastSentCum(c) := cumReleased(c)
            watchdog := 0.U
          }
        }.elsewhen(rrValid) {
          val ch = rrCandidate
          val pl = prodPayload(ch)
          val fl = prodFlags(ch)
          val isDual = prodIsDual(ch)
          val frame = assembleFrame(prodChIdHead(ch), fl, pl)
          val fired = tryEnq(frame)
          when(fired) {
            grantCh := ch
            // Update RR pointer (advance past granted). Use +& to avoid
            // truncation when ch == numProd-1.
            rrPtr := (ch +& 1.U) % numProd.U
            when(isDual) {
              state := S.sSendTail
            }.otherwise {
              // Single frame: AXI fire happens here.
              prodFire(ch) := true.B
              cumSent(ch) := cumSent(ch) + 1.U
            }
          }
        }
      }
      is(S.sSendTail) {
        val ch = grantCh
        val frame = assembleFrame(prodChIdTail(ch), 0.U, prodTailPayload(ch))
        val fired = tryEnq(frame)
        when(fired) {
          prodFire(ch) := true.B
          cumSent(ch) := cumSent(ch) + 1.U
          state := S.sIdle
        }
      }
      is(S.sSendCredit) {
        // Reserved for future use. Currently CREDIT sent inline in sIdle.
        state := S.sIdle
      }
    }

    // Credit watchdog: increment when any channel is ahead and we are idle.
    when(anyAhead && state === S.sIdle && !creditPending) {
      watchdog := Mux(watchdog === creditWatchdog.U, watchdog, watchdog + 1.U)
    }

    // ========================================================================
    // RX path
    // ========================================================================
    val rxFrame = rxAsync.io.deq.bits
    val rxValid = rxAsync.io.deq.valid

    val rxChId = chIdOf(rxFrame)
    val rxFlags = flagsOf(rxFrame)
    val rxCrc = crcOf(rxFrame)
    val rxPayload = payloadOf(rxFrame)
    val rxCrcExpect = Crc8.compute(crcInput(rxChId, rxFlags, rxPayload))
    val rxCrcOk = (rxCrc === rxCrcExpect)

    // Decoded type flags
    val rxIsAw = rxValid && rxCrcOk && (rxChId === ChId.AW.U)
    val rxIsAr = rxValid && rxCrcOk && (rxChId === ChId.AR.U)
    val rxIsB = rxValid && rxCrcOk && (rxChId === ChId.B.U)
    val rxIsWHead = rxValid && rxCrcOk && (rxChId === ChId.W_HEAD.U)
    val rxIsWTail = rxValid && rxCrcOk && (rxChId === ChId.W_TAIL.U)
    val rxIsRHead = rxValid && rxCrcOk && (rxChId === ChId.R_HEAD.U)
    val rxIsRTail = rxValid && rxCrcOk && (rxChId === ChId.R_TAIL.U)
    val rxIsCredit = rxValid && rxCrcOk && (rxChId === ChId.CREDIT.U)

    // CRC error: sticky.
    val crcErr = RegInit(false.B)
    when(rxValid && !rxCrcOk) { crcErr := true.B }
    io.link_err_o := crcErr

    // Per-channel RX sync FIFOs (5 of them).
    val qAw = Module(new Queue(new AxiAddress(addrW, dataW, idW), syncDepth))
    val qW = Module(new Queue(new AxiWriteData(dataW, idW), syncDepth))
    val qAr = Module(new Queue(new AxiAddress(addrW, dataW, idW), syncDepth))
    val qB = Module(new Queue(new AxiWriteResponse(idW), syncDepth))
    val qR = Module(new Queue(new AxiReadData(dataW, idW), syncDepth))

    // Default fifo enqueue: invalid.
    qAw.io.enq.valid := false.B
    qAw.io.enq.bits := AwArPack.unpack(rxPayload, addrW, idW)
    qAr.io.enq.valid := false.B
    qAr.io.enq.bits := AwArPack.unpack(rxPayload, addrW, idW)
    qB.io.enq.valid := false.B
    qB.io.enq.bits := BPack.unpack(rxPayload, idW)
    qW.io.enq.valid := false.B
    qW.io.enq.bits := DontCare
    qR.io.enq.valid := false.B
    qR.io.enq.bits := DontCare

    // Enqueue paths for single-frame channels.
    when(rxIsAw) {
      qAw.io.enq.valid := true.B
      qAw.io.enq.bits := AwArPack.unpack(rxPayload, addrW, idW)
    }
    when(rxIsAr) {
      qAr.io.enq.valid := true.B
      qAr.io.enq.bits := AwArPack.unpack(rxPayload, addrW, idW)
    }
    when(rxIsB) {
      qB.io.enq.valid := true.B
      qB.io.enq.bits := BPack.unpack(rxPayload, idW)
    }

    // W head/tail assembly.
    val wHeadHave = RegInit(false.B)
    val wHeadPayload = RegInit(0.U(kPayloadBits.W))
    val wHeadFlags = RegInit(0.U(4.W))
    when(rxIsWHead) {
      // If we already had a head waiting, it's a protocol error (should not
      // happen when peer sends head/tail back-to-back). Take the new one.
      wHeadHave := true.B
      wHeadPayload := rxPayload
      wHeadFlags := rxFlags
    }
    when(rxIsWTail) {
      // Combine and enqueue.
      qW.io.enq.valid := true.B
      qW.io.enq.bits := WPack.assemble(wHeadPayload, wHeadFlags, rxPayload, dataW, idW)
      wHeadHave := false.B
    }

    // R head/tail assembly.
    val rHeadHave = RegInit(false.B)
    val rHeadPayload = RegInit(0.U(kPayloadBits.W))
    val rHeadFlags = RegInit(0.U(4.W))
    when(rxIsRHead) {
      rHeadHave := true.B
      rHeadPayload := rxPayload
      rHeadFlags := rxFlags
    }
    when(rxIsRTail) {
      qR.io.enq.valid := true.B
      qR.io.enq.bits := RPack.assemble(rHeadPayload, rHeadFlags, rxPayload, dataW, idW)
      rHeadHave := false.B
    }

    // Always accept frames from the AsyncQueue (it never stalls under correct
    // credit, see plan §6.3). Track frames we don't decode here.
    rxAsync.io.deq.ready := true.B

    // CREDIT frame: update sender's cum_acked.
    when(rxIsCredit) {
      val (cum, _mask) = CreditPack.unpackPayload(rxPayload, creditWidth)
      // Always update cum_acked to the new absolute counter; mask is advisory.
      for (c <- 0 until kNumCc) cumAcked(c) := cum(c)
    }

    // ---- AXI consumer outputs (RX FIFO -> downstream AXI ports) ---------
    // Note: both sides of each connection have the same direction (both are
    // "producer" Decoupled), so we drive explicitly rather than using `<>`.

    // Local AXI master interface: drive B/R back toward upstream master.
    axiM.write.resp.valid := qB.io.deq.valid
    axiM.write.resp.bits := qB.io.deq.bits
    qB.io.deq.ready := axiM.write.resp.ready

    axiM.read.data.valid := qR.io.deq.valid
    axiM.read.data.bits := qR.io.deq.bits
    qR.io.deq.ready := axiM.read.data.ready

    // Local AXI slave interface: drive AW/W/AR toward upstream slave.
    axiS.write.addr.valid := qAw.io.deq.valid
    axiS.write.addr.bits := qAw.io.deq.bits
    qAw.io.deq.ready := axiS.write.addr.ready

    axiS.write.data.valid := qW.io.deq.valid
    axiS.write.data.bits := qW.io.deq.bits
    qW.io.deq.ready := axiS.write.data.ready

    axiS.read.addr.valid := qAr.io.deq.valid
    axiS.read.addr.bits := qAr.io.deq.bits
    qAr.io.deq.ready := axiS.read.addr.ready

    // ---- Credit return on RX FIFO pop -----------------------------------
    when(qAw.io.deq.fire) { cumReleased(CcId.AW) := cumReleased(CcId.AW) + 1.U }
    when(qW.io.deq.fire)  { cumReleased(CcId.W)  := cumReleased(CcId.W)  + 1.U }
    when(qAr.io.deq.fire) { cumReleased(CcId.AR) := cumReleased(CcId.AR) + 1.U }
    when(qB.io.deq.fire)  { cumReleased(CcId.B)  := cumReleased(CcId.B)  + 1.U }
    when(qR.io.deq.fire)  { cumReleased(CcId.R)  := cumReleased(CcId.R)  + 1.U }
  }
}
