// Copyright 2025 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Thin SV wrapper around `dmi_jtag` that flattens dm::dmi_req_t and
// dm::dmi_resp_t to plain logic ports so the module can be declared as a
// Chisel BlackBox without dragging the `dm` package into the Chisel side.
//
// Field widths/ordering match dm_pkg::dmi_req_t (addr[31:0], op[1:0],
// data[31:0]) and dmi_resp_t (data[31:0], resp[1:0]) which are the same
// widths used by the CoralNPU `DebugModuleReqIO` / `DebugModuleRspIO`
// bundles in the kernel, so connections can be one-to-one.
//
// External dependencies (must be in the source list when this is built):
//   - pulp_riscv_dbg/src/dm_pkg.sv
//   - pulp_riscv_dbg/src/dmi_jtag.sv
//   - pulp_riscv_dbg/src/dmi_jtag_tap.sv
//   - prim_*.sv primitives referenced therein

module dmi_jtag_wrapper #(
    parameter logic [31:0] IdcodeValue = 32'h04f5484d
) (
    input  logic        clk_i,
    input  logic        rst_ni,
    input  logic        testmode_i,
    input  logic        test_rst_ni,

    output logic        dmi_rst_no,

    // Flattened dmi_req_t
    output logic [31:0] dmi_req_address_o,
    output logic [1:0]  dmi_req_op_o,
    output logic [31:0] dmi_req_data_o,
    output logic        dmi_req_valid_o,
    input  logic        dmi_req_ready_i,

    // Flattened dmi_resp_t
    input  logic [31:0] dmi_resp_data_i,
    input  logic [1:0]  dmi_resp_op_i,
    output logic        dmi_resp_ready_o,
    input  logic        dmi_resp_valid_i,

    // JTAG pads
    input  logic        tck_i,
    input  logic        tms_i,
    input  logic        trst_ni,
    input  logic        td_i,
    output logic        td_o,
    output logic        tdo_oe_o
);

  dm::dmi_req_t  dmi_req;
  dm::dmi_resp_t dmi_resp;

  assign dmi_req_address_o = dmi_req.addr;
  assign dmi_req_op_o      = dmi_req.op;
  assign dmi_req_data_o    = dmi_req.data;

  assign dmi_resp.data = dmi_resp_data_i;
  assign dmi_resp.resp = dmi_resp_op_i;

  dmi_jtag #(.IdcodeValue(IdcodeValue)) i_dmi_jtag (
      .clk_i           (clk_i),
      .rst_ni          (rst_ni),
      .testmode_i      (testmode_i),
      .test_rst_ni     (test_rst_ni),
      .dmi_rst_no      (dmi_rst_no),
      .dmi_req_o       (dmi_req),
      .dmi_req_valid_o (dmi_req_valid_o),
      .dmi_req_ready_i (dmi_req_ready_i),
      .dmi_resp_i      (dmi_resp),
      .dmi_resp_ready_o(dmi_resp_ready_o),
      .dmi_resp_valid_i(dmi_resp_valid_i),
      .tck_i           (tck_i),
      .tms_i           (tms_i),
      .trst_ni         (trst_ni),
      .td_i            (td_i),
      .td_o            (td_o),
      .tdo_oe_o        (tdo_oe_o)
  );

endmodule
