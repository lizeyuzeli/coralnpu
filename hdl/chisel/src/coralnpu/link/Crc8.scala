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

/** Combinational CRC-8 generator, polynomial 0x07 (CRC-8-CCITT, no reflection,
  * init 0x00). Bit-serial unrolled.
  *
  * Used to protect the 120-bit (chId(4) || flags(4) || payload(112)) field of
  * a link frame; receiver compares against the 8-bit crc field.
  */
object Crc8 {
  /** Compute CRC-8 over an arbitrary-width input, MSB-first. */
  def compute(data: UInt): UInt = {
    val w = data.getWidth
    val poly = "b00000111".U(8.W)
    var crc = 0.U(8.W)
    // Process MSB-first.
    for (i <- (0 until w).reverse) {
      val bit = data(i)
      val top = crc(7) ^ bit
      val shifted = (crc(6, 0) ## 0.U(1.W)).asUInt
      crc = Mux(top.asBool, (shifted ^ poly).asUInt, shifted)
    }
    crc
  }
}
