/*
description: 
1. the ROB module receives uop information from Dispatch unit and uop result from Processor Unit (PU).
2. the ROB module provides all status for dispatch unit to foreward operand from ROB.
3. the ROB module send retire request to retire unit.
4. the ROB module receives trap information from LSU and flush buffer(s)

feature list:
1. the ROB can receive 2 uop information form Dispatch unit at most per cycle.
2. the ROB can receive 9 uop result from PU at most per cycle.
    a. However, U-arch of RVV limit the result number from 9 to 8.
3. the ROB can send 4 retire uops to writeback unit at most per cycle.
4. the ROB infomation for dispatch need to be sorted, which depends on program order.
*/

`ifndef HDL_VERILOG_RVV_DESIGN_RVV_SVH
`include "rvv_backend.svh"
`endif

module rvv_backend_rob
(
    clk,
    rst_n,
    uop_valid_dp2rob,
    uop_dp2rob,
    uop_ready_rob2dp,
    rob_empty,
    rob_entry_rob2dp,
    wr_valid_pu2rob,
    wr_pu2rob,
    rd_valid_rob2rt,
    rd_rob2rt,
    rd_ready_rt2rob,
    rob_entry_rob2rt,
    uop_rob2dp,
    trap_valid_rmp2rob,
    trap_rob_entry_rmp2rob,
    trap_ready_rob2rmp,
    trap_ready_rvv2rvs,
    trap_flush_rvv
`ifdef FAULT_TOLERANT_ON
    ,
    reinject_freeslot_dp2rob,   // in : per-unit per-port free RS push slot mask this cycle
    reinject_valid_rob2dp,      // out: per-unit per-port replay request
    reinject_entry_rob2dp       // out: per-unit per-port physical ROB entry to replay
`endif
);
// global signal
    input   logic                   clk;
    input   logic                   rst_n;

// push uop infomation to ROB
// Dispatch to ROB
    input   logic     [`NUM_DP_UOP-1:0] uop_valid_dp2rob;
    input   DP2ROB_t  [`NUM_DP_UOP-1:0] uop_dp2rob;
    output  logic     [`NUM_DP_UOP-1:0] uop_ready_rob2dp;
    output  logic                       rob_empty;
    output  logic     [`ROB_DEPTH_WIDTH-1:0] rob_entry_rob2dp;

// push uop result to ROB
// PU to ROB
    input   logic     [`NUM_SMPORT-1:0] wr_valid_pu2rob;
    input   PU2ROB_t  [`NUM_SMPORT-1:0] wr_pu2rob;

// retire uops
// pop vd_data from ROB and write to VRF
    output  logic     [`NUM_RT_UOP-1:0] rd_valid_rob2rt;
    output  ROB2RT_t  [`NUM_RT_UOP-1:0] rd_rob2rt;
    input   logic     [`NUM_RT_UOP-1:0] rd_ready_rt2rob;
    output  logic     [`ROB_DEPTH_WIDTH-1:0] rob_entry_rob2rt;

// bypass all rob entries to Dispatch unit
// rob_entries must be in program order instead of entry_index
    output  ROB2DP_t  [`ROB_DEPTH-1:0]  uop_rob2dp;

// trap signal handshake
    input   logic                           trap_valid_rmp2rob;
    input   logic   [`ROB_DEPTH_WIDTH-1:0]  trap_rob_entry_rmp2rob;
    output  logic                           trap_ready_rob2rmp;
    output  logic                           trap_ready_rvv2rvs;
    output  logic                           trap_flush_rvv;

`ifdef FAULT_TOLERANT_ON
// DMR reinject command bus (ROB = brain, dispatch = hands)
// dispatch feeds a per-unit per-port free-slot mask; ROB fills slots with pending replays.
// unit index: 0=ALU, 1=MUL/MAC, 2=DIV (+FDIV), 3=FMA. port index: 0..NUM_DP_UOP-1 (the RS push port).
    input   logic [3:0][`NUM_DP_UOP-1:0]                   reinject_freeslot_dp2rob; // [unit][port] 1=free
    output  logic [3:0][`NUM_DP_UOP-1:0]                   reinject_valid_rob2dp;    // [unit][port] 1=replay this port
    output  logic [3:0][`NUM_DP_UOP-1:0][`ROB_DEPTH_WIDTH-1:0] reinject_entry_rob2dp; // [unit][port] physical entry
`endif

// ---internal signal definition--------------------------------------
    logic                               trap_in;

  // Uop info
    DP2ROB_t  [`NUM_RT_UOP-1:0]         uop_rob2rt;
    logic     [`NUM_RT_UOP-1:0]         uop_valid_rob2rt;
    DP2ROB_t  [`ROB_DEPTH-1:0]          uop_info;
    logic     [`ROB_DEPTH-1:0]          entry_valid;

    logic     [`ROB_DEPTH_WIDTH-1:0]    uop_wptr;
    logic     [`ROB_DEPTH_WIDTH-1:0]    uop_rptr;
    logic     [`NUM_DP_UOP-1:0]         uop_info_fifo_almost_full;

  // Uop result
    RES_ROB_t [`ROB_DEPTH-1:0]          res_mem;
    logic     [`ROB_DEPTH-1:0]          uop_done;

