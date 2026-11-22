// rvv_backend_ft: instruction-level DMR for the RVV backend (FULL mode).
//
// Sits between dispatch and the ALU/MUL/DIV/FALU reservation stations, and
// between the result arbiter and the ROB. Nothing inside ROB or the
// execution units changes.
//
//  * Dispatch: every whitelisted uop that is pushed into its RS (copy-1) has
//    its RS payload saved in replay_mem[rob_entry].
//  * Copy-2 / replay: issued from replay_mem through the highest dispatch
//    slot of the target RS, only in a cycle where dispatch does not use that
//    slot for that RS and the RS has room. Dispatch never waits on FT.
//  * Results: the first result of an FT entry is held here; the ROB only
//    sees the result once two executions agree. On mismatch both copies are
//    re-executed, up to `RVV_FT_RETRY_MAX times; after that a trap is raised
//    through the existing RVV trap path (mcause=2, sentinel mtval).
//
// Whitelist: ALU (except VEU_CMP, which accumulates across uops), MUL/MAC,
// DIV/FDIV, FALU with VEU_FMA. Only uops that own a ROB entry.
//
// Macros: RVV_FT_ON (enables this module), RVV_FT_RETRY_MAX (default 3),
// RVV_FT_INJECT_ON (simulation fault injection, see bottom of file).
// RVV_FT_MODE_OPPORTUNISTIC / RVV_FT_WATERMARK are reserved; not implemented.

`ifndef HDL_VERILOG_RVV_DESIGN_RVV_SVH
`include "rvv_backend.svh"
`endif

`ifdef RVV_FT_ON

`ifndef RVV_FT_RETRY_MAX
  `define RVV_FT_RETRY_MAX 3
`endif

module rvv_backend_ft
(
    input  logic                                clk,
    input  logic                                rst_n,
    input  logic                                flush,
    input  logic [`ROB_DEPTH_WIDTH-1:0]         rob_rptr,
  // dispatch -> ROB (for rob_tag / pc of the entry)
    input  logic        [`NUM_DP_UOP-1:0]       uop_valid_dp2rob,
    input  DP2ROB_t     [`NUM_DP_UOP-1:0]       uop_dp2rob,
  // dispatch -> RS (original) and RS ready
    input  logic        [`NUM_DP_UOP-1:0]       rs_valid_dp2alu,
    input  ALU_RS_t     [`NUM_DP_UOP-1:0]       rs_dp2alu,
    input  logic        [`NUM_DP_UOP-1:0]       rs_ready_alu2dp,
    input  logic        [`NUM_DP_UOP-1:0]       rs_valid_dp2mul,
    input  MUL_RS_t     [`NUM_DP_UOP-1:0]       rs_dp2mul,
    input  logic        [`NUM_DP_UOP-1:0]       rs_ready_mul2dp,
    input  logic        [`NUM_DP_UOP-1:0]       rs_valid_dp2div,
    input  DIV_RS_t     [`NUM_DP_UOP-1:0]       rs_dp2div,
    input  logic        [`NUM_DP_UOP-1:0]       rs_ready_div2dp,
`ifdef ZVE32F_ON
    input  logic        [`NUM_DP_UOP-1:0]       rs_valid_dp2falu,
    input  FALU_RS_t    [`NUM_DP_UOP-1:0]       rs_dp2falu,
    input  logic        [`NUM_DP_UOP-1:0]       rs_ready_falu2dp,
`endif
  // to RS (dispatch + copy-2 injection)
    output logic        [`NUM_DP_UOP-1:0]       rs_valid_ft2alu,
    output ALU_RS_t     [`NUM_DP_UOP-1:0]       rs_ft2alu,
    output logic        [`NUM_DP_UOP-1:0]       rs_valid_ft2mul,
    output MUL_RS_t     [`NUM_DP_UOP-1:0]       rs_ft2mul,
    output logic        [`NUM_DP_UOP-1:0]       rs_valid_ft2div,
    output DIV_RS_t     [`NUM_DP_UOP-1:0]       rs_ft2div,
`ifdef ZVE32F_ON
    output logic        [`NUM_DP_UOP-1:0]       rs_valid_ft2falu,
    output FALU_RS_t    [`NUM_DP_UOP-1:0]       rs_ft2falu,
`endif
  // result arbiter -> ROB
    input  logic        [`NUM_SMPORT-1:0]       res_valid_arb2ft,
    input  PU2ROB_t     [`NUM_SMPORT-1:0]       res_arb2ft,
    output logic        [`NUM_SMPORT-1:0]       res_valid_ft2rob,
    output PU2ROB_t     [`NUM_SMPORT-1:0]       res_ft2rob,
  // unrecoverable error -> RVV trap path
    output logic                                trap_valid_o,
    output RVVInstruction                       trap_data_o
);

// ---parameters------------------------------------------------------
  localparam int D    = `ROB_DEPTH;
  localparam int DW   = `ROB_DEPTH_WIDTH;
  localparam int K    = `NUM_DP_UOP-1;   // dispatch slot used for copy-2
  localparam int NU   = 4;               // unit classes: ALU/MUL/DIV/FALU
  localparam int RMAX = `RVV_FT_RETRY_MAX;
  localparam int RW   = $clog2(RMAX+1);
  localparam int W_ALU = $bits(ALU_RS_t);
  localparam int W_MUL = $bits(MUL_RS_t);
  localparam int W_DIV = $bits(DIV_RS_t);
`ifdef ZVE32F_ON
  localparam int W_FALU = $bits(FALU_RS_t);
`else
  localparam int W_FALU = 1;
`endif
  localparam int W01 = (W_ALU > W_MUL) ? W_ALU : W_MUL;
  localparam int W23 = (W_DIV > W_FALU) ? W_DIV : W_FALU;
  localparam int W   = (W01 > W23) ? W01 : W23;   // replay_mem width

  localparam logic [1:0] U_ALU = 2'd0, U_MUL = 2'd1, U_DIV = 2'd2, U_FALU = 2'd3;

// ---state (per ROB entry)-------------------------------------------
  logic [W-1:0]                 rpm      [D-1:0];  // replay_mem payload
  logic [D-1:0]                 vld;               // entry is FT-tracked
  logic [D-1:0][1:0]            unit;
  logic [D-1:0][1:0]            pend;              // copies still to issue
  logic [D-1:0]                 have;              // one result held
  logic [D-1:0][RW-1:0]         retry;
  logic [D-1:0][`ROB_TAG_WIDTH-1:0] tag;
