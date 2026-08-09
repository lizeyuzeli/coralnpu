# RVV Core Module-Level Fault-Injection Framework

A cocotb + Verilator-VPI fault-injection (FI) harness for **module-level
vulnerability analysis** of the coralnpu RVV core. It partitions the RVV
backend into **four sub-modules**, injects single-bit faults into the full
state of one sub-module at a time, and classifies each run into a
**three-layer / six-bucket** outcome taxonomy. The four sub-modules map
one-to-one onto the planned fault-tolerance (FT) schemes, so the vulnerability
profile reads directly as "which sub-module needs which protection".

This harness analyses the **unprotected design** (`FAULT_TOLERANT_ON` OFF).
Once the FT schemes land, re-running the same seed sweep with FT ON and
diffing the SDC/DUE columns quantifies their effect — same framework, no new
code.

---

## 1. The four sub-modules

| `FI_MODULE` | Fault space (every targeted FF/SRAM in the sub-module) | FT scheme |
|---|---|---|
| `decode_path` | Front-end queues: command / legal-command / uop queue buffers (`multi_fifo.mem`). Three serial stages, each a different payload form, all injected. | TMR? |
| `compute_ctrl` | ROB **control** state: `uop_done`, `trap_flag`, the entry-valid bits (from `u_uop_valid_fifo.mem`, not the `entry_valid` net — see below), and the `trap_ready` flop that drives the global flush. Pointers excluded. | TMR |
| `execute` | All execution-unit pipeline FFs (`edff.q` + `cdffr.q`) across ALU / PMTRDT / MAC-MUL / DIV / FALU, plus the reservation-station buffers and the per-unit result fifos (`multi_fifo.mem`). | DMR |
| `storage` | Vector register file `vreg` (NUM_VRF × VLEN). | ECC |

Plus two **diagnostic** entries, kept runnable by name but out of the matrix
(`"analysis": False` keeps them out of `FI_MODULE=all`; use `every` or name
them directly):

| entry | why it is measured but not an analysis sub-module |
|---|---|
| `rob_data` | ROB **data** plane (`res_mem` + `u_uop_info_fifo.mem`). Answers no FT-scheme question: an execution error reaching it is already caught by DMR's write-back comparison, and ECC here would protect this one register stage only. Short residency (a few cycles) vs. the architecturally-visible VRF. |
| `fifo_ptr` | Every `multi_fifo` wptr/rptr/entry counter. Bookkeeping, not control- or data-path state; no per-sub-module FT scheme in this project covers it. |

### Why a cell is in the partition

A cell earns its place by **representing real circuit error, or by proving an
FT mechanism works — not by being a flip-flop**. Registers are a *proxy* for a
much larger population of errors, and the proxy is only as good as what it
stands for. Two consequences:

* **The AVF denominator is the sub-module**, because the decision this flow
  feeds is "which sub-module gets which FT scheme". Bit counts are therefore
  **not comparable across sub-modules**: a control flop that can send the ROB
  state machine off the rails and a buffer cell that represents only itself do
  not belong in one bit-count-weighted ranking. (Stage 2 published such a
  ranking; Stage 3 retracted it.)
* **Fan-in cone, with two boundaries.** Logic *inside* a sub-module is
  represented by the flops it feeds. Logic in an *upstream* sub-module is not —
  attributing it here would double-count it against its real owner. So ROB
  control bits model **their own** upset (one flipped bit derails the state
  machine), not "all the logic behind them".

**Inject the state, not the net that reads it.** Two targets look right and
are not: `vreg` is driven by the VRF's `edff` cells, and `entry_valid` is the
combinational `fifo_data` output of `u_uop_valid_fifo`. A deposit onto either
is recomputed away before it can propagate — the deposit probe measured
`entry_valid` at **landed 0/4, survived 0/9**. Both modules therefore inject
the storage behind the net.

**ROB is split by semantics, not by block.** `compute_ctrl` and `rob_data`
live in the same `u_rob` instance but get opposite FT treatment (control →
TMR, data plane → deliberately unprotected). Merging them would produce one
number describing both, useful for neither — which is also why demoting
`rob_data` to a diagnostic does not fold its bits into `compute_ctrl`.