`ifdef FAULT_TOLERANT_ON
  // DMR fault-tolerance, physical-entry indexed (aligned with res_mem/uop_done/trap_flag)
    logic     [`ROB_DEPTH-1:0]          ft_flag;        // entry is fault-tolerant (mirror of is_ft, physical order)
    logic     [`ROB_DEPTH-1:0][1:0]     ft_unit;        // replay target unit, physical order (mirror of DP2ROB.ft_unit)
    logic     [`ROB_DEPTH-1:0]          got_first;      // first DMR copy result has arrived & been stored
    logic     [1:0]                     retry_cnt [`ROB_DEPTH-1:0]; // rollback count, trap when reaching FT_RETRY_MAX
    logic     [`ROB_DEPTH-1:0][1:0]     ft_reinject_pend; // copies still owed for this entry: copy-2 (=1) or both (=2)
  // per-entry combinational DMR decision (physical order), driven by ft_decode below
    logic     [`ROB_DEPTH-1:0]          ft_set_done;    // DMR check passed this cycle -> allow retire
    logic     [`ROB_DEPTH-1:0]          ft_set_first;   // first copy arrived this cycle -> latch, suppress done
    logic     [`ROB_DEPTH-1:0]          ft_rollback;    // DMR mismatch this cycle -> resend both copies
  // reinject bookkeeping (physical order)
    logic     [`ROB_DEPTH-1:0]          new_ft_push;        // FT entry first becomes valid -> owe copy-2
    logic     [`ROB_DEPTH-1:0][1:0]     reinject_accept;    // copies dispatched this cycle per entry (for pend decrement)
  // retry-exhaustion handoff: K rollbacks reached -> fall back to global trap (INV-5)
    logic     [`ROB_DEPTH-1:0]          ft_trap_req;        // this cycle's rollback would hit FT_RETRY_MAX
  // ---- TMR of the ROB baseline control state (25 bit) -----------------
  // uop_done / trap_flag / entry_valid / trap_ready are what the ROB state
  // machine runs on: a single upset in any of them is enough to derail it
  // (it is also the only analysis module where a `seu` produced SDC-critical
  // outcomes). Each is stored FT_TMR_COPIES times and read back through a
  // majority voter; the ORIGINAL name is kept for the voted net, so none of
  // the readers further down change. This is register-only TMR -- the D-side
  // logic cone stays shared, because what is being modelled is the control
  // bits themselves flipping, not the logic that computes them.
  // entry_valid lives inside multi_fifo's `mem`, so it is triplicated by
  // instantiating the fifo three times (the IP itself is not touched).
    `FT_KEEP logic [`FT_TMR_COPIES-1:0][`ROB_DEPTH-1:0]  uop_done_tmr;
    `FT_KEEP logic [`FT_TMR_COPIES-1:0][`ROB_DEPTH-1:0]  trap_flag_tmr;
    `FT_KEEP logic [`FT_TMR_COPIES-1:0]                  trap_ready_tmr;
             logic [`FT_TMR_COPIES-1:0][`ROB_DEPTH-1:0]  entry_valid_tmr;      // fifo_data per copy
             logic [`FT_TMR_COPIES-1:0][`NUM_RT_UOP-1:0] uop_valid_rob2rt_tmr; // dataout per copy
`ifdef FT_INJECT_ON
  // self-test: force a one-time mismatch per FT entry to exercise the
  // duplicate->compare->rollback->retry->recover chain (see config.svh).
    logic     [`ROB_DEPTH-1:0]          injected;           // fault already injected for this entry instance
    logic     [`ROB_DEPTH-1:0]          ft_inject_fire;     // injection forced this cycle
`endif
`endif

  // trap
    logic     [`ROB_DEPTH-1:0]          trap_flag;

  // temp signal
    logic     [`ROB_DEPTH_WIDTH-1:0]    wind_uop_wptr [`ROB_DEPTH-1:0];
    logic     [`ROB_DEPTH_WIDTH-1:0]    wind_uop_rptr [`ROB_DEPTH-1:0];
