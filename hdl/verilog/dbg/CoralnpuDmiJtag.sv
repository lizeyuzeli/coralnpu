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
// Self-contained DMI-over-JTAG implementation for the CoralNPU CoreChip
// tape-out. Bundles the OpenTitan vendored pulp_riscv_dbg debug TAP and
// dmi_jtag, a minimal CDC implementation (`dmi_cdc`) suitable for v1
// silicon, and a flat-port wrapper module `coralnpu_dmi_jtag` that the
// Chisel BlackBox in `CoralnpuDmiJtag.scala` binds to.
//
// Provenance:
//   * `package dm`, `dmi_jtag_tap`, `dmi_jtag` are derived from
//     lowRISC/opentitan's vendored pulp_riscv_dbg (Apache-2.0 / SHL-0.51).
//     - dmi_jtag_tap is modified to inline a plain `~tck_i` inverter
//       instead of pulling in `prim_clock_inv`, removing the OpenTitan
//       prim_* dependency tree.
//   * `dmi_cdc` is a new, minimal RZ (4-phase) async handshake CDC. It
//     replaces OpenTitan's `dmi_cdc` (which uses `prim_fifo_async_simple`
//     and friends) with a self-contained equivalent. Throughput is one
//     transaction per several round-trip-synchroniser cycles, which is
//     fine for a JTAG-driven DMI on real silicon.

// ============================================================================
// dm package (CSR/DMI type definitions, abbreviated from OpenTitan).
// ============================================================================
package dm;
  // DTM op encodings (used by dmi_jtag's request register).
  typedef enum logic [1:0] {
    DTM_NOP   = 2'h0,
    DTM_READ  = 2'h1,
    DTM_WRITE = 2'h2
  } dtm_op_e;

  typedef enum logic [1:0] {
    DTM_SUCCESS = 2'h0,
    DTM_ERR     = 2'h2,
    DTM_BUSY    = 2'h3
  } dtm_op_status_e;

  // DMI request/response packed structs.
  // Layout matches OpenTitan's pulp_riscv_dbg dm_pkg so that the vendored
  // dmi_jtag.sv compiles unchanged.
  typedef struct packed {
    logic [31:0] addr;
    dtm_op_e     op;
    logic [31:0] data;
  } dmi_req_t;

  typedef struct packed {
    logic [31:0] data;
    logic [1:0]  resp;
  } dmi_resp_t;

  typedef struct packed {
    logic [31:18] zero1;
    logic         dmihardreset;
    logic         dmireset;
    logic         zero0;
    logic [14:12] idle;
    logic [11:10] dmistat;
    logic [9:4]   abits;
    logic [3:0]   version;
  } dtmcs_t;
endpackage : dm

