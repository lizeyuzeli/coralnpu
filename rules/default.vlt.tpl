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
public_flat_rw -module "rvv_backend_vrf_reg" -var "vreg"
public_flat_rw -module "rvv_backend_rob"     -var "uop_done"
public_flat_rw -module "rvv_backend_rob"     -var "trap_flag"
public_flat_rw -module "rvv_backend_rob"     -var "entry_valid"

// Expose the storage cell of the generic enable-DFF wrapper used throughout
// the RVV backend. This makes all MAC / ALU / DIV pipeline registers
// observable & depositable via VPI without per-signal vlt declarations.
public_flat_rw -module "edff"                -var "q"