**Full-module sampling.** Each experiment treats the concatenation of all
target widths as one flat fault space and picks a global bit uniformly. We do
*not* inject only the last pipeline stage: that would under-count a module's
vulnerable area (it drops intermediate-stage width and the masking
information of multi-stage logic) for no saving in setup or simulation count.

**Partition rule, INV-3 (important).** The injection space is partitioned **by
sub-module**, not assembled by enumerating registers that looked interesting.
Every sequential cell belongs to exactly one sub-module, and the source list
must be reconciled against the RTL's actual sequential-cell list — that
reconciliation is what turned up `trap_ready`, a single flop driving the
global flush that had been in *no* fault space for two stages.

The `multi_fifo` read/write pointers (`wptr`/`rptr`/`entry_count`) are `cdffr`
instances, but they are fifo-internal bookkeeping, not control- or data-path
state. They are **excluded** from every analysis sub-module and measured on
their own as `fifo_ptr`. The collector enforces this by taking only `mem` from
inside a `multi_fifo` subtree, and by walking only execution-unit subtrees
(which contain no fifo) for `q`. The 10 pointer bits of `u_uop_info_fifo` are
**explicitly exempt** from hardening as well: reaching them means modifying the
`multi_fifo` IP, i.e. hardening a generic FIFO rather than this NPU.

**INV-6: the framework injects flip-flops only.** Register-only TMR (the
Stage 3 scheme) corrects both `seu` and `set`, because an injection lands on
exactly one of three copies and the majority vote wins. So any "AVF → 0 after
hardening" result is valid **strictly at FF-level fault scope**. A SET on the
shared D-side combinational logic hits all three copies alike and is *not*
covered — and this framework has no means to expose that gap. Cite INV-6
alongside any post-hardening AVF number.

---

## 2. The three fault models

Selected with `FI_FAULT_TYPE`. Each models a distinct physical threat the FT
schemes defend against.

| `FI_FAULT_TYPE` | Physical threat | Behaviour |
|---|---|---|
| `seu` | Radiation-induced single-event upset on a storage cell | Flip one bit once; leave it flipped until the design naturally overwrites the cell. The canonical SEU model for FFs / SRAM. |
| `set` | Combinational single-event transient (glitch) | Wait for the cell's **next write**, then corrupt the value it just captured — the net effect of a glitch in the cone feeding that FF. |
| `stuck` | Permanent stuck-at (broken cell) | From the inject cycle to run end, force the bit to `FI_STUCK_VAL` (0 or 1) every clock, overriding design writes. |

### Why FF/SRAM granularity (not combinational nets)

Combinational logic holds no state — a value forced onto a comb net is
recomputed and overwritten on the next delta cycle, so it does not persist.
A combinational fault only does harm if it is latched by a flip-flop; the
`set` model captures exactly that net effect by depositing on the downstream
FF. Every targeted cell is a sequential element, which is also the standard
granularity for RTL SEU studies (enumerable, locatable, repeatable).

**`set` must be aligned to a write.** An earlier version flipped at a random
cycle and flipped back one cycle later. The deposit probe measured a
write-enable duty cycle **under 1.5%**, so that model landed on an idle cell
almost every time and was erased with no effect — it modelled a transient *on*
the flop, not a SET upstream of it. The current model waits for a write
(exactly via `e` on `edff`/`cdffr`, otherwise by observing the stored value
change) and corrupts what was captured. If the cell is never written again
before the run ends, **nothing is injected** and the run is reported as
`fault_fired=False` rather than counted as MASKED: a fault that was never
expressed is not evidence of tolerance.

All observation and deposition happens at the **falling** edge. A value read
at the rising edge is the pre-edge value (the update has not settled), so a
rising-edge comparison never sees a write, and a rising-edge deposit races the
design's own write.

---

## 3. The outcome taxonomy (three layers, six buckets, zero thresholds)

```
hung?            -> DUE-hang      (io_halted never asserted; pipeline deadlock)
io_fault?        -> DUE-crash     (scalar-core exception path; Core.scala io.fault)
status != 0?     -> DUE-detected  (app inference_status nonzero; software self-check)
halt & status 0:
  output == golden (bit-exact)            -> MASKED
  output != golden, top-1 (argmax) same   -> SDC-benign   (HW silently wrong, NN-tolerated)
  output != golden, top-1 changed         -> SDC-critical  (silent + decision flip)
```