`ifdef FAULT_TOLERANT_ON
    localparam int NUM_SMPORT_IDX = (`NUM_SMPORT > 1) ? $clog2(`NUM_SMPORT) : 1;
`endif

    genvar                              i,j;
// ---code start------------------------------------------------------
`ifdef FT_TMR_INJECT_ON
  // ---- TMR self-test sweeper ---------------------------------------
  // Walks the whole (protected bit x copy) space in one simulation, one point
  // at a time: 25 bits = uop_done[8] + trap_flag[8] + trap_ready[1] +
  // entry_valid[8], times FT_TMR_COPIES. A point is corrupted for the first
  // half of its period and left alone for the second half, so each point tests
  // two things in turn -- the voter masking a live single-copy fault, and the
  // scrub pulling the beaten copy back to the majority once the fault stops.
  //
  // The injected value is the complement of the VOTED bit rather than a flip of
  // the copy's own bit. A flip can land on the value the copy was going to hold
  // anyway, which silently injects nothing and leaves that sweep point untested;
  // complement-of-majority is a guaranteed divergence at every point.
  localparam int FT_TMR_INJ_BITS   = 3*`ROB_DEPTH + 1;
  localparam int FT_TMR_INJ_POINTS = FT_TMR_INJ_BITS*`FT_TMR_COPIES;
  localparam int FT_TMR_PT_W       = $clog2(FT_TMR_INJ_POINTS+1);
  localparam int FT_TMR_CNT_W      = $clog2(`FT_TMR_INJ_PERIOD);

    // Free-running, deliberately NOT reset: the fixture pulses rst_n before
    // every ELF (~210 cycles apart), so a reset-able sweep counter never gets
    // past point 1 and the whole self-test silently degenerates into "corrupt
    // bit 0 forever". Initialised at declaration instead; this block is
    // simulation-only, so there is no flop to infer.
    logic [FT_TMR_CNT_W-1:0]        ft_tmr_inj_cnt = '0;
    logic [FT_TMR_PT_W-1:0]         ft_tmr_inj_pt  = '0;
    logic [FT_TMR_PT_W-1:0]         ft_tmr_inj_bit;   // 0..24, which protected bit
    logic [FT_TMR_PT_W-1:0]         ft_tmr_inj_copy;  // which of the three copies
    logic [FT_TMR_PT_W-1:0]         ft_tmr_inj_ent;   // entry index within its group
    logic                           ft_tmr_inj_act;   // corrupting right now
    logic                           ft_tmr_inj_act_q;
    logic                           ft_tmr_inj_done_q = 1'b0;
    logic                           ft_tmr_hit_done;
    logic                           ft_tmr_hit_trap;
    logic                           ft_tmr_hit_ready;
    logic                           ft_tmr_hit_valid;

  always_ff @(posedge clk) begin
      if (ft_tmr_inj_pt < FT_TMR_INJ_POINTS) begin
          if (ft_tmr_inj_cnt == `FT_TMR_INJ_PERIOD-1) begin
              ft_tmr_inj_cnt <= '0;
              ft_tmr_inj_pt  <= ft_tmr_inj_pt + 1'b1;
          end
          else
              ft_tmr_inj_cnt <= ft_tmr_inj_cnt + 1'b1;
      end
  end

  assign ft_tmr_inj_act  = (ft_tmr_inj_pt < FT_TMR_INJ_POINTS) &&
                           (ft_tmr_inj_cnt < (`FT_TMR_INJ_PERIOD/2));
  assign ft_tmr_inj_copy = ft_tmr_inj_pt % `FT_TMR_COPIES;
  assign ft_tmr_inj_bit  = ft_tmr_inj_pt / `FT_TMR_COPIES;
  assign ft_tmr_inj_ent  = (ft_tmr_inj_bit <   `ROB_DEPTH) ?  ft_tmr_inj_bit
                         : (ft_tmr_inj_bit < 2*`ROB_DEPTH) ?  ft_tmr_inj_bit -   `ROB_DEPTH
                                                           :  ft_tmr_inj_bit - (2*`ROB_DEPTH+1);

  assign ft_tmr_hit_done  = ft_tmr_inj_act && (ft_tmr_inj_bit <   `ROB_DEPTH);
  assign ft_tmr_hit_trap  = ft_tmr_inj_act && (ft_tmr_inj_bit >=  `ROB_DEPTH)
                                           && (ft_tmr_inj_bit < 2*`ROB_DEPTH);
  assign ft_tmr_hit_ready = ft_tmr_inj_act && (ft_tmr_inj_bit == 2*`ROB_DEPTH);
  assign ft_tmr_hit_valid = ft_tmr_inj_act && (ft_tmr_inj_bit >  2*`ROB_DEPTH);

  // The three flop groups are scrubbed, so once a point's injection window
  // closes they must re-converge on the next clock. Checking that here is what
  // separates "the voter hid the fault" from "the fault was also repaired" --
  // the regression result alone cannot tell those apart. entry_valid is absent
  // because its point is applied on the voter input, not on the fifo mem, so
  // its copies never actually diverge (see the fifo instantiation below).
  always_ff @(posedge clk) begin
      ft_tmr_inj_act_q <= ft_tmr_inj_act;
      if (rst_n && !ft_tmr_inj_act && !ft_tmr_inj_act_q) begin
          if ((uop_done_tmr[0]  != uop_done_tmr[1])  || (uop_done_tmr[1]  != uop_done_tmr[2]) ||
              (trap_flag_tmr[0] != trap_flag_tmr[1]) || (trap_flag_tmr[1] != trap_flag_tmr[2]) ||
              (trap_ready_tmr[0]!= trap_ready_tmr[1])|| (trap_ready_tmr[1]!= trap_ready_tmr[2]))
              $error("FT_TMR: copies failed to scrub back after point %0d", ft_tmr_inj_pt);
      end
  end

  // A silent pass means nothing unless the sweep actually reached the last
  // point: a run that ends mid-sweep looks exactly like a clean one. The point
  // trace plus the completion line are what make the regression result evidence
  // -- one line per point, 75 in total, so it stays readable in the test log.
  always_ff @(posedge clk) begin
      if ((ft_tmr_inj_pt == FT_TMR_INJ_POINTS) && !ft_tmr_inj_done_q) begin
          ft_tmr_inj_done_q <= 1'b1;
          $display("FT_TMR: sweep complete, %0d points covered", FT_TMR_INJ_POINTS);
      end
      else if (ft_tmr_inj_act && (ft_tmr_inj_cnt == '0))
          $display("FT_TMR: point %0d (bit %0d copy %0d)", ft_tmr_inj_pt, ft_tmr_inj_bit, ft_tmr_inj_copy);
  end

`endif
  // Uop info FIFO
    multi_fifo #(
        .T            (DP2ROB_t),
        .M            (`NUM_DP_UOP),
        .N            (`NUM_RT_UOP),
        .DEPTH        (`ROB_DEPTH),
        .ASYNC_RSTN   (1'b1),
        .CHAOS_PUSH   (1'b1),
        .FULL_PUSH    (1'b1)
    ) u_uop_info_fifo (
      // global
        .clk          (clk),
        .rst_n        (rst_n),
      // push side
        .push         (uop_valid_dp2rob),
        .datain       (uop_dp2rob),
        .full         (),
        .almost_full  (uop_info_fifo_almost_full),
      // pop side
        .pop          (rd_valid_rob2rt & rd_ready_rt2rob),
        .dataout      (uop_rob2rt),
        .empty        (rob_empty),
        .almost_empty (),
      // fifo info
        .clear        (trap_flush_rvv),
        .fifo_data    (uop_info),
        .wptr         (uop_wptr),
        .rptr         (uop_rptr),
        .entry_count  ()
    );

    assign rob_entry_rob2dp = uop_wptr;
    assign uop_ready_rob2dp = ~uop_info_fifo_almost_full;

  // entry valid
  // set if DP push uop into ROB
  // clear if RT pop uop from ROB
  // reset once flush ROB
`ifdef FAULT_TOLERANT_ON
  // TMR of entry_valid. Those 8 bit live inside multi_fifo's `mem`, and the IP
  // is deliberately not modified, so the fifo is instantiated FT_TMR_COPIES
  // times instead and its outputs are voted. The instance is tiny (T=logic:
  // 8 b of mem + 10 b of pointers), so the two extra copies cost ~36 FF and
  // come with independent pointers -- which incidentally protects half of the
  // ROB's fifo bookkeeping for free.
  // BOTH used outputs must be voted, not just fifo_data: `dataout` gates the
  // retire valids and trap_in, so leaving it unvoted would protect the
  // forwarding view of entry_valid while the retire path stayed exposed.
  // Every copy sees identical push/pop/clear inputs -- pop is derived from the
  // VOTED dataout -- so the three pointer sets cannot drift apart.
  // Note: unlike the flops below, a fifo copy cannot be scrubbed from the
  // majority (that would mean writing into the IP's mem); an upset bit is
  // outvoted but persists until POP_CLEAR clears the slot on retire.
  generate
    for (i=0; i<`FT_TMR_COPIES; i++) begin : gen_uop_valid_fifo_tmr
      `FT_KEEP multi_fifo #(
        .T            (logic),
        .M            (`NUM_DP_UOP),
        .N            (`NUM_RT_UOP),
        .DEPTH        (`ROB_DEPTH),
        .POP_CLEAR    (1'b1),
        .ASYNC_RSTN   (1'b1),
        .CHAOS_PUSH   (1'b1),
        .FULL_PUSH    (1'b1)
      ) u_uop_valid_fifo (
      // global
        .clk          (clk),
        .rst_n        (rst_n),
      // push side
        .push         (uop_valid_dp2rob),
        .datain       (uop_valid_dp2rob),
        .full         (),
        .almost_full  (),
      // pop side
        .pop          (rd_valid_rob2rt & rd_ready_rt2rob),
        .dataout      (uop_valid_rob2rt_tmr[i]),
        .empty        (),
        .almost_empty (),
      // fifo info
        .clear        (trap_flush_rvv),
        .fifo_data    (entry_valid_tmr[i]),
        .wptr         (),
        .rptr         (),
        .entry_count  ()
      );
    end
  endgenerate

`ifdef FT_TMR_INJECT_ON
  // entry_valid's sweep points are applied here, on the voter input, because the
  // bits themselves are inside the untouched IP's mem. For the voter this is
  // indistinguishable from a corrupted mem bit; what it does not reproduce is
  // the persistence of a real mem upset, which would survive until POP_CLEAR
  // rather than until the injection window closes. That gap is the price of not
  // forking multi_fifo, and it is on the "outvoted" side of the test, not the
  // "detected" side -- see INV-6 in the FI plan for the same caveat.
  // The corrupted value is taken as the complement of the NEXT copy's bit, not
  // of the voter output: the voter output feeds back here, so complementing it
  // would close a combinational loop. Copies never diverge on their own, so the
  // neighbour carries the same value the majority does.
    logic [`FT_TMR_COPIES-1:0][`ROB_DEPTH-1:0] entry_valid_tmr_inj;
  generate
    for (i=0; i<`FT_TMR_COPIES; i++) begin : gen_entry_valid_tmr_inj
      always_comb begin
          entry_valid_tmr_inj[i] = entry_valid_tmr[i];
          if (ft_tmr_hit_valid && (ft_tmr_inj_copy==i))
              entry_valid_tmr_inj[i][ft_tmr_inj_ent] =
                  ~entry_valid_tmr[(i+1)%`FT_TMR_COPIES][ft_tmr_inj_ent];
      end
    end
  endgenerate
  `FT_KEEP ft_voter #(.WIDTH(`ROB_DEPTH))  u_vote_entry_valid (
        .d            (entry_valid_tmr_inj),
        .y            (entry_valid)
  );
`else
  `FT_KEEP ft_voter #(.WIDTH(`ROB_DEPTH))  u_vote_entry_valid (
        .d            (entry_valid_tmr),
        .y            (entry_valid)
  );
`endif
  `FT_KEEP ft_voter #(.WIDTH(`NUM_RT_UOP)) u_vote_uop_valid_rob2rt (
        .d            (uop_valid_rob2rt_tmr),
        .y            (uop_valid_rob2rt)
  );
