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

package coralnpu.link

import chisel3._
import chisel3.util._

import bus._

/** Link frame layout (128 bits).
  *
  *   [127:124] ch_id  (4b)   — frame type, see [[ChId]]
  *   [123:120] flags  (4b)   — channel-specific flags (e.g. AXI 'last')
  *   [119:112] crc    (8b)   — CRC-8 over [127:120] || [111:0]
  *   [111:0]   payload (112b) — channel-specific
  */
object LinkFrame {
  val kBeatBits   = 128
  val kPayloadLo  = 0
  val kPayloadHi  = 111  // [111:0] = payload (112 bit)
  val kCrcLo      = 112
  val kCrcHi      = 119  // [119:112] = CRC-8
  val kFlagsLo    = 120
  val kFlagsHi    = 123  // [123:120]
  val kChIdLo     = 124
  val kChIdHi     = 127  // [127:124]

  val kPayloadBits = 112

  /** Channel IDs used in [[ChIdLo..ChIdHi]]. */
  object ChId {
    val AW     = 0
    val AR     = 1
    val B      = 2
    val W_HEAD = 3
    val W_TAIL = 4
    val R_HEAD = 5
    val R_TAIL = 6
    val CREDIT = 7
    val WIDTH  = 4
  }

  /** Credit channel index (5 logical AXI channels). Same numbering on both
    * sides; sender's TX[c] credit consumes peer receiver's sync FIFO[c].
    */
  object CcId {
    val AW = 0
    val W  = 1
    val AR = 2
    val B  = 3
    val R  = 4
    val N  = 5
  }

  /** Number of separately-credited virtual channels = 5. */
  val kNumCc = CcId.N

  /** Number of physical TX channel slots fed into TxArbiter (5 data + credit). */
  val kNumTxData = kNumCc

  /** Frame type used between RxUnpacker and credit/sync FIFO sinks. */
  class FrameDecoded(payloadBits: Int = kPayloadBits) extends Bundle {
    val ch_id   = UInt(ChId.WIDTH.W)
    val flags   = UInt(4.W)
    val payload = UInt(payloadBits.W)
  }

  /** Build a 128b frame from its parts. */
  def assemble(chId: UInt, flags: UInt, crc: UInt, payload: UInt): UInt = {
    require(payload.getWidth == kPayloadBits, s"payload must be ${kPayloadBits}b, got ${payload.getWidth}")
    Cat(chId(ChId.WIDTH-1, 0), flags(3, 0), crc(7, 0), payload)
  }

  /** Pre-CRC bit vector (everything except crc field), used as CRC input. */
  def crcInput(chId: UInt, flags: UInt, payload: UInt): UInt = {
    Cat(chId(ChId.WIDTH-1, 0), flags(3, 0), payload)
  }

  def chIdOf(frame: UInt): UInt   = frame(kChIdHi, kChIdLo)
  def flagsOf(frame: UInt): UInt  = frame(kFlagsHi, kFlagsLo)
  def crcOf(frame: UInt): UInt    = frame(kCrcHi, kCrcLo)
  def payloadOf(frame: UInt): UInt = frame(kPayloadHi, kPayloadLo)
}

/** AxiAddress packed into the 112b payload.
  *
  * Order (LSB-first): addr(addrW) | prot(3) | id(idW) | len(8) | size(3) |
  *                    burst(2) | lock(1) | cache(4) | qos(4) | region(4)
  */
object AwArPack {
  def width(addrW: Int, idW: Int): Int =
    addrW + 3 + idW + 8 + 3 + 2 + 1 + 4 + 4 + 4

  def pack(a: AxiAddress, addrW: Int, idW: Int): UInt = {
    val w = width(addrW, idW)
    require(w <= LinkFrame.kPayloadBits, s"AW/AR payload $w bits exceeds frame")
    val raw = Cat(
      a.region(3, 0),
      a.qos(3, 0),
      a.cache(3, 0),
      a.lock(0),
      a.burst(1, 0),
      a.size(2, 0),
      a.len(7, 0),
      a.id(idW - 1, 0),
      a.prot(2, 0),
      a.addr(addrW - 1, 0),
    )
    // Zero-extend up to 112b.
    Cat(0.U((LinkFrame.kPayloadBits - w).W), raw)
  }

  def unpack(payload: UInt, addrW: Int, idW: Int): AxiAddress = {
    val a = Wire(new AxiAddress(addrW, /*dataWidthBits=*/ 8, idW))
    var off = 0
    a.addr := payload(off + addrW - 1, off); off += addrW
    a.prot := payload(off + 3 - 1, off); off += 3
    a.id := payload(off + idW - 1, off); off += idW
    a.len := payload(off + 8 - 1, off); off += 8
    a.size := payload(off + 3 - 1, off); off += 3
    a.burst := payload(off + 2 - 1, off); off += 2
    a.lock := payload(off + 1 - 1, off); off += 1
    a.cache := payload(off + 4 - 1, off); off += 4
    a.qos := payload(off + 4 - 1, off); off += 4
    a.region := payload(off + 4 - 1, off); off += 4
    a
  }
}

/** AxiWriteResponse packed into the 112b payload.
  *  id(idW) | resp(2)
  */
