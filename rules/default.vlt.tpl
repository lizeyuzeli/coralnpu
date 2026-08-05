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
public_flat_rw -module "rvv_backend_rob"     -var "entry_valid"

// Expose the storage cell of the generic enable-DFF wrapper used throughout
// the RVV backend. One directive makes every edff instance depositable: the
// MAC / ALU / DIV / FALU pipeline registers AND the VRF storage cells (vreg
// is built from edff `q` slices). No per-signal vlt declarations needed.
public_flat_rw -module "edff"                -var "q"

// Same idea for the clear-on-c DFF wrapper (cdffr): exposes execution-unit
// datapath/control pipeline registers (div quotient/remainder, falu cmp_d1,
// per-unit valid/info delays, etc.). NOTE: the multi_fifo read/write pointers
// are also cdffr instances; the campaign registry filters those out by path
// so they are not treated as datapath/control fault targets.
public_flat_rw -module "cdffr"               -var "q"

// FIFO storage cell: one directive exposes the buffer of every multi_fifo
// instance (front-end command/legal/uop queues, per-unit reservation
// stations, result fifo). The campaign registry routes each instance to its
// owning module by hierarchical path.
public_flat_rw -module "multi_fifo"          -var "mem"