`else
    multi_fifo #(
        .T            (logic),
        .M            (`NUM_DP_UOP),
        .N            (`NUM_RT_UOP),
        .DEPTH        (`ROB_DEPTH),
        .POP_CLEAR    (1'b1),
        .ASYNC_RSTN   (1'b1),
        .CHAOS_PUSH   (1'b1),
        .FULL_PUSH    (1'b1)
    ) u_uop_valid_fifo (
      // global
        .clk          (clk),
        .rst_n        (rst_n),
      // push side
        .push         (uop_valid_dp2rob),
        .datain       (uop_valid_dp2rob),
        .full         (),
        .almost_full  (),
      // pop side
        .pop          (rd_valid_rob2rt & rd_ready_rt2rob),
        .dataout      (uop_valid_rob2rt),
        .empty        (),
        .almost_empty (),
      // fifo info
        .clear        (trap_flush_rvv),
        .fifo_data    (entry_valid),
        .wptr         (),
        .rptr         (),
        .entry_count  ()
    );
`endif

  // update PU result to result memory
    always_ff @(posedge clk, negedge rst_n) begin
        if (!rst_n)
            res_mem <= 'b0;
        else begin
            for (int k=0; k<`NUM_SMPORT; k++) begin
                if (wr_valid_pu2rob[k]) begin
                  `ifdef TB_SUPPORT
                    res_mem[wr_pu2rob[k].rob_entry].uop_pc    <= wr_pu2rob[k].uop_pc;
                  `endif                
                    res_mem[wr_pu2rob[k].rob_entry].w_valid   <= wr_pu2rob[k].w_valid;
                    res_mem[wr_pu2rob[k].rob_entry].w_data    <= wr_pu2rob[k].w_data;
                    res_mem[wr_pu2rob[k].rob_entry].vsaturate <= wr_pu2rob[k].vsaturate;
                  `ifdef ZVE32F_ON
                    res_mem[wr_pu2rob[k].rob_entry].fpexp     <= wr_pu2rob[k].fpexp;
                  `endif
                end
            end
        end
    end

  // uop done
  // set if PU update uop result
  // clear if RT pop uop reuslt from ROB
  // reset once flush ROB.

  // wind back pointer
     generate
         for (i=0; i<`ROB_DEPTH; i++) begin : gen_wind_uop_ptr
           assign wind_uop_rptr[i] = uop_rptr+i;
           assign wind_uop_wptr[i] = uop_wptr+i;
         end
     endgenerate

`ifdef FAULT_TOLERANT_ON
  // ---- DMR: mirror is_ft into physical-entry order --------------------
  // uop_info is program-order: uop_info[p] == mem[uop_rptr+p].
  // => physical entry E maps to program slot (E-uop_rptr), so
  //    uop_info[E-uop_rptr].is_ft is the is_ft of the uop physically at E,
  //    aligned with res_mem/uop_done/trap_flag (all physical-indexed).
  generate
    for (i=0; i<`ROB_DEPTH; i++) begin : gen_ft_flag
      assign ft_flag[i] = uop_info[(`ROB_DEPTH_WIDTH'(i) - uop_rptr)].is_ft;
      assign ft_unit[i] = uop_info[(`ROB_DEPTH_WIDTH'(i) - uop_rptr)].ft_unit;
    end
  endgenerate
  // new_ft_push: a FT entry just got its copy-1 pushed this cycle -> owes copy-2.
  // Decode it DIRECTLY from the dispatch push bus, NOT from an entry_valid rising
  // edge: at ROB saturation a physical slot can be retired (valid->0) and re-pushed
  // (valid->1) in the SAME cycle, so its valid never dips and a rising-edge detector
  // silently misses the push -> copy-2 is never owed -> the entry latches got_first
  // and hangs forever (never done, never trap). The push port for uop i lands at
  // physical entry uop_wptr + (#earlier ports that actually push to ROB), matching
  // dispatch's rob_address chain and multi_fifo's CHAOS_PUSH compaction.
  always_comb begin
    new_ft_push = '0;
    for (int i2=0; i2<`NUM_DP_UOP; i2++) begin
      automatic logic [`ROB_DEPTH_WIDTH-1:0] ent = uop_wptr;
      for (int j2=0; j2<i2; j2++)
        ent = ent + (`ROB_DEPTH_WIDTH)'(uop_valid_dp2rob[j2]);
      if (uop_valid_dp2rob[i2] & uop_dp2rob[i2].is_ft)
        new_ft_push[ent] = 1'b1;
    end
  end
`endif

`ifdef FAULT_TOLERANT_ON
  // TMR copies of uop_done. Every copy runs the same next-state logic off the
  // VOTED value, so the D-side cone stays shared (register-only TMR) and the
  // three copies cannot diverge on their own.
  // The unconditional `uop_done_tmr[i] <= uop_done` is the scrub, and it is
  // what makes a `seu` recoverable rather than merely outvoted: without it a
  // bit that no branch below writes would implicitly hold ITS OWN value, so an
  // upset copy would stay wrong until that entry happens to be written again,
  // and a second upset anywhere in those 8 bit would break the majority. With
  // it, the copy is rewritten from the majority on the very next clock.
  // The later assignments in this block override it bit-wise (NBA last-write),
  // exactly as they override the implicit hold in the FT-off version below.
  generate
    for (i=0; i<`FT_TMR_COPIES; i++) begin : gen_uop_done_tmr
     always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            uop_done_tmr[i] <= '0;
        else if (trap_flush_rvv)
            uop_done_tmr[i] <= '0;
        else begin
            uop_done_tmr[i] <= uop_done;               // scrub from the majority
            for (int k=0; k<`NUM_RT_UOP; k++) begin
                if (rd_valid_rob2rt[k] && rd_ready_rt2rob[k])
                    uop_done_tmr[i][wind_uop_rptr[k]] <= 1'b0;
            end
            for (int k=0; k<`NUM_SMPORT; k++) begin
                if (wr_valid_pu2rob[k])
                    if (!ft_flag[wr_pu2rob[k].rob_entry])  // FT entries gated by comparator below
                    uop_done_tmr[i][wr_pu2rob[k].rob_entry] <= 1'b1;
            end
            for (int e=0; e<`ROB_DEPTH; e++)
                if (ft_set_done[e]) uop_done_tmr[i][e] <= 1'b1;  // DMR check passed
`ifdef FT_TMR_INJECT_ON
            // Sweep point: last in the chain so it beats the scrub and every
            // functional write, i.e. it models the flop itself being held wrong
            // rather than a bad value arriving on its D. Kept inside the else
            // branch so reset and flush still dominate -- a point that lands
            // on a flush cycle simply goes untested that cycle, out of the
            // FT_TMR_INJ_PERIOD/2 cycles its window is wide.
            if (ft_tmr_hit_done && (ft_tmr_inj_copy==i))
                uop_done_tmr[i][ft_tmr_inj_ent] <= ~uop_done[ft_tmr_inj_ent];
`endif
        end
      end
    end
  endgenerate

  `FT_KEEP ft_voter #(.WIDTH(`ROB_DEPTH)) u_vote_uop_done (
        .d            (uop_done_tmr),
        .y            (uop_done)
  );
`else
     always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            uop_done <= '0;
        else if (trap_flush_rvv)
            uop_done <= '0;
        else begin
            for (int k=0; k<`NUM_RT_UOP; k++) begin
                if (rd_valid_rob2rt[k] && rd_ready_rt2rob[k])
                    uop_done[wind_uop_rptr[k]] <= 1'b0;
            end
            for (int k=0; k<`NUM_SMPORT; k++) begin
                if (wr_valid_pu2rob[k])
                    uop_done[wr_pu2rob[k].rob_entry] <= 1'b1;
            end
        end
    end
`endif

`ifdef FAULT_TOLERANT_ON
  // ---- DMR result-equality helpers ------------------------------------
  // Compare the architectural payload of two PU results / a result vs the
  // stored res_mem copy. Only fields that retire are compared.
    function automatic logic ft_res_eq (input PU2ROB_t a, input PU2ROB_t b);
        ft_res_eq = (a.w_valid   === b.w_valid)
                 && (a.w_data    === b.w_data)
                 && (a.vsaturate === b.vsaturate)
              `ifdef ZVE32F_ON
                 && (a.fpexp     === b.fpexp)
              `endif
                 ;
    endfunction
    function automatic logic ft_res_eq_mem (input PU2ROB_t a, input RES_ROB_t m);
        ft_res_eq_mem = (a.w_valid   === m.w_valid)
                     && (a.w_data    === m.w_data)
                     && (a.vsaturate === m.vsaturate)
                  `ifdef ZVE32F_ON
                     && (a.fpexp     === m.fpexp)
                  `endif
                     ;
    endfunction

  // ---- DMR three-branch entry comparator (combinational) --------------
  // For each physical entry that is fault-tolerant, count the PU results
  // landing on it THIS cycle and decide: pass (set_done) / latch first copy
  // (set_first) / mismatch (rollback). res_mem holds the first copy until the
  // clock edge, so branch (c) compares the incoming 2nd copy against res_mem.
    logic [`ROB_DEPTH-1:0]                       ft_hit_any;   // >=1 result this cycle
    logic [`ROB_DEPTH-1:0][1:0]                  ft_hit_cnt;   // 0..2 results this cycle
    logic [`ROB_DEPTH-1:0][NUM_SMPORT_IDX-1:0]  ft_k0, ft_k1; // first / second smport idx
    always_comb begin
        ft_set_done  = '0;
        ft_set_first = '0;
        ft_rollback  = '0;
      `ifdef FT_INJECT_ON
        ft_inject_fire = '0;
      `endif
        for (int e=0; e<`ROB_DEPTH; e++) begin
            ft_hit_any[e] = 1'b0;
            ft_hit_cnt[e] = 2'd0;
            ft_k0[e]      = '0;
            ft_k1[e]      = '0;
            for (int k=0; k<`NUM_SMPORT; k++) begin
                if (wr_valid_pu2rob[k] && (wr_pu2rob[k].rob_entry == (`ROB_DEPTH_WIDTH)'(e))) begin
                    if (!ft_hit_any[e]) ft_k0[e] = (NUM_SMPORT_IDX)'(k);
                    else                ft_k1[e] = (NUM_SMPORT_IDX)'(k);
                    ft_hit_any[e] = 1'b1;
                    ft_hit_cnt[e] = ft_hit_cnt[e] + 2'd1;
                end
            end
            if (ft_flag[e]) begin
              `ifdef FT_INJECT_ON
                // self-test: on the entry's FIRST real comparison (2-same-cycle, or
                // the 2nd copy arriving after got_first), force a one-time mismatch.
                if (!injected[e] && ((ft_hit_cnt[e] == 2'd2) ||
                                     ((ft_hit_cnt[e] == 2'd1) && got_first[e])))
                    ft_inject_fire[e] = 1'b1;
              `endif
                if (ft_hit_cnt[e] == 2'd2) begin            // branch (a): two same-cycle
                    if (ft_res_eq(wr_pu2rob[ft_k0[e]], wr_pu2rob[ft_k1[e]])
                      `ifdef FT_INJECT_ON
                        && !ft_inject_fire[e]
                      `endif
                       )
                        ft_set_done[e] = 1'b1;
                    else
                        ft_rollback[e] = 1'b1;
                end else if (ft_hit_cnt[e] == 2'd1) begin
                    if (!got_first[e])                       // branch (b): first copy
                        ft_set_first[e] = 1'b1;
                    else if (ft_res_eq_mem(wr_pu2rob[ft_k0[e]], res_mem[e]) // branch (c)
                      `ifdef FT_INJECT_ON
                        && !ft_inject_fire[e]
                      `endif
                       )
                        ft_set_done[e] = 1'b1;
                    else
                        ft_rollback[e] = 1'b1;
                end
            end
        end
    end

  // ---- DMR per-entry state: got_first / retry_cnt ---------------------
  // got_first: set when the first copy is latched (branch b); cleared on
  //   pass/rollback (entry resolved) or when the entry retires/flushes.
  // retry_cnt: bumped on each rollback; reaching FT_RETRY_MAX hands off to
  //   the existing global trap path (wired in a later step).
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            got_first <= '0;
            for (int e=0; e<`ROB_DEPTH; e++) retry_cnt[e] <= 2'd0;
        end else if (trap_flush_rvv) begin
            got_first <= '0;
            for (int e=0; e<`ROB_DEPTH; e++) retry_cnt[e] <= 2'd0;
        end else begin
            for (int e=0; e<`ROB_DEPTH; e++) begin
                if (ft_set_first[e])
                    got_first[e] <= 1'b1;
                else if (ft_set_done[e] || ft_rollback[e])
                    got_first[e] <= 1'b0;          // entry resolved this cycle
                if (ft_rollback[e])
                    retry_cnt[e] <= retry_cnt[e] + 2'd1;
                else if (ft_set_done[e])
                    retry_cnt[e] <= 2'd0;          // clean finish, reset for entry reuse
            end
        end
    end

  // ---- DMR retry exhaustion -> global trap handoff (INV-5) ------------
  // A rollback while retry_cnt already sits at the last allowed value means
  // re-execution has failed FT_RETRY_MAX times; stop retrying and fall back to
  // the existing global trap path (trap_flag -> retire -> trap_flush_rvv).
  generate
    for (i=0; i<`ROB_DEPTH; i++) begin : gen_ft_trap_req
      assign ft_trap_req[i] = ft_rollback[i] &
                              (retry_cnt[i] >= (`FT_RETRY_MAX-1));
    end
  endgenerate

`ifdef FT_INJECT_ON
  // ---- DMR self-test: one-time fault injection bookkeeping ------------
  // injected[e] set the cycle a fault is forced; cleared when the entry slot is
  // (re)allocated by a fresh FT push or on flush, so each entry instance is
  // injected at most once (see config.svh: must be once, else trap loop).
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            injected <= '0;
        else if (trap_flush_rvv)
            injected <= '0;
        else begin
            for (int e=0; e<`ROB_DEPTH; e++) begin
                if (new_ft_push[e])      injected[e] <= 1'b0; // slot reallocated
                else if (ft_inject_fire[e]) injected[e] <= 1'b1;
            end
        end
    end
`endif

  // ---- DMR reinject pending counter (physical order) ------------------
  // copies still owed for an entry: a fresh FT push owes copy-2 (=1); a
  // rollback invalidates both stored copies and owes both (=2). Each accepted
  // handshake (dispatch grabbed a free RS slot) decrements; resolve/flush clears.
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            for (int e=0; e<`ROB_DEPTH; e++) ft_reinject_pend[e] <= 2'd0;
        else if (trap_flush_rvv)
            for (int e=0; e<`ROB_DEPTH; e++) ft_reinject_pend[e] <= 2'd0;
        else begin
            for (int e=0; e<`ROB_DEPTH; e++) begin
                if (ft_set_done[e])
                    ft_reinject_pend[e] <= 2'd0;               // entry resolved
                else if (ft_rollback[e])
                    ft_reinject_pend[e] <= 2'd2;               // resend both copies
                else if (new_ft_push[e])
                    ft_reinject_pend[e] <= 2'd1;               // owe copy-2
                else
                    ft_reinject_pend[e] <= ft_reinject_pend[e] - reinject_accept[e];
            end
        end
    end

  // ---- DMR multi-issue reinject arbiter (combinational) ---------------
  // Walk entries in PROGRAM order (oldest first). For each owed copy, find a
  // free RS push port on its target unit (mask from dispatch) and claim it.
  // ROB is the brain: it only decides what/where; dispatch (hands) does the
  // actual RS MUX. Multi-issue: up to NUM_DP_UOP ports per unit per cycle, so
  // all 4 units (ALU/MUL/DIV+FDIV/FMA) fill in parallel for max replay throughput.
    logic [`NUM_DP_UOP-1:0] ft_port_used [3:0]; // per-unit ports already claimed this cycle
    always_comb begin
        reinject_valid_rob2dp = '0;
        reinject_entry_rob2dp = '0;
        for (int e=0; e<`ROB_DEPTH; e++) reinject_accept[e] = 2'd0;
        for (int u=0; u<4; u++)          ft_port_used[u] = '0;
        for (int p=0; p<`ROB_DEPTH; p++) begin
            automatic logic [`ROB_DEPTH_WIDTH-1:0] e = wind_uop_rptr[p]; // program slot p -> physical entry
            automatic logic [1:0] u = ft_unit[e];
            if (ft_flag[e]) begin
                for (int c=0; c<2; c++) begin                       // up to 2 copies owed
                    if (reinject_accept[e] < ft_reinject_pend[e]) begin
                        // grab the lowest free, not-yet-claimed port on unit u
                        for (int pt=0; pt<`NUM_DP_UOP; pt++) begin
                            if ((reinject_accept[e] < ft_reinject_pend[e]) &&
                                reinject_freeslot_dp2rob[u][pt] &&
                                !ft_port_used[u][pt]) begin
                                reinject_valid_rob2dp[u][pt] = 1'b1;
                                reinject_entry_rob2dp[u][pt] = e;
                                ft_port_used[u][pt]          = 1'b1;
                                reinject_accept[e]           = reinject_accept[e] + 2'd1;
                            end
                        end
                    end
                end
            end
        end
    end
`endif

  `ifdef ASSERT_ON
    logic [`ROB_DEPTH-1:0][`NUM_SMPORT-1:0] res_sel; // one hot code for each entry
    generate
        for (i=0; i<`ROB_DEPTH; i++) begin : gen_res_sel
            for (j=0; j<`NUM_SMPORT; j++) begin : gen_smport    
                assign res_sel[i][j] = wr_valid_pu2rob[j] && (wr_pu2rob[j].rob_entry == i);
            end

            `rvv_expect($onehot0(res_sel[i])
                      `ifdef FAULT_TOLERANT_ON
                        || ft_flag[i]   // DMR: two same-cycle results on a FT entry is legal (branch a)
                      `endif
                       )
            else $error("ROB: Multiple PU results write same entry: index %d, PU %d\n", i, $sampled(res_sel[i]));

        end
    endgenerate
  `endif

  `ifdef ASSERT_ON
    generate
      for (i=0; i<`ROB_DEPTH; i++) begin : gen_res_write_check
        `rvv_forbid( uop_done[wind_uop_rptr[i]] && !entry_valid[i] )
        else $error("ROB: Write back to ROB entry[%d] while entry is invalid", i);

      `ifdef TB_SUPPORT
        `rvv_forbid( uop_done[wind_uop_rptr[i]] && entry_valid[i] && (res_mem[wind_uop_rptr[i]].uop_pc !== uop_info[i].uop_pc) )
        else $error("ROB: Result pc written back to ROB is mismacth: res_mem[%d].uop_pc(0x%08x) != uop_info[%d].uop_pc(0x%08x)", i, res_mem[wind_uop_rptr[i]].uop_pc, i, uop_info[i].uop_pc);
      `endif
      end
    endgenerate
  `endif

  // trap flag
  // write trap to ROB when trap occurs
  // flush all fifo when the uop triggering trap is the oldest uop in ROB
`ifdef FAULT_TOLERANT_ON
  // TMR copies of trap_flag, same shape as uop_done above: shared D-side cone
  // fed from the voted value, plus the scrub assignment that lets a single
  // upset be corrected instead of merely outvoted.
  // Two set sources have to live in this one block, and both have to stay
  // INSIDE the else arm rather than after the if/else chain: a statement after
  // the chain also executes on the !rst_n branch, so the async reset would stop
  // dominating and no legal flop can be inferred (DC rejects it). They must
  // also be siblings rather than `else if`, because ft_trap_req is a
  // single-cycle pulse -- dropping it when a trap_valid_rmp2rob lands in the
  // same cycle would strand the entry with retry_cnt at the limit.
  generate
    for (i=0; i<`FT_TMR_COPIES; i++) begin : gen_trap_flag_tmr
      always_ff @(posedge clk or negedge rst_n) begin
          if (!rst_n)
              trap_flag_tmr[i] <= '0;
          else if (trap_flush_rvv)
              trap_flag_tmr[i] <= '0;
          else begin
              trap_flag_tmr[i] <= trap_flag;           // scrub from the majority
              if (trap_valid_rmp2rob & trap_ready_rob2rmp)
                  trap_flag_tmr[i][trap_rob_entry_rmp2rob] <= 1'b1;
              // DMR retry exhaustion: K failed re-executions -> mark entry
              // trapped so it retires as a trap and triggers the existing
              // global flush fallback (INV-5).
              for (int e=0; e<`ROB_DEPTH; e++)
                  if (ft_trap_req[e]) trap_flag_tmr[i][e] <= 1'b1;
`ifdef FT_TMR_INJECT_ON
              // Sweep point, same placement rationale as uop_done above. This
              // is the sharpest of the four: a stuck trap_flag on a live entry
              // retires it as a trap and flushes the machine, so if the voter
              // ever let one copy through, the regression would not merely
              // mismatch, it would take the trap path.
              if (ft_tmr_hit_trap && (ft_tmr_inj_copy==i))
                  trap_flag_tmr[i][ft_tmr_inj_ent] <= ~trap_flag[ft_tmr_inj_ent];
`endif
          end
      end
    end
  endgenerate

  `FT_KEEP ft_voter #(.WIDTH(`ROB_DEPTH)) u_vote_trap_flag (
        .d            (trap_flag_tmr),
        .y            (trap_flag)
  );
`else
  always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n)
          trap_flag <= '0;
      else if (trap_flush_rvv)
          trap_flag <= '0;
      else if (trap_valid_rmp2rob & trap_ready_rob2rmp)
          trap_flag[trap_rob_entry_rmp2rob] <= 1'b1;
  end
`endif

  // trap ready is always 1
  assign trap_ready_rob2rmp = 1'b1;

  // retire uop(s)
  generate
      for (i=0; i<`NUM_RT_UOP; i++) begin : gen_rob2rt
        // retire_uop valid
          if (i==0) begin : gen_0
            assign rd_valid_rob2rt[0] = uop_valid_rob2rt[0] & (uop_done[wind_uop_rptr[0]]|trap_flag[wind_uop_rptr[i]]);
          end else begin : gen_i
            assign rd_valid_rob2rt[i] = uop_valid_rob2rt[i] & uop_done[wind_uop_rptr[i]] & rd_valid_rob2rt[i-1] & ~trap_flag[wind_uop_rptr[i]-1'b1];
          end
        // retire_uop data
        `ifdef TB_SUPPORT          
          assign rd_rob2rt[i].uop_pc          = uop_rob2rt[i].uop_pc;
          assign rd_rob2rt[i].last_uop_valid  = uop_rob2rt[i].last_uop_valid;
        `endif          
          assign rd_rob2rt[i].w_valid         = res_mem[wind_uop_rptr[i]].w_valid & uop_done[wind_uop_rptr[i]];
          assign rd_rob2rt[i].w_index         = uop_rob2rt[i].w_index;
          assign rd_rob2rt[i].w_data          = res_mem[wind_uop_rptr[i]].w_data;
          assign rd_rob2rt[i].w_type          = uop_rob2rt[i].w_type;
          assign rd_rob2rt[i].vd_type         = uop_rob2rt[i].byte_type;
          assign rd_rob2rt[i].trap_flag       = trap_flag[wind_uop_rptr[i]];
          assign rd_rob2rt[i].vector_csr      = uop_rob2rt[i].vector_csr;
          assign rd_rob2rt[i].vxsaturate      = res_mem[wind_uop_rptr[i]].vsaturate;
        `ifdef ZVE32F_ON
          assign rd_rob2rt[i].fpexp           = res_mem[wind_uop_rptr[i]].fpexp;
        `endif
      end
  endgenerate

  assign rob_entry_rob2rt = uop_rptr;
  
  // trap handle ready and flush signal
  assign trap_in = uop_valid_rob2rt[0] & rd_rob2rt[0].trap_flag & rd_ready_rt2rob[0];
`ifdef FAULT_TOLERANT_ON
  // TMR of the trap handshake flop -- one bit, and the most consequential one
  // in the ROB: it drives trap_flush_rvv, which flushes the whole backend, and
  // its `.e(1'b1)` means it is written every single cycle. Measured on the bare
  // core, an upset here goes straight to DUE-hang.
  // The d input feeds back the VOTED output, not each copy's own q, so the
  // three copies can never latch different halves of the two-cycle handshake
  // and an upset copy is pulled back into line on the next clock -- the same
  // self-correcting property the scrub gives the vectors above, for free.
  generate
    for (i=0; i<`FT_TMR_COPIES; i++) begin : gen_trap_ready_tmr
`ifdef FT_TMR_INJECT_ON
      // Sweep point. edff is IP, so the corruption goes on the D input instead
      // of the flop -- with `.e(1'b1)` the flop is written every cycle, so a
      // held-complement D is indistinguishable at q from a held-wrong flop.
      `FT_KEEP edff trap_ready (.q(trap_ready_tmr[i]), .d((trap_in&(!trap_ready_rvv2rvs)) ^ (ft_tmr_hit_ready && (ft_tmr_inj_copy==i))), .e(1'b1), .clk(clk), .rst_n(rst_n));
`else
      `FT_KEEP edff trap_ready (.q(trap_ready_tmr[i]), .d(trap_in&(!trap_ready_rvv2rvs)), .e(1'b1), .clk(clk), .rst_n(rst_n));
`endif
    end
  endgenerate
  `FT_KEEP ft_voter #(.WIDTH(1)) u_vote_trap_ready (
        .d            (trap_ready_tmr),
        .y            (trap_ready_rvv2rvs)
  );
`else
  edff trap_ready (.q(trap_ready_rvv2rvs), .d(trap_in&(!trap_ready_rvv2rvs)), .e(1'b1), .clk(clk), .rst_n(rst_n));
`endif
  assign trap_flush_rvv = trap_in||trap_ready_rvv2rvs; // flush 2 cycles

  // bypass ROB info to Dispatch
  generate
      for (i=0; i<`ROB_DEPTH; i++) begin : gen_rob2dp
        `ifdef TB_SUPPORT
          assign uop_rob2dp[i].uop_pc  = uop_info[i].uop_pc;
        `endif
          assign uop_rob2dp[i].valid   = entry_valid[i];
          assign uop_rob2dp[i].w_valid = res_mem[wind_uop_rptr[i]].w_valid & uop_done[wind_uop_rptr[i]];
          assign uop_rob2dp[i].w_index = uop_info[i].w_index;
          assign uop_rob2dp[i].w_type  = uop_info[i].w_type;
          assign uop_rob2dp[i].w_data  = res_mem[wind_uop_rptr[i]].w_data;
          assign uop_rob2dp[i].byte_type = uop_info[i].byte_type;
          assign uop_rob2dp[i].vector_csr = uop_info[i].vector_csr;
      end
  endgenerate
  
endmodule