object BPack {
  def width(idW: Int): Int = idW + 2

  def pack(b: AxiWriteResponse, idW: Int): UInt = {
    val raw = Cat(b.resp(1, 0), b.id(idW - 1, 0))
    Cat(0.U((LinkFrame.kPayloadBits - width(idW)).W), raw)
  }

  def unpack(payload: UInt, idW: Int): AxiWriteResponse = {
    val r = Wire(new AxiWriteResponse(idW))
    r.id := payload(idW - 1, 0)
    r.resp := payload(idW + 1, idW)
    r
  }
}

/** AxiWriteData (data 128b + last + strb 16b) split across W_HEAD + W_TAIL.
  *
  * W_HEAD payload: data[63:0] (64) | strb (dataW/8)
  * W_TAIL payload: data[dataW-1:64] (= dataW-64)
  * 'last' bit lives in flags[0] of W_HEAD.
  *
  * Assumes dataW > 64; for the verif target dataW=128.
  */
object WPack {
  def headWidth(dataW: Int): Int = 64 + dataW / 8
  def tailWidth(dataW: Int): Int = dataW - 64

  def headPayload(d: AxiWriteData, dataW: Int): UInt = {
    val raw = Cat(d.strb(dataW/8 - 1, 0), d.data(63, 0))
    Cat(0.U((LinkFrame.kPayloadBits - headWidth(dataW)).W), raw)
  }
  def tailPayload(d: AxiWriteData, dataW: Int): UInt = {
    val raw = d.data(dataW - 1, 64)
    Cat(0.U((LinkFrame.kPayloadBits - tailWidth(dataW)).W), raw)
  }

  def headFlags(d: AxiWriteData): UInt = Cat(0.U(3.W), d.last)

  def assemble(headPayload: UInt, headFlags: UInt, tailPayload: UInt,
               dataW: Int, idW: Int): AxiWriteData = {
    val out = Wire(new AxiWriteData(dataW, idW))
    out.data := Cat(tailPayload(tailWidth(dataW) - 1, 0), headPayload(63, 0))
    out.strb := headPayload(63 + dataW/8, 64)
    out.last := headFlags(0)
    out
  }
}

/** AxiReadData split across R_HEAD + R_TAIL.
  *  R_HEAD payload: data[63:0] (64) | id(idW) | resp(2)
  *  R_TAIL payload: data[dataW-1:64]
  *  'last' bit in flags[0] of R_HEAD.
  */
object RPack {
  def headWidth(dataW: Int, idW: Int): Int = 64 + idW + 2
  def tailWidth(dataW: Int): Int = dataW - 64

  def headPayload(r: AxiReadData, dataW: Int, idW: Int): UInt = {
    val raw = Cat(r.data(63, 0), r.id(idW - 1, 0), r.resp(1, 0))
    Cat(0.U((LinkFrame.kPayloadBits - headWidth(dataW, idW)).W), raw)
  }
  def tailPayload(r: AxiReadData, dataW: Int): UInt = {
    val raw = r.data(dataW - 1, 64)
    Cat(0.U((LinkFrame.kPayloadBits - tailWidth(dataW)).W), raw)
  }
  def headFlags(r: AxiReadData): UInt = Cat(0.U(3.W), r.last)

  def assemble(headPayload: UInt, headFlags: UInt, tailPayload: UInt,
               dataW: Int, idW: Int): AxiReadData = {
    val out = Wire(new AxiReadData(dataW, idW))
    out.resp := headPayload(1, 0)
    out.id := headPayload(idW + 1, 2)
    val dLow = headPayload(idW + 65, idW + 2)
    val dHi = tailPayload(tailWidth(dataW) - 1, 0)
    out.data := Cat(dHi, dLow)
    out.last := headFlags(0)
    out
  }
}

/** CREDIT frame payload: cum_released[0..4] (creditWidth each) | valid_mask(5).
  */
object CreditPack {
  def packPayload(cum: Vec[UInt], validMask: UInt, creditWidth: Int): UInt = {
    require(cum.length == LinkFrame.kNumCc)
    val raw = Cat(
      validMask(LinkFrame.kNumCc - 1, 0),
      cum(4)(creditWidth - 1, 0),
      cum(3)(creditWidth - 1, 0),
      cum(2)(creditWidth - 1, 0),
      cum(1)(creditWidth - 1, 0),
      cum(0)(creditWidth - 1, 0),
    )
    val width = LinkFrame.kNumCc * creditWidth + LinkFrame.kNumCc
    Cat(0.U((LinkFrame.kPayloadBits - width).W), raw)
  }

  def unpackPayload(payload: UInt, creditWidth: Int): (Vec[UInt], UInt) = {
    val cum = Wire(Vec(LinkFrame.kNumCc, UInt(creditWidth.W)))
    for (i <- 0 until LinkFrame.kNumCc) {
      cum(i) := payload(creditWidth * (i + 1) - 1, creditWidth * i)
    }
    val maskLo = LinkFrame.kNumCc * creditWidth
    val mask = payload(maskLo + LinkFrame.kNumCc - 1, maskLo)
    (cum, mask)
  }
}
