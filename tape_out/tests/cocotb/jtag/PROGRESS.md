# Core_Jtag_Chip JTAG-DMI cocotb suite — debugging progress

## Status

| Test                                       | Status |
| ------------------------------------------ | ------ |
| `dmactive`                                 | PASS   |
| `probe_impl`                               | PASS   |
| `hartsel`                                  | PASS   |
| `abstract_access_nonexistent_register`     | PASS   |
| `ndmreset`                                 | FAIL   |
| `halt_resume`                              | FAIL   |
| `abstract_access_registers`                | FAIL   |
| `single_step`                              | FAIL   |
| `breakpoint`                               | FAIL   |
| `scalar_registers`                         | TIMEOUT |

## Fixed: AXI loopback hang

**Symptom:** Every test hung at the first AXI write (`load_elf`, `write_word`)
with `slave_wagent timeout waiting for wready` after 4096 aclk cycles.

**Root cause:** The LVDS link's `RstSync` + `AsyncFIFO` + `CreditTracker`
need a reset pulse *while aclk is ticking* to leave their power-on state.
The tutorial flow gets that for free via `Fixture.Create` +
`load_elf_and_lookup_symbols` (which `await reset()`s a second time after
the clock has started). The JTAG cocotb tests followed the
`tests/cocotb/core_mini_axi_debug.py` pattern (init / reset / start_clock,
no second reset) which works against `CoreMiniAxi` (no LVDS) but not
against `Core_Jtag_Chip` / `Core_Axi_Chip`.

**Fix:** new helper `JtagDmiInterface.start_clock_and_reset()` that
launches the clock and then drives a second `super().reset()`. All 10
tests now use it. Diagnostic-only smoke at `_smoke_axi.py` confirms the
pure-AXI path works on `Core_Jtag_Chip` once the second reset is in
place. Filed under `:smoke_axi_cocotb` (manual tag).

## Open: DMI requests stall once the core is halted

**Symptom:** Every failing test reaches `dm_wait_for_halted()` then hangs
at the next `dm_read_reg(0x7B0)` (DCSR). The JTAG-DMI driver retries
with exponential idle-cycles, then aborts with `DMI transaction stuck in
BUSY`. The four PASSing tests never enter halt mode and never hit this.

**Triage data:**

1. JTAG-DMI dm_read / dm_write to non-CSR-access targets (DMCONTROL,
   DMSTATUS, ABSTRACTCS) work flawlessly pre-halt — see PASSing tests.
2. Abstract command on a non-existent register also works (returns
   `cmderr=2` fast); the abstract-command machinery itself is fine.
3. The only common element among failing tests: **post-halt access to a
   real CSR / GPR via abstract command**.
4. Setting `DMI_VIA_CSR=1` (route `dm_read/dm_write` through the upstream
   AXI-CSR path instead of JTAG) **does not fix it** — the CSR-DMI path
   on `Core_Jtag_Chip` returns garbage from its very first dm_read,
   while it works fine on `Core_Axi_Chip`. So both DMI paths are
   degraded on `Core_Jtag_Chip` even though only one is exercised at a
   time per test.

**Hypothesis (most likely):**

The kernel arbitrates two DMI sources via a round-robin arbiter
(`CoreChipKernel.scala`):

```
val dmReqArbiter = Module(new CoralNPURRArbiter(new DebugModuleReqIO(p), 2))
dmReqArbiter.io.in(0) <> GateDecoupled(io.dm.req, dmEnable)        // dmi_jtag in Core_Chip
dmReqArbiter.io.in(1) <> GateDecoupled(csr.io.debug.req, dmEnable) // CSR-DMI
```

On `Core_Axi_Chip` the dmi_jtag pads are tied (tck=0, trst_n=1, tms=0,
td_i=0) so dmi_jtag never raises `dmi_req_valid_o`; in(0) is permanently
idle and the arbiter degenerates to "always pick in(1)".

On `Core_Jtag_Chip` the JTAG TAP is bit-banged, and even when "idle"
between scans the dmi_jtag wrapper may briefly raise dmi_req_valid_o
across CDC, or hold it asserted while a transaction is in-flight to the
DM. If the DM/arbiter handshake in that window is mishandled, in(0)
ends up "valid but never accepted", which:

- starves in(1) (CSR-DMI path) → the early garbage we observed in the
  `DMI_VIA_CSR=1` experiment;
- and on the JTAG path, keeps `dmi_resp_valid` low so the TAP-side
  scan returns BUSY indefinitely.

This dovetails with the observation that **only post-halt accesses fail
on JTAG-DMI**: pre-halt the kernel's own CSR/regfile accesses are fast
and the arbiter sees a steady stream of in(0) → DM → response, so no
starvation. Once the core halts, the DM serializes abstract commands
through a slower micro-sequence and any stuck-valid on in(0) becomes
visible.

## Suggested next steps

1. **Wave dump on `halt_resume`.** Add `waves=True` to the failing test
   and inspect, on a single time-axis, all of:
   - `dmReqArbiter.io.in(0).valid / .ready / .bits`
   - `dmReqArbiter.io.in(1).valid / .ready / .bits`
   - `dmi_jtag.dmi_req_valid_o / dmi_req_ready_i`
   - `kernel.io.dm.rsp.valid / .ready`
   - `dm.io.haltreq(0)` and `core.io.dm.debug_mode`
   - `cg.io.enable` and `rst_sync.io.clk_o`
   around the first post-halt `dm_read_reg(0x7B0)` (≈120 µs into the
   test). Confirm/refute the in(0)-stuck-valid hypothesis.

2. **If confirmed**, the minimal upstream fix lives in
   `CoreChipKernel.scala`. Either:
   - swap `CoralNPURRArbiter` for an `Arbiter` with **in(1) priority**
     (CSR path first), since in(0) is "remote/optional" hardware that
     should never starve software-driven CSR access; **or**
   - keep RR but add a "dmi_req_valid drops to 0 between scans" guard
     in dmi_jtag (vendor file — careful, that's pulp-platform code).

   The first option is local to our Chisel and should not regress
   `Core_Axi_Chip` (in(0) is permanently idle there anyway).

3. **After fix**, drop `_force_dmi_via_csr` knob and the
   `start_clock_and_reset` helper docstrings can shrink (still keep
   the helper itself — it codifies an LVDS bring-up requirement).

## Files touched in this session

- `tape_out/hdl/chisel/src/coralnpu/Core_Jtag_Chip.scala` — added
  dead-facade `io.dm` / `io.debug` bundles so the existing
  `CoreMiniAxiInterface.__init__` signal probe binds without hacks.
- `tape_out/coralnpu_test_utils/jtag_dmi_interface.py` — new
  `start_clock_and_reset()` helper; `idle_tcks=64` + exponential
  back-off in `_dmi_request`; `DMI_VIA_CSR=1` triage knob.
- `tape_out/tests/cocotb/jtag/core_jtag_chip_debug.py` — all 10 tests
  now `await iface.start_clock_and_reset()` after `await iface.reset()`.
- `tape_out/tests/cocotb/jtag/_smoke_axi.py` (new) + BUILD entry
  `:smoke_axi_cocotb` (manual tag) — pure-AXI smoke regression to keep
  the LVDS bring-up requirement from silently regressing.