`ifdef TB_SUPPORT
  logic [D-1:0][`PC_WIDTH-1:0]  pc;
`endif
  PU2ROB_t [D-1:0]              hold;

// ---dispatch: whitelist and replay_mem write------------------------
  logic [`NUM_DP_UOP-1:0]           alloc;
  logic [`NUM_DP_UOP-1:0][1:0]      alloc_unit;
  logic [`NUM_DP_UOP-1:0][DW-1:0]   alloc_entry;
  logic [`NUM_DP_UOP-1:0][W-1:0]    alloc_data;

  always_comb begin
    for (int i = 0; i < `NUM_DP_UOP; i++) begin
      alloc[i]       = 1'b0;
      alloc_unit[i]  = U_ALU;
      alloc_entry[i] = rs_dp2alu[i].rob_entry;
      alloc_data[i]  = W'(rs_dp2alu[i]);
      if (rs_valid_dp2alu[i] && !rs_dp2alu[i].is_cmp) begin
        alloc[i] = 1'b1;
      end else if (rs_valid_dp2mul[i]) begin
        alloc[i]       = 1'b1;
        alloc_unit[i]  = U_MUL;
        alloc_entry[i] = rs_dp2mul[i].rob_entry;
        alloc_data[i]  = W'(rs_dp2mul[i]);
      end else if (rs_valid_dp2div[i]) begin
        alloc[i]       = 1'b1;
        alloc_unit[i]  = U_DIV;
        alloc_entry[i] = rs_dp2div[i].rob_entry;
        alloc_data[i]  = W'(rs_dp2div[i]);
      end