| Layer | Buckets | What it tells the FT designer |
|---|---|---|
| **MASKED** | `MASKED` | Fault never reached the output. No protection needed. |
| **SDC** | `SDC-benign`, `SDC-critical` | Silent data corruption — the **only motivation** for ECC / DMR / TMR. `critical` flipped the decision; `benign` quantifies how much real HW corruption the NN happened to tolerate. |
| **DUE** | `DUE-hang`, `DUE-crash`, `DUE-detected` | Detected-but-unrecoverable — something signalled or stalled. Needs recovery (watchdog / trap handler / rollback). Lower priority than silent SDC. |

All three DUE buckets are **verified real coralnpu paths**, not assumptions:
`crash` from `Core.scala:74 io.fault := score.io.fault` (scalar-core
exception), `detected` from `run_<model>.cc` setting `inference_status`
nonzero on failure, `hang` from a control-state upset deadlocking the
pipeline so `io_halted` never asserts.

### Zero thresholds

`MASKED` requires a **bit-exact** output and SDC is split purely by `argmax` —
there are no `masked_tolerance` / `acc_degraded_threshold` knobs. This relies
on a deterministic golden run; `run_campaign` runs golden twice up front and
asserts the two outputs are bit-identical before trusting any outcome.

---

## 4. Environment-variable reference

| Variable | Default | Notes |
|---|---|---|
| `FI_MODULE` | `all` | `decode_path` \| `compute_ctrl` \| `execute` \| `storage` \| `rob_data` \| `fifo_ptr` \| `all` (the four analysis sub-modules) \| `every` (adds the two diagnostics) |
| `FI_FAULT_TYPE` | `all` | `seu` \| `set` \| `stuck` \| `all` |
| `FI_STUCK_VAL` | `0` | Polarity for the `stuck` model (0 or 1). Ignored otherwise. |
| `FI_N` | `50` | Runs per group. Total runs = `FI_N × |modules| × |fault_types|`. |
| `FI_SEED` | `0xC0DE` | RNG seed (deterministic re-runs). |
| `FI_DUMP_HIERARCHY` | unset | If set, dump the cocotb-visible hierarchy (depth 4) before running. Use when bringing up registry paths. |
| `FI_PROBE_N` / `FI_PROBE_STRIDE` | `200` / `1000` | Deposit-probe sample count and the cycles between samples (see §6). |
| `FI_PROBE_STRICT` | unset | If set, a failed deposit fails the probe test instead of only logging an error. |

The experiment matrix is `4 sub-modules × 3 fault types = 12 groups`; each
group is `FI_N` single-fault runs. The two diagnostics (`rob_data`,
`fifo_ptr`) add 3 groups each when named.

---

## 5. Outputs

### `fi_results.csv` — one row per injected run

| Column | Meaning |
|---|---|
| `model` | Model identifier passed to `run_campaign(model_name=...)`. |
| `module` / `ft_scheme` | The module under test and its target FT scheme. |
| `fault_type` / `stuck_val` | `seu`/`set`/`stuck`; polarity if stuck. |
| `run_id` | Monotonic across the whole matrix. |
| `target_path` | Hierarchical path of the exact signal hit (the injection site). |
| `local_bit` / `global_bit` / `fault_space_bits` | Bit within the signal, bit within the module's flat space, and the total module space. |
| `bit_live` | Did this bit toggle at all during the golden run? (see conditional AVF below) |
| `inject_cycle` / `halt_cycle` | Cycle of the flip; cycle the run ended. |
| `fault_fired` | Was the fault actually expressed? Only `set` can come back `False`. |
| `halted` / `faulted` / `hung` / `status` | Raw run state. |
| `output_bitexact` / `argmax_match` | Output comparison vs golden. |
| `outcome` | One of the six buckets. |

### `fi_summary.csv` — one row per group (15 rows for a full matrix)

Per group: the three-layer shares (`MASKED_pct` / `SDC_pct` / `DUE_pct`) and
the six-bucket shares (`MASKED_b_pct`, `SDC-benign_pct`, `SDC-critical_pct`,
`DUE-hang_pct`, `DUE-crash_pct`, `DUE-detected_pct`). The same per-group
counts are also logged.