// ============================================================================
// dmi_jtag_tap : JTAG TAP controller for the DMI register chain.
// Adapted from lowRISC/opentitan pulp_riscv_dbg. The only change is the
// removal of the prim_clock_inv dependency: `tck_n` is just `~tck_i`.
// ============================================================================
module dmi_jtag_tap #(
  parameter int unsigned IrLength = 5,
  parameter logic [31:0] IdcodeValue = 32'h00000001
) (
  input  logic        tck_i,
  input  logic        tms_i,
  input  logic        trst_ni,
  input  logic        td_i,
  output logic        td_o,
  output logic        tdo_oe_o,
  input  logic        testmode_i,
  output logic        tck_o,
  output logic        dmi_clear_o,
  output logic        update_o,
  output logic        capture_o,
  output logic        shift_o,
  output logic        tdi_o,
  output logic        dtmcs_select_o,
  input  logic        dtmcs_tdo_i,
  output logic        dmi_select_o,
  input  logic        dmi_tdo_i
);

  typedef enum logic [3:0] {
    TestLogicReset, RunTestIdle, SelectDrScan,
    CaptureDr, ShiftDr, Exit1Dr, PauseDr, Exit2Dr,
    UpdateDr, SelectIrScan, CaptureIr, ShiftIr,
    Exit1Ir, PauseIr, Exit2Ir, UpdateIr
  } tap_state_e;

  tap_state_e tap_state_q, tap_state_d;
  logic update_dr, shift_dr, capture_dr;

  typedef enum logic [IrLength-1:0] {
    BYPASS0   = 'h0,
    IDCODE    = 'h1,
    DTMCSR    = 'h10,
    DMIACCESS = 'h11,
    BYPASS1   = 'h1f
  } ir_reg_e;

  logic [IrLength-1:0]  jtag_ir_shift_d, jtag_ir_shift_q;
  ir_reg_e              jtag_ir_d, jtag_ir_q;
  logic capture_ir, shift_ir, update_ir, test_logic_reset;

  always_comb begin : p_jtag
    jtag_ir_shift_d = jtag_ir_shift_q;
    jtag_ir_d       = jtag_ir_q;
    if (shift_ir) begin
      jtag_ir_shift_d = {td_i, jtag_ir_shift_q[IrLength-1:1]};
    end
    if (capture_ir) begin
      jtag_ir_shift_d =  IrLength'(4'b0101);
    end
    if (update_ir) begin
      jtag_ir_d = ir_reg_e'(jtag_ir_shift_q);
    end
    if (test_logic_reset) begin
      jtag_ir_shift_d = '0;
      jtag_ir_d = IDCODE;
    end
  end

  always_ff @(posedge tck_i, negedge trst_ni) begin : p_jtag_ir_reg
    if (!trst_ni) begin
      jtag_ir_shift_q <= '0;
      jtag_ir_q       <= IDCODE;
    end else begin
      jtag_ir_shift_q <= jtag_ir_shift_d;
      jtag_ir_q       <= jtag_ir_d;
    end
  end

  logic [31:0] idcode_d, idcode_q;
  logic        idcode_select;
  logic        bypass_select;
  logic        bypass_d, bypass_q;

  always_comb begin
    idcode_d = idcode_q;
    bypass_d = bypass_q;
    if (capture_dr) begin
      if (idcode_select) idcode_d = IdcodeValue;
      if (bypass_select) bypass_d = 1'b0;
    end
    if (shift_dr) begin
      if (idcode_select)  idcode_d = {td_i, 31'(idcode_q >> 1)};
      if (bypass_select)  bypass_d = td_i;
    end
    if (test_logic_reset) begin
      idcode_d = IdcodeValue;
      bypass_d = 1'b0;
    end
  end

  always_comb begin : p_data_reg_sel
    dmi_select_o   = 1'b0;
    dtmcs_select_o = 1'b0;
    idcode_select  = 1'b0;
    bypass_select  = 1'b0;
    unique case (jtag_ir_q)
      BYPASS0:   bypass_select  = 1'b1;
      IDCODE:    idcode_select  = 1'b1;
      DTMCSR:    dtmcs_select_o = 1'b1;
      DMIACCESS: dmi_select_o   = 1'b1;
      BYPASS1:   bypass_select  = 1'b1;
      default:   bypass_select  = 1'b1;
    endcase
  end

  logic tdo_mux;
  always_comb begin : p_out_sel
    if (shift_ir) begin
      tdo_mux = jtag_ir_shift_q[0];
    end else begin
      unique case (jtag_ir_q)
        IDCODE:         tdo_mux = idcode_q[0];
        DTMCSR:         tdo_mux = dtmcs_tdo_i;
        DMIACCESS:      tdo_mux = dmi_tdo_i;
        default:        tdo_mux = bypass_q;
      endcase
    end
  end

  // Inline tck inverter (replaces prim_clock_inv from OpenTitan).
  logic tck_n;
  assign tck_n = ~tck_i;
  // Silence unused-port warnings.
  logic unused_testmode;
  assign unused_testmode = testmode_i;

  always_ff @(posedge tck_n, negedge trst_ni) begin : p_tdo_regs
    if (!trst_ni) begin
      td_o     <= 1'b0;
      tdo_oe_o <= 1'b0;
    end else begin
      td_o     <= tdo_mux;
      tdo_oe_o <= (shift_ir | shift_dr);
    end
  end

  always_comb begin : p_tap_fsm
    test_logic_reset   = 1'b0;
    capture_dr         = 1'b0;
    shift_dr           = 1'b0;
    update_dr          = 1'b0;
    capture_ir         = 1'b0;
    shift_ir           = 1'b0;
    update_ir          = 1'b0;
    unique case (tap_state_q)
      TestLogicReset: begin
        tap_state_d = (tms_i) ? TestLogicReset : RunTestIdle;
        test_logic_reset = 1'b1;
      end
      RunTestIdle:    tap_state_d = (tms_i) ? SelectDrScan : RunTestIdle;
      SelectDrScan:   tap_state_d = (tms_i) ? SelectIrScan : CaptureDr;
      CaptureDr: begin
        capture_dr = 1'b1;
        tap_state_d = (tms_i) ? Exit1Dr : ShiftDr;
      end
      ShiftDr: begin
        shift_dr = 1'b1;
        tap_state_d = (tms_i) ? Exit1Dr : ShiftDr;
      end
      Exit1Dr:        tap_state_d = (tms_i) ? UpdateDr : PauseDr;
      PauseDr:        tap_state_d = (tms_i) ? Exit2Dr : PauseDr;
      Exit2Dr:        tap_state_d = (tms_i) ? UpdateDr : ShiftDr;
      UpdateDr: begin
        update_dr = 1'b1;
        tap_state_d = (tms_i) ? SelectDrScan : RunTestIdle;
      end
      SelectIrScan:   tap_state_d = (tms_i) ? TestLogicReset : CaptureIr;
      CaptureIr: begin
        capture_ir = 1'b1;
        tap_state_d = (tms_i) ? Exit1Ir : ShiftIr;
      end
      ShiftIr: begin
        shift_ir = 1'b1;
        tap_state_d = (tms_i) ? Exit1Ir : ShiftIr;
      end
      Exit1Ir:        tap_state_d = (tms_i) ? UpdateIr : PauseIr;
      PauseIr:        tap_state_d = (tms_i) ? Exit2Ir : PauseIr;
      Exit2Ir:        tap_state_d = (tms_i) ? UpdateIr : ShiftIr;
      UpdateIr: begin
        update_ir = 1'b1;
        tap_state_d = (tms_i) ? SelectDrScan : RunTestIdle;
      end
      default: tap_state_d = TestLogicReset;
    endcase
  end

  always_ff @(posedge tck_i or negedge trst_ni) begin : p_regs
    if (!trst_ni) begin
      tap_state_q <= TestLogicReset;
      idcode_q    <= IdcodeValue;
      bypass_q    <= 1'b0;
    end else begin
      tap_state_q <= tap_state_d;
      idcode_q    <= idcode_d;
      bypass_q    <= bypass_d;
    end
  end

  assign tck_o = tck_i;
  assign tdi_o = td_i;
  assign update_o = update_dr;
  assign shift_o = shift_dr;
  assign capture_o = capture_dr;
  assign dmi_clear_o = test_logic_reset;

endmodule : dmi_jtag_tap

// ============================================================================
// dmi_cdc : minimal 4-phase (RZ) async handshake CDC for DMI.
// Replaces OpenTitan's `dmi_cdc` to remove the prim_fifo_async_simple
// dependency.  Each transaction completes in O(synchroniser depth)
// cycles on each side; throughput is limited by the JTAG side, which
// emits a transaction at most every few hundred TCKs, so the simple
// scheme is more than fast enough.
//
// Protocol (per direction):
//   * Source holds wdata + req=1 once wvalid_i && wready_o fires.
//   * Destination 2-flop-synchronises req.  When dst_req=1 and the
//     downstream consumer accepts (rready_i), it latches ack=1.
//   * Source 2-flop-synchronises ack, then drops req=0.
//   * Destination sees src_req=0 (after sync), drops ack=0.
//   * Source sees ack=0 (after sync), wready_o asserts again.
// ============================================================================
module dmi_cdc (
  input  logic             testmode_i,
  input  logic             test_rst_ni,

  // JTAG side (write/read pair).
  input  logic             tck_i,
  input  logic             trst_ni,
  input  dm::dmi_req_t     jtag_dmi_req_i,
  output logic             jtag_dmi_ready_o,
  input  logic             jtag_dmi_valid_i,
  input  logic             jtag_dmi_cdc_clear_i,

  output dm::dmi_resp_t    jtag_dmi_resp_o,
  output logic             jtag_dmi_valid_o,
  input  logic             jtag_dmi_ready_i,

  // Core side.
  input  logic             clk_i,
  input  logic             rst_ni,

  output logic             core_dmi_rst_no,
  output dm::dmi_req_t     core_dmi_req_o,
  output logic             core_dmi_valid_o,
  input  logic             core_dmi_ready_i,

  input  dm::dmi_resp_t    core_dmi_resp_i,
  output logic             core_dmi_ready_o,
  input  logic             core_dmi_valid_i
);

  // Tie off DFT/test ports (not used for v1 silicon path).
  logic unused_tm;
  assign unused_tm = testmode_i ^ test_rst_ni;
  assign core_dmi_rst_no = rst_ni;

  // --------------------------------------------------------------------
  // Req path: tck_i -> clk_i
  // --------------------------------------------------------------------
  // Source (tck_i) holds data + req.
  logic            req_src_req;     // src->dst handshake bit
  logic            req_src_ack_s2;  // dst->src ack, after 2-flop sync
  dm::dmi_req_t    req_src_data;

  logic            req_jtag_clear;
  assign req_jtag_clear = !trst_ni || jtag_dmi_cdc_clear_i;

  assign jtag_dmi_ready_o = (req_src_req == req_src_ack_s2);

  always_ff @(posedge tck_i or negedge trst_ni) begin
    if (!trst_ni) begin
      req_src_req  <= 1'b0;
      req_src_data <= '0;
    end else if (jtag_dmi_cdc_clear_i) begin
      req_src_req  <= 1'b0;
      req_src_data <= '0;
    end else if (jtag_dmi_valid_i && jtag_dmi_ready_o) begin
      req_src_req  <= ~req_src_req;
      req_src_data <= jtag_dmi_req_i;
    end
  end

  // Dest (clk_i) sees req via 2-flop sync.
  logic req_dst_req_s1, req_dst_req_s2, req_dst_req_s3;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      req_dst_req_s1 <= 1'b0;
      req_dst_req_s2 <= 1'b0;
      req_dst_req_s3 <= 1'b0;
    end else begin
      req_dst_req_s1 <= req_src_req;
      req_dst_req_s2 <= req_dst_req_s1;
      req_dst_req_s3 <= req_dst_req_s2;
    end
  end

  logic req_dst_ack;
  assign core_dmi_valid_o = (req_dst_req_s2 != req_dst_ack);
  assign core_dmi_req_o   = req_src_data;  // safe: stable while handshake outstanding

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      req_dst_ack <= 1'b0;
    end else if (core_dmi_valid_o && core_dmi_ready_i) begin
      req_dst_ack <= req_dst_req_s2;
    end
  end

  // Dst ack back to src via 2-flop sync.
  logic req_src_ack_s1;
  always_ff @(posedge tck_i or negedge trst_ni) begin
    if (!trst_ni) begin
      req_src_ack_s1 <= 1'b0;
      req_src_ack_s2 <= 1'b0;
    end else begin
      req_src_ack_s1 <= req_dst_ack;
      req_src_ack_s2 <= req_src_ack_s1;
    end
  end

  // --------------------------------------------------------------------
  // Resp path: clk_i -> tck_i  (mirrors the above with swapped clocks)
  // --------------------------------------------------------------------
  logic            resp_src_req;
  logic            resp_src_ack_s2;
  dm::dmi_resp_t   resp_src_data;

  assign core_dmi_ready_o = (resp_src_req == resp_src_ack_s2);

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      resp_src_req  <= 1'b0;
      resp_src_data <= '0;
    end else if (core_dmi_valid_i && core_dmi_ready_o) begin
      resp_src_req  <= ~resp_src_req;
      resp_src_data <= core_dmi_resp_i;
    end
  end

  logic resp_dst_req_s1, resp_dst_req_s2;
  always_ff @(posedge tck_i or negedge trst_ni) begin
    if (!trst_ni) begin
      resp_dst_req_s1 <= 1'b0;
      resp_dst_req_s2 <= 1'b0;
    end else begin
      resp_dst_req_s1 <= resp_src_req;
      resp_dst_req_s2 <= resp_dst_req_s1;
    end
  end

  logic resp_dst_ack;
  assign jtag_dmi_valid_o = (resp_dst_req_s2 != resp_dst_ack);
  assign jtag_dmi_resp_o  = resp_src_data;

  always_ff @(posedge tck_i or negedge trst_ni) begin
    if (!trst_ni) begin
      resp_dst_ack <= 1'b0;
    end else if (jtag_dmi_valid_o && jtag_dmi_ready_i) begin
      resp_dst_ack <= resp_dst_req_s2;
    end
  end

  logic resp_src_ack_s1;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      resp_src_ack_s1 <= 1'b0;
      resp_src_ack_s2 <= 1'b0;
    end else begin
      resp_src_ack_s1 <= resp_dst_ack;
      resp_src_ack_s2 <= resp_src_ack_s1;
    end
  end

  // Spare s3 reference to keep simulators that warn on unread bits happy.
  logic unused_s3;
  assign unused_s3 = req_dst_req_s3;
endmodule : dmi_cdc

// ============================================================================
// dmi_jtag : OpenTitan's vendored module, unchanged. Drives the TAP and
// owns the DTM register FSM. Crosses to the core via dmi_cdc.
// ============================================================================
module dmi_jtag #(
  parameter logic [31:0] IdcodeValue = 32'h00000DB3,
  parameter int unsigned NumDmiWordAbits = 16
) (
  input  logic         clk_i,
  input  logic         rst_ni,
  input  logic         testmode_i,
  input  logic         test_rst_ni,

  output logic         dmi_rst_no,
  output dm::dmi_req_t dmi_req_o,
  output logic         dmi_req_valid_o,
  input  logic         dmi_req_ready_i,

  input  dm::dmi_resp_t dmi_resp_i,
  output logic         dmi_resp_ready_o,
  input  logic         dmi_resp_valid_i,

  input  logic         tck_i,
  input  logic         tms_i,
  input  logic         trst_ni,
  input  logic         td_i,
  output logic         td_o,
  output logic         tdo_oe_o
);

  typedef enum logic [1:0] {
    DMINoError = 2'h0, DMIReservedError = 2'h1,
    DMIOPFailed = 2'h2, DMIBusy = 2'h3
  } dmi_error_e;
  dmi_error_e error_d, error_q;

  logic tck;
  logic jtag_dmi_clear;
  logic dmi_clear;
  logic update;
  logic capture;
  logic shift;
  logic tdi;

  logic dtmcs_select;
  dm::dtmcs_t dtmcs_d, dtmcs_q;

  assign dmi_clear = jtag_dmi_clear || (dtmcs_select && update && dtmcs_q.dmihardreset);

  always_comb begin
    dtmcs_d = dtmcs_q;
    if (capture) begin
      if (dtmcs_select) begin
        dtmcs_d  = '{
                      zero1        : '0,
                      dmihardreset : 1'b0,
                      dmireset     : 1'b0,
                      zero0        : '0,
                      idle         : 3'd1,
                      dmistat      : error_q,
                      abits        : 6'(NumDmiWordAbits),
                      version      : 4'd1
                    };
      end
    end
    if (shift) begin
      if (dtmcs_select) dtmcs_d  = {tdi, 31'(dtmcs_q >> 1)};
    end
  end

  always_ff @(posedge tck or negedge trst_ni) begin
    if (!trst_ni) begin
      dtmcs_q <= '0;
    end else begin
      dtmcs_q <= dtmcs_d;
    end
  end

  logic        dmi_select;
  logic        dmi_tdo;

  dm::dmi_req_t  dmi_req;
  logic          dmi_req_ready;
  logic          dmi_req_valid;

  dm::dmi_resp_t dmi_resp;
  logic          dmi_resp_valid;
  logic          dmi_resp_ready;

  typedef struct packed {
    logic [NumDmiWordAbits-1:0]  address;
    logic [31:0] data;
    logic [1:0]  op;
  } dmi_t;

  typedef enum logic [2:0] { Idle, Read, WaitReadValid, Write, WaitWriteValid } state_e;
  state_e state_d, state_q;

  logic [$bits(dmi_t)-1:0] dr_d, dr_q;
  logic [NumDmiWordAbits-1:0] address_d, address_q;
  logic [31:0] data_d, data_q;

  dmi_t  dmi;
  assign dmi          = dmi_t'(dr_q);
  assign dmi_req.addr = $bits(dmi_req.addr)'(address_q);
  assign dmi_req.data = data_q;
  assign dmi_req.op   = (state_q == Write) ? dm::DTM_WRITE : dm::DTM_READ;
  assign dmi_resp_ready = 1'b1;

  logic error_dmi_busy;
  logic error_dmi_op_failed;

  always_comb begin : p_fsm
    error_dmi_busy = 1'b0;
    error_dmi_op_failed = 1'b0;
    state_d   = state_q;
    address_d = address_q;
    data_d    = data_q;
    error_d   = error_q;
    dmi_req_valid = 1'b0;

    if (dmi_clear) begin
      state_d   = Idle;
      data_d    = '0;
      error_d   = DMINoError;
      address_d = '0;
    end else begin
      unique case (state_q)
        Idle: begin
          if (dmi_select && update && (error_q == DMINoError)) begin
            address_d = dmi.address;
            data_d = dmi.data;
            if (dm::dtm_op_e'(dmi.op) == dm::DTM_READ) begin
              state_d = Read;
            end else if (dm::dtm_op_e'(dmi.op) == dm::DTM_WRITE) begin
              state_d = Write;
            end
          end
        end
        Read: begin
          dmi_req_valid = 1'b1;
          if (dmi_req_ready) begin
            state_d = WaitReadValid;
          end
        end
        WaitReadValid: begin
          if (dmi_resp_valid) begin
            unique case (dmi_resp.resp)
              dm::DTM_SUCCESS: data_d = dmi_resp.data;
              dm::DTM_ERR: begin
                data_d = 32'hDEAD_BEEF;
                error_dmi_op_failed = 1'b1;
              end
              dm::DTM_BUSY: begin
                data_d = 32'hB051_B051;
                error_dmi_busy = 1'b1;
              end
              default: data_d = 32'hBAAD_C0DE;
            endcase
            state_d = Idle;
          end
        end
        Write: begin
          dmi_req_valid = 1'b1;
          if (dmi_req_ready) begin
            state_d = WaitWriteValid;
          end
        end
        WaitWriteValid: begin
          if (dmi_resp_valid) begin
            unique case (dmi_resp.resp)
              dm::DTM_ERR: error_dmi_op_failed = 1'b1;
              dm::DTM_BUSY: error_dmi_busy = 1'b1;
              default: ;
            endcase
            state_d = Idle;
          end
        end
        default: begin
          if (dmi_resp_valid) begin
            state_d = Idle;
          end
        end
      endcase

      if (update && state_q != Idle) begin
        error_dmi_busy = 1'b1;
      end
      if (capture && state_q inside {Read, WaitReadValid}) begin
        error_dmi_busy = 1'b1;
      end
      if (error_dmi_busy && error_q == DMINoError) begin
        error_d = DMIBusy;
      end
      if (error_dmi_op_failed && error_q == DMINoError) begin
        error_d = DMIOPFailed;
      end
      if (update && dtmcs_q.dmireset && dtmcs_select) begin
        error_d = DMINoError;
      end
    end
  end

  assign dmi_tdo = dr_q[0];

  always_comb begin : p_shift
    dr_d = dr_q;
    if (dmi_clear) begin
      dr_d = '0;
    end else begin
      if (capture) begin
        if (dmi_select) begin
          if (error_q == DMINoError && !error_dmi_busy) begin
            dr_d = {address_q, data_q, DMINoError};
          end else if (error_q == DMIBusy || error_dmi_busy) begin
            dr_d = {address_q, data_q, DMIBusy};
          end
        end
      end
      if (shift) begin
        if (dmi_select) begin
          dr_d = {tdi, dr_q[$bits(dr_q)-1:1]};
        end
      end
    end
  end

  always_ff @(posedge tck or negedge trst_ni) begin
    if (!trst_ni) begin
      dr_q      <= '0;
      state_q   <= Idle;
      address_q <= '0;
      data_q    <= '0;
      error_q   <= DMINoError;
    end else begin
      dr_q      <= dr_d;
      state_q   <= state_d;
      address_q <= address_d;
      data_q    <= data_d;
      error_q   <= error_d;
    end
  end

  dmi_jtag_tap #(
    .IrLength (5),
    .IdcodeValue(IdcodeValue)
  ) i_dmi_jtag_tap (
    .tck_i,
    .tms_i,
    .trst_ni,
    .td_i,
    .td_o,
    .tdo_oe_o,
    .testmode_i,
    .tck_o          ( tck              ),
    .dmi_clear_o    ( jtag_dmi_clear   ),
    .update_o       ( update           ),
    .capture_o      ( capture          ),
    .shift_o        ( shift            ),
    .tdi_o          ( tdi              ),
    .dtmcs_select_o ( dtmcs_select     ),
    .dtmcs_tdo_i    ( dtmcs_q[0]       ),
    .dmi_select_o   ( dmi_select       ),
    .dmi_tdo_i      ( dmi_tdo          )
  );

  dmi_cdc i_dmi_cdc (
    .testmode_i,
    .test_rst_ni,
    .tck_i                ( tck              ),
    .trst_ni              ( trst_ni          ),
    .jtag_dmi_cdc_clear_i ( dmi_clear        ),
    .jtag_dmi_req_i       ( dmi_req          ),
    .jtag_dmi_ready_o     ( dmi_req_ready    ),
    .jtag_dmi_valid_i     ( dmi_req_valid    ),
    .jtag_dmi_resp_o      ( dmi_resp         ),
    .jtag_dmi_valid_o     ( dmi_resp_valid   ),
    .jtag_dmi_ready_i     ( dmi_resp_ready   ),
    .clk_i,
    .rst_ni,
    .core_dmi_rst_no      ( dmi_rst_no       ),
    .core_dmi_req_o       ( dmi_req_o        ),
    .core_dmi_valid_o     ( dmi_req_valid_o  ),
    .core_dmi_ready_i     ( dmi_req_ready_i  ),
    .core_dmi_resp_i      ( dmi_resp_i       ),
    .core_dmi_ready_o     ( dmi_resp_ready_o ),
    .core_dmi_valid_i     ( dmi_resp_valid_i )
  );

endmodule : dmi_jtag

// ============================================================================
// coralnpu_dmi_jtag : flat-port wrapper. This is the BlackBox the Chisel
// CoreChip binds to. Translates between the OpenTitan dm:: packed structs
// and individual logic vectors so the Chisel side doesn't need to import
// the `dm` package.
// ============================================================================
module coralnpu_dmi_jtag #(
  parameter logic [31:0] IdcodeValue = 32'h04f5484d
) (
  input  logic        clk_i,
  input  logic        rst_ni,

  output logic        dmi_rst_no,
  output logic [31:0] dmi_req_addr_o,
  output logic [1:0]  dmi_req_op_o,
  output logic [31:0] dmi_req_data_o,
  output logic        dmi_req_valid_o,
  input  logic        dmi_req_ready_i,

  input  logic [31:0] dmi_resp_data_i,
  input  logic [1:0]  dmi_resp_resp_i,
  input  logic        dmi_resp_valid_i,
  output logic        dmi_resp_ready_o,

  input  logic        tck_i,
  input  logic        tms_i,
  input  logic        trst_ni,
  input  logic        td_i,
  output logic        td_o,
  output logic        tdo_oe_o
);

  dm::dmi_req_t  req_internal;
  dm::dmi_resp_t resp_internal;
  assign dmi_req_addr_o     = req_internal.addr;
  assign dmi_req_op_o       = req_internal.op;
  assign dmi_req_data_o     = req_internal.data;
  assign resp_internal.data = dmi_resp_data_i;
  assign resp_internal.resp = dmi_resp_resp_i;

  dmi_jtag #(
    .IdcodeValue(IdcodeValue)
  ) u_dmi_jtag (
    .clk_i,
    .rst_ni,
    .testmode_i      (1'b0),
    .test_rst_ni     (1'b1),
    .dmi_rst_no      (dmi_rst_no),
    .dmi_req_o       (req_internal),
    .dmi_req_valid_o (dmi_req_valid_o),
    .dmi_req_ready_i (dmi_req_ready_i),
    .dmi_resp_i      (resp_internal),
    .dmi_resp_ready_o(dmi_resp_ready_o),
    .dmi_resp_valid_i(dmi_resp_valid_i),
    .tck_i           (tck_i),
    .tms_i           (tms_i),
    .trst_ni         (trst_ni),
    .td_i            (td_i),
    .td_o            (td_o),
    .tdo_oe_o        (tdo_oe_o)
  );

endmodule : coralnpu_dmi_jtag