`ifdef ZVE32F_ON
      else if (rs_valid_dp2falu[i] && (rs_dp2falu[i].uop_exe_unit == VEU_FMA)) begin
        alloc[i]       = 1'b1;
        alloc_unit[i]  = U_FALU;
        alloc_entry[i] = rs_dp2falu[i].rob_entry;
        alloc_data[i]  = W'(rs_dp2falu[i]);
      end
`endif
      // Only uops that own a ROB entry produce exactly one result.
      alloc[i] = alloc[i] && uop_valid_dp2rob[i] && !flush;
    end
  end

  always_ff @(posedge clk) begin
    for (int i = 0; i < `NUM_DP_UOP; i++)
      if (alloc[i]) rpm[alloc_entry[i]] <= alloc_data[i];
  end

// ---copy-2 / replay issue (one per unit class per cycle)------------
  // Oldest pending entry first, counted from the ROB read pointer.
  logic [NU-1:0]          inj_any;
  logic [NU-1:0][DW-1:0]  inj_entry;
  logic [NU-1:0]          inj_slot_free;
  logic [NU-1:0]          inj_fire;

  always_comb begin
    for (int u = 0; u < NU; u++) begin
      inj_any[u]   = 1'b0;
      inj_entry[u] = '0;
      for (int k = 0; k < D; k++) begin
        logic [DW-1:0] e;
        e = rob_rptr + DW'(k);
        if (!inj_any[u] && vld[e] && (pend[e] != 2'd0) && (unit[e] == 2'(u))) begin
          inj_any[u]   = 1'b1;
          inj_entry[u] = e;
        end
      end
    end
    // Slot K is free for RS X when dispatch is not pushing slot K into X.
    // pushRdy[K] already accounts for dispatch pushes in slots 0..K-1.
    inj_slot_free[U_ALU] = !rs_valid_dp2alu[K] && rs_ready_alu2dp[K];
    inj_slot_free[U_MUL] = !rs_valid_dp2mul[K] && rs_ready_mul2dp[K];
    inj_slot_free[U_DIV] = !rs_valid_dp2div[K] && rs_ready_div2dp[K];
`ifdef ZVE32F_ON
    inj_slot_free[U_FALU] = !rs_valid_dp2falu[K] && rs_ready_falu2dp[K];
`else
    inj_slot_free[U_FALU] = 1'b0;
`endif
    for (int u = 0; u < NU; u++)
      inj_fire[u] = inj_any[u] && inj_slot_free[u] && !flush;
  end

  always_comb begin
    rs_valid_ft2alu = rs_valid_dp2alu;
    rs_ft2alu       = rs_dp2alu;
    rs_valid_ft2mul = rs_valid_dp2mul;
    rs_ft2mul       = rs_dp2mul;
    rs_valid_ft2div = rs_valid_dp2div;
    rs_ft2div       = rs_dp2div;
    if (inj_fire[U_ALU]) begin
      rs_valid_ft2alu[K] = 1'b1;
      rs_ft2alu[K]       = ALU_RS_t'(rpm[inj_entry[U_ALU]][W_ALU-1:0]);
    end
    if (inj_fire[U_MUL]) begin
      rs_valid_ft2mul[K] = 1'b1;
      rs_ft2mul[K]       = MUL_RS_t'(rpm[inj_entry[U_MUL]][W_MUL-1:0]);
    end
    if (inj_fire[U_DIV]) begin
      rs_valid_ft2div[K] = 1'b1;
      rs_ft2div[K]       = DIV_RS_t'(rpm[inj_entry[U_DIV]][W_DIV-1:0]);
    end
  end

`ifdef ZVE32F_ON
  always_comb begin
    rs_valid_ft2falu = rs_valid_dp2falu;
    rs_ft2falu       = rs_dp2falu;
    if (inj_fire[U_FALU]) begin
      rs_valid_ft2falu[K] = 1'b1;
      rs_ft2falu[K]       = FALU_RS_t'(rpm[inj_entry[U_FALU]][W_FALU-1:0]);
    end
  end
`endif

// ---results: pair, compare, forward--------------------------------
  localparam int NP = `NUM_SMPORT;

  logic [NP-1:0]    hit, first, has_partner, decide, store, match, fwd, trap_fire;
  logic [NP-1:0]    inj_mask;
  logic             trap_sent;   // a trap is out, waiting for the flush
  PU2ROB_t [NP-1:0] other;

  function automatic logic same_result(input PU2ROB_t a, input PU2ROB_t b);
    same_result = (a.w_data == b.w_data) && (a.w_valid == b.w_valid) &&
                  (a.vsaturate == b.vsaturate)
                `ifdef ZVE32F_ON
                  && (a.fpexp == b.fpexp)
                `endif
                  ;
  endfunction

  always_comb begin
    for (int j = 0; j < NP; j++)
      hit[j] = res_valid_arb2ft[j] && vld[res_arb2ft[j].rob_entry] && !flush;
  end

  always_comb begin
    for (int j = 0; j < NP; j++) begin
      logic [DW-1:0] e;
      e = res_arb2ft[j].rob_entry;
      first[j] = hit[j];
      for (int p = 0; p < j; p++)
        if (hit[p] && (res_arb2ft[p].rob_entry == e)) first[j] = 1'b0;
      has_partner[j] = 1'b0;
      other[j]       = hold[e];
      for (int p = j+1; p < NP; p++)
        if (!has_partner[j] && hit[p] && (res_arb2ft[p].rob_entry == e)) begin
          has_partner[j] = 1'b1;
          other[j]       = res_arb2ft[p];
        end
      decide[j] = first[j] && (has_partner[j] || have[e]);
      store[j]  = first[j] && !has_partner[j] && !have[e];
    end
  end

  // Fault injection for functional self-test (simulation only).
  //   +RVV_FT_INJ_N=<n>        flip bit 0 of one operand in the n-th comparison
  //   +RVV_FT_INJ_PERSIST=1    ... and in every later comparison (forces a trap)
`ifdef RVV_FT_INJECT_ON
  int unsigned inj_n, inj_persist, cmp_cnt;
  initial begin
    if (!$value$plusargs("RVV_FT_INJ_N=%d", inj_n))             inj_n = 0;
    if (!$value$plusargs("RVV_FT_INJ_PERSIST=%d", inj_persist)) inj_persist = 0;
  end
  always_comb begin
    int unsigned idx;
    idx = cmp_cnt;
    for (int j = 0; j < NP; j++) begin
      inj_mask[j] = 1'b0;
      if (decide[j]) begin
        idx = idx + 1;
        inj_mask[j] = (inj_n != 0) && ((inj_persist != 0) ? (idx >= inj_n) : (idx == inj_n));
      end
    end
  end
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) cmp_cnt <= 0;
    else        cmp_cnt <= cmp_cnt + $countones(decide);
  end