Two re-normalised views sit next to them, because the raw shares have two
known dilutions:

* **Conditional AVF** (`live_bits`, `live_frac_pct`, `n_runs_live`,
  `*_pct_live`). A workload exercises only part of the silicon — measured at
  35% of `execute` and 28% of `storage` for `dnn_small_int8`. Injecting a bit
  that never changes is guaranteed MASKED and says nothing about the design,
  so the same layers are also reported over live bits only. The live mask is
  collected for free during golden run #1 (sampled every 200 cycles).
* **Fired-only** (`n_runs_not_fired`, `*_pct_fired`) — excludes `set` runs
  where the cell was never written again, per §2.

CSV filenames carry the group tag (`fi_results_<module>_<fault_type>.csv`) so
the per-group bazel targets can run in parallel without clobbering.

---

## 6. Positive control (`fi_deposit_probe`)

`MASKED` is indistinguishable from "the injection never happened", so the
framework carries its own **positive control**: a separate cocotb test that
deposits into a bit, reads it back, and reports whether the write landed and
whether it survived. It never writes the vulnerability CSVs, and the campaign
targets never run it — a broken instrument must not be able to contaminate
data.

It samples every module (`FI_PROBE_N` samples, `FI_PROBE_STRIDE` cycles apart)
and prints a table stratified by **target class × deposit phase**, with
`survived` split by the cell's write enable `e`:

| verdict | reading |
|---|---|
| `landed` < 100% | the deposit does not reach the cell — the vlt exposure or the handle is broken; fix the injection point |
| `landed` 100%, `survived\|e=1` ≈ 0, `survived\|e=0` ≈ 100% | **physically correct** — the design overwrote the cell. Not a bug |
| `landed` 100%, `survived\|e=0` ≈ 0 | the tool is refreshing the cell as if it were combinational — the instrument is broken |

