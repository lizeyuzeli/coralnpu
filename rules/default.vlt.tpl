`verilator_config
// Typical Chisel-defined IO
public -module "{HDL_TOPLEVEL}" -var "io_*"

// Common clock names
public -module "{HDL_TOPLEVEL}" -var "clock"
public -module "{HDL_TOPLEVEL}" -var "clk"

// Common reset names
public -module "{HDL_TOPLEVEL}" -var "reset"
public -module "{HDL_TOPLEVEL}" -var "rst"
public -module "{HDL_TOPLEVEL}" -var "rst_ni"

// Fault-injection: expose selected RVV-backend state so cocotb can deposit
// single-bit upsets via VPI. Harmless for builds that do not include these
// modules (Verilator emits at most a warning and ignores the directive).
// `public_flat_rw` keeps the variable accessible via VPI even if Verilator
// inlines the wrapping modules into the toplevel.
//
// ROB control state (the compute_ctrl module's targets). The VRF storage is
// NOT exposed as the `vreg` net: that net is driven by the edff `q` cells
// below, so a deposit onto it would be overwritten by the edff output. The
// storage module injects those edff `q` flip-flops instead.
public_flat_rw -module "rvv_backend_rob"     -var "uop_done"
public_flat_rw -module "rvv_backend_rob"     -var "trap_flag"
// FAULT_TOLERANT_ON only: the TMR copies of those same two registers (Stage 3
// of the DMR plan triplicates the ROB's 25 control bits). On an FT_ON build
// `uop_done` / `trap_flag` above still exist but are the majority voter's
// combinational OUTPUT, so depositing on them is recomputed away -- the
// storage is these `*_tmr` vectors, and the campaign registry switches to
// them automatically (fi_utils `sources_ft`). Absent on an FT_OFF build,
// where Verilator ignores the directive with at most a warning.
// The other two of the four are reached without a directive of their own:
// entry_valid lives in the triplicated u_uop_valid_fifo (multi_fifo `mem`,
// covered below) and trap_ready in three edff copies (`q`, covered below).
public_flat_rw -module "rvv_backend_rob"     -var "uop_done_tmr"
public_flat_rw -module "rvv_backend_rob"     -var "trap_flag_tmr"
// `entry_valid` is deliberately NOT exposed, for the same reason as `vreg`:
// it is the combinational `fifo_data` output of u_uop_valid_fifo, so a deposit
// onto it is recomputed away before it can propagate (measured at landed 0/4
// by the deposit probe). The compute_ctrl module injects that fifo's `mem`.
// ROB data plane (result memory). Its companion, the uop info buffer, needs no
// directive of its own -- it is a multi_fifo, covered by the `mem` line below.
public_flat_rw -module "rvv_backend_rob"     -var "res_mem"

// FAULT_TOLERANT_ON only: the rest of the DMR/TMR bookkeeping, all of it real
// flip-flops that exist only in that build. Every one of these was outside the
// campaign's fault space until the Stage 5 space audit, which means the FT_ON
// denominator counted the mechanism's benefit without counting its silicon.
//   got_first_tmr / ft_reinject_pend_tmr  triplicated (Stage 5a), 24 + 48 bit
//   retry_cnt                             NOT triplicated, 8 x 2 bit
// The CE instrument (ft_ce_cnt, ft_tmr_disagree_q) is deliberately left
// unexposed: nothing in the design reads it, so injecting it could only add
// guaranteed-MASKED bits to the denominator. It gets a directive when the CE
// reporting CSR gives it a reader.
public_flat_rw -module "rvv_backend_rob"     -var "got_first_tmr"
public_flat_rw -module "rvv_backend_rob"     -var "ft_reinject_pend_tmr"
public_flat_rw -module "rvv_backend_rob"     -var "retry_cnt"

// FAULT_TOLERANT_ON only: the DMR replay buffer (rvv_backend.sv root scope,
// ROB_DEPTH x FT_RS_W). It holds a shadow copy of each in-flight RS payload so
// a mismatched pair can be re-issued, which makes it both the largest single
// thing FT adds and the mechanism's common-mode point -- a fault here is
// replayed into BOTH copies, so DMR cannot see it. Exposing it is what lets
// the execute module's FT_ON fault space include the cost of the scheme
// instead of only its benefit.
public_flat_rw -module "rvv_backend"         -var "replay_mem"

// Expose the storage cell of the generic enable-DFF wrapper used throughout
// the RVV backend. One directive makes every edff instance depositable: the
// MAC / ALU / DIV / FALU pipeline registers AND the VRF storage cells (vreg
// is built from edff `q` slices). No per-signal vlt declarations needed.
public_flat_rw -module "edff"                -var "q"

// Same idea for the clear-on-c DFF wrapper (cdffr): exposes execution-unit
// datapath/control pipeline registers (div quotient/remainder, falu cmp_d1,
// per-unit valid/info delays, etc.). NOTE: the multi_fifo read/write pointers
// are also cdffr instances; the campaign registry never reaches them from a
// datapath module (those walk `mem` under a fifo, not `q`) and measures them
// separately as the `fifo_ptr` diagnostic item.
public_flat_rw -module "cdffr"               -var "q"

// FIFO storage cell: one directive exposes the buffer of every multi_fifo
// instance (front-end command/legal/uop queues, per-unit reservation
// stations, result fifo). The campaign registry routes each instance to its
// owning module by hierarchical path.
public_flat_rw -module "multi_fifo"          -var "mem"

// Write-enable / clear inputs of the two DFF wrappers. These are NOT fault
// targets -- the campaign never injects on them. They are exposed read-side
// so the injection framework can tell the two failure modes of a deposit
// apart: a flipped `q` that is gone one cycle later is physically correct
// when `e`(or `c`) was asserted (the design simply wrote the cell), but is a
// framework bug when it was not. The deposit positive control keys its
// verdict on this, and the `set` fault model needs the write-enable edge to
// place a combinational upset where a real one would be latched.
public_flat_rw -module "edff"                -var "e"
public_flat_rw -module "cdffr"               -var "e"
public_flat_rw -module "cdffr"               -var "c"