`else
  assign inj_mask = '0;
`endif

  always_comb begin
    for (int j = 0; j < NP; j++) begin
      PU2ROB_t cmp_b;
      cmp_b = other[j];
      if (inj_mask[j]) cmp_b.w_data[0] = ~cmp_b.w_data[0];
      match[j]     = decide[j] && same_result(res_arb2ft[j], cmp_b);
      fwd[j]       = decide[j] && match[j];
      trap_fire[j] = decide[j] && !match[j] &&
                     (retry[res_arb2ft[j].rob_entry] == RW'(RMAX));
      // Report one trap per flush; the scalar core flushes everything.
      for (int p = 0; p < j; p++) if (trap_fire[p]) trap_fire[j] = 1'b0;
      if (trap_sent) trap_fire[j] = 1'b0;
      // Non-FT results pass through; an FT entry reaches the ROB once,
      // after both executions agree.
      res_valid_ft2rob[j] = res_valid_arb2ft[j] && (!hit[j] || fwd[j]);
    end
  end
  assign res_ft2rob = res_arb2ft;

// ---state update----------------------------------------------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      vld   <= '0;
      unit  <= '0;
      pend  <= '0;
      have  <= '0;
      retry <= '0;
      tag   <= '0;
`ifdef TB_SUPPORT
      pc    <= '0;
`endif
    end else if (flush) begin
      vld  <= '0;
      pend <= '0;
      have <= '0;
    end else begin
      for (int i = 0; i < `NUM_DP_UOP; i++) begin
        if (alloc[i]) begin
          vld  [alloc_entry[i]] <= 1'b1;
          unit [alloc_entry[i]] <= alloc_unit[i];
          pend [alloc_entry[i]] <= 2'd1;      // copy-1 went out with dispatch
          have [alloc_entry[i]] <= 1'b0;
          retry[alloc_entry[i]] <= '0;
          tag  [alloc_entry[i]] <= uop_dp2rob[i].rob_tag;
`ifdef TB_SUPPORT
          pc   [alloc_entry[i]] <= uop_dp2rob[i].uop_pc;
`endif
        end
      end
      for (int u = 0; u < NU; u++)
        if (inj_fire[u]) pend[inj_entry[u]] <= pend[inj_entry[u]] - 2'd1;
      for (int j = 0; j < NP; j++) begin
        logic [DW-1:0] e;
        e = res_arb2ft[j].rob_entry;
        if (store[j]) have[e] <= 1'b1;
        if (decide[j]) begin
          have[e] <= 1'b0;
          if (match[j]) begin
            vld[e] <= 1'b0;                   // verified result goes to ROB
          end else if (!trap_fire[j]) begin
            pend [e] <= 2'd2;                 // replay both copies
            retry[e] <= retry[e] + 1'b1;
          end                                 // else: wait for trap flush
        end
      end
    end
  end

  always_ff @(posedge clk) begin
    for (int j = 0; j < NP; j++)
      if (store[j]) hold[res_arb2ft[j].rob_entry] <= res_arb2ft[j];
  end

// ---trap report-----------------------------------------------------
  // Sentinel instruction: opcode LOAD with all bits set (mew=1, reserved in
  // RVV 1.0), so mtval = 0xFFFF_FF87 cannot come from a real instruction.
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      trap_valid_o <= 1'b0;
      trap_data_o  <= '0;
      trap_sent    <= 1'b0;
    end else begin
      trap_valid_o <= |trap_fire && !flush;
      trap_sent    <= flush ? 1'b0 : (trap_sent | (|trap_fire));
      for (int j = NP-1; j >= 0; j--) begin
        if (trap_fire[j]) begin
          trap_data_o.rob_tag <= tag[res_arb2ft[j].rob_entry];
`ifdef TB_SUPPORT
          trap_data_o.pc      <= pc[res_arb2ft[j].rob_entry];
`else
          trap_data_o.pc      <= '0;
`endif
          trap_data_o.opcode  <= LOAD;
          trap_data_o.bits    <= '1;
        end
      end
    end
  end

// ---simulation-only observability-----------------------------------
`ifndef SYNTHESIS
  longint unsigned st_alloc, st_copy2, st_cmp, st_match, st_mismatch, st_trap;
  longint unsigned st_unit [NU];
  initial begin
    st_alloc = 0; st_copy2 = 0; st_cmp = 0; st_match = 0; st_mismatch = 0; st_trap = 0;
    for (int u = 0; u < NU; u++) st_unit[u] = 0;
  end
  always @(posedge clk) begin  // sim-only counters, kept across test resets
    if (rst_n) begin
      st_alloc    <= st_alloc    + $countones(alloc);
      st_copy2    <= st_copy2    + $countones(inj_fire);
      st_cmp      <= st_cmp      + $countones(decide);
      st_match    <= st_match    + $countones(fwd);
      st_mismatch <= st_mismatch + $countones(decide & ~match);
      st_trap     <= st_trap     + $countones(trap_fire);
      for (int u = 0; u < NU; u++) st_unit[u] <= st_unit[u] + 64'(inj_fire[u]);
    end
  end
`ifdef RVV_FT_INJECT_ON
  always_ff @(posedge clk) begin
    for (int j = 0; j < NP; j++) begin
      if (decide[j] && !match[j])
        $display("[RVV_FT] %0t mismatch rob_entry=%0d unit=%0d retry=%0d%s pc_a=%h pc_b=%h a=%h b=%h", $time,
                 res_arb2ft[j].rob_entry, unit[res_arb2ft[j].rob_entry], retry[res_arb2ft[j].rob_entry],
                 trap_fire[j] ? " -> TRAP" : " -> replay",
`ifdef TB_SUPPORT
                 res_arb2ft[j].uop_pc, other[j].uop_pc,
`else
                 0, 0,
`endif
                 res_arb2ft[j].w_data, other[j].w_data);
      if (trap_fire[j])
        $display("[RVV_FT] %0t trap_fire rob_entry=%0d rob_tag=%0d rptr=%0d", $time,
                 res_arb2ft[j].rob_entry, tag[res_arb2ft[j].rob_entry], rob_rptr);
      if (inj_mask[j])
        $display("[RVV_FT] %0t inject rob_entry=%0d", $time, res_arb2ft[j].rob_entry);
    end
  end
`endif
`ifdef RVV_FT_INJECT_ON
  logic flush_q;
  always_ff @(posedge clk) begin
    flush_q <= flush;
    if (flush && !flush_q) $display("[RVV_FT] %0t flush (vld=%b)", $time, vld);
    if (trap_valid_o) $display("[RVV_FT] %0t trap_valid_o rob_tag=%0d", $time, trap_data_o.rob_tag);
  end
`endif
  final begin
    $display("[RVV_FT] stats: alloc=%0d copy2_issued=%0d compares=%0d match=%0d mismatch=%0d trap=%0d",
             st_alloc, st_copy2, st_cmp, st_match, st_mismatch, st_trap);
    $display("[RVV_FT] copy2 by unit: alu=%0d mul=%0d div=%0d falu=%0d",
             st_unit[0], st_unit[1], st_unit[2], st_unit[3]);
  end
`endif

endmodule

`endif // RVV_FT_ON