`e`/`c` of `edff`/`cdffr` are exposed read-side for exactly this verdict (and
for the `set` model's write detection); they are never injection targets.
The `pose`/`nege` rows deposit at the rising / falling edge respectively, so
the same table also says whether a phase change would fix a low survival rate.

The determinism gate at the top of `run_campaign` (golden run twice, outputs
asserted bit-identical) is the matching **negative control**.

---

## 7. How to read the results

The headline per-group number is the **SDC rate**. Build the comparison table:

| Sub-module | seu SDC% | seu DUE% | set … | stuck … | Reading |
|---|---|---|---|---|---|
| decode_path | | | | | high DUE → recovery / TMR |
| compute_ctrl | | | | | high DUE-hang → TMR + watchdog |
| execute | | | | | high SDC → DMR |
| storage | | | | | high SDC → ECC |

Decision rule: **high-SDC sub-modules want ECC/DMR/TMR (kill silent errors);
high-DUE sub-modules want recovery hooks (lower priority — at least they were
detected).**

**Read down the column, not across it.** Each rate is an average over its own
sub-module's bits, so it answers "does *this* sub-module need protection". It
does **not** rank sub-modules against each other — multiplying a rate by a bit
count to get a "weighted vulnerability" mixes populations that represent
different amounts of silicon per bit. Stage 2 did exactly that and produced a
ranking that put a short-lived buffer above the ROB state machine; Stage 3
retracted it. If you need a cross-sub-module priority, argue it from the
failure mechanism and the FT scheme's cost, not from `AVF × bits`.

Two honesty caveats that must accompany any conclusion:

1. **Absolute rates are workload-specific** (measured only on the configured
   model). State conclusions as *module-to-module comparisons under the same
   workload and seed* ("control-path SDC is 5× the data-path"), not as
   absolute rates.
2. **NN masking effect.** A low data-path SDC rate is partly the model
   tolerating real hardware corruption, not hardware robustness. The
   `SDC-benign` bucket quantifies exactly that tolerated corruption — the
   higher it is, the more the hardware was actually wrong.

---

## 8. Layout

```
tests/cocotb/fault_injection/        <- this package (framework only)
  fi_utils.py      module registry, hierarchy collector, bit-flip
                   primitives, outcome classifier
  fi_campaign.py   run_campaign(): golden + determinism gate, 5x3 driver,
                   liveness watcher, per-run CSV + per-group summary
                   run_probe():    deposit positive control (section 6)
  BUILD            py_library exports: fi_utils, fi_campaign

tests/cocotb/tflite/arm_ml_zoo/<model>/
  cocotb_<model>_fi.py   thin shell: load ELF + reference IO, call run_campaign
  BUILD                  cocotb_test_suite target (tags=["manual"])

rules/default.vlt.tpl    public_flat_rw exposures: ROB regs, edff.q,
                         cdffr.q, multi_fifo.mem, and read-side edff.e /
                         cdffr.e / cdffr.c
```

### Adding the FI targets (already in `rules/default.vlt.tpl`)

```
public_flat_rw -module "rvv_backend_rob"     -var "uop_done"   (and trap_flag, res_mem)
public_flat_rw -module "edff"                -var "q"
public_flat_rw -module "cdffr"               -var "q"
public_flat_rw -module "multi_fifo"          -var "mem"
public_flat_rw -module "edff"                -var "e"          (read-side only)
public_flat_rw -module "cdffr"               -var "e"          (read-side only)
public_flat_rw -module "cdffr"               -var "c"          (read-side only)
```

`entry_valid` and `vreg` are deliberately **not** exposed: injecting them does
nothing (section 1). Exposure itself is free — removing the `e`/`c` lines and
rebuilding was measured to leave every module's fault space bit-identical, so
`public_flat_rw` is pure observation and does not perturb Verilator inlining.

One `edff`/`cdffr`/`multi_fifo` directive exposes every instance of that
module; the registry in `fi_utils.MODULES` routes each instance to its owning
RVV module by hierarchical path. After editing the vlt, rebuild the Verilator
model.

### Adding a model

Drop a thin shell next to the model and a `cocotb_test_suite` with deps on
`//tests/cocotb/fault_injection:fi_utils` and `:fi_campaign`. The shell only
loads the ELF + reference IO and calls
`await fi_campaign.run_campaign(dut, fixture, elf, x, y, model_name="<model>")`.
All knobs are env-driven, so the shell never changes across taxonomy edits.
See `tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8/` for a working example.

---

## 9. Quick start

One bazel target per group, so the matrix parallelizes across cores. All are
`tags=["manual"]`.

```bash
P=//tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:cocotb_dnn_small_int8_fi

# One group (storage SEU smoke, 10 runs).
bazel test ${P}_fi_storage_seu --test_env=FI_N=10

# The whole matrix, 30 runs per group, 18 targets in parallel.
bazel test ${P}_fi_{decode_path,compute_ctrl,rob_data,execute,storage,fifo_ptr}_{seu,set,stuck} \
    --test_env=FI_N=30 --test_timeout=10800

# Positive control (fast: no full inference, ~15 s).
bazel test ${P}_fi_deposit_probe

# Env-driven whole-matrix run in a single target (serial; ad-hoc use only).
bazel test ${P}_fi_run_all --test_env=FI_MODULE=execute --test_env=FI_FAULT_TYPE=stuck \
    --test_env=FI_STUCK_VAL=1

# Bring up registry paths: dump the cocotb hierarchy.
bazel test ${P}_fi_run_all --test_env=FI_DUMP_HIERARCHY=1 --test_env=FI_N=0

# Locate outputs after a run.
find bazel-testlogs -name 'fi_results*.csv' -o -name 'fi_summary*.csv' -o -name 'fi_probe.csv'
```

## 10. Scope / non-goals (current phase)

- **Single model.** Wired to `dnn_small_int8` to bound runtime. RVV bare-op
  workloads need a different (exact-vector) classifier with no benign/critical
  split — deferred.
- **execute not yet split into control vs data path.** The `edff`/`cdffr`
  primitive type does *not* equal data/control semantics (an `edff` can hold a
  ROB-entry pointer; a `cdffr` can hold a divider operand), so a clean split
  needs per-signal semantic tagging — deferred to a second pass.
- **Verilator only.** VCS exposes the same paths via `+access+rw` but is
  untested here.
- **Propagation control not implemented.** The probe proves a deposit lands
  and survives; it does not prove that a surviving flip can reach the output.
  The `stuck` groups' non-zero SDC is the current stand-in evidence.
- **No scalar-core / LSU / AXI targets.** Everything is inside the RVV
  backend; a fault in the scalar core or the memory path is out of scope.

