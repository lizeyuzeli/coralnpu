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
// Chisel BlackBox wrapper for `coralnpu_dmi_jtag` (see
// hdl/verilog/dbg/CoralnpuDmiJtag.sv).  Self-contained DMI-over-JTAG
// IP for the CoralNPU CoreChip tape-out top: vendored OpenTitan
// dmi_jtag + a minimal CDC, no external `prim_*` dependencies.

package coralnpu.dbg

import chisel3._
import chisel3.experimental.IntParam
import chisel3.util.HasBlackBoxResource

// Flat-port BlackBox of the SV `coralnpu_dmi_jtag` wrapper.
//
// Port summary:
//   * `clk_i` / `rst_ni`        : core-clock domain (matches kernel AXI clock).
//   * dmi_req_*  / dmi_resp_*   : flattened OpenTitan dm::dmi_req_t /
//                                 dm::dmi_resp_t. Addr=32b, Op=2b, Data=32b,
//                                 Resp=2b (encoding matches CoralNPU's
//                                 DmReqOp / DmRspOp).
//   * tck_i/tms_i/trst_ni/td_i  : raw JTAG pads (TCK is a free-running clock).
//   * td_o / tdo_oe_o           : TDO output + output-enable.
class CoralnpuDmiJtag(idcodeValue: BigInt = BigInt("04f5484d", 16))
    extends BlackBox(Map(
      "IdcodeValue" -> IntParam(idcodeValue)
    ))
    with HasBlackBoxResource {

  override val desiredName = "coralnpu_dmi_jtag"

  val io = IO(new Bundle {
    // Core (DMI) clock domain.
    val clk_i  = Input(Clock())
    val rst_ni = Input(Bool())

    // DMI request channel (host -> Debug Module).
    val dmi_rst_no       = Output(Bool())
    val dmi_req_addr_o   = Output(UInt(32.W))
    val dmi_req_op_o     = Output(UInt(2.W))
    val dmi_req_data_o   = Output(UInt(32.W))
    val dmi_req_valid_o  = Output(Bool())
    val dmi_req_ready_i  = Input(Bool())

    // DMI response channel (Debug Module -> host).
    val dmi_resp_data_i  = Input(UInt(32.W))
    val dmi_resp_resp_i  = Input(UInt(2.W))
    val dmi_resp_valid_i = Input(Bool())
    val dmi_resp_ready_o = Output(Bool())

    // JTAG pads.
    val tck_i    = Input(Clock())
    val tms_i    = Input(Bool())
    val trst_ni  = Input(Bool())
    val td_i     = Input(Bool())
    val td_o     = Output(Bool())
    val tdo_oe_o = Output(Bool())
  })

  addResource("CoralnpuDmiJtag.sv")
}
