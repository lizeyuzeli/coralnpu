# RVV Core Module-Level Fault-Injection Framework

A cocotb + Verilator-VPI fault-injection (FI) harness for **module-level
vulnerability analysis** of the coralnpu RVV core. It partitions the RVV
backend into **four modules**, injects single-bit faults into the full state
of one module at a time, and classifies each run into a **three-layer /
six-bucket** outcome taxonomy. The four modules map one-to-one onto the
planned fault-tolerance (FT) schemes, so the vulnerability profile reads
directly as "which module needs which protection".

This harness analyses the **unprotected design** (`FAULT_TOLERANT_ON` OFF).
Once the FT schemes land, re-running the same seed sweep with FT ON and
diffing the SDC/DUE columns quantifies their effect — same framework, no new
code.

---

## 1. The four modules

| `FI_MODULE` | Fault space (every targeted FF/SRAM in the module) | FT scheme |
|---|---|---|
| `decode_path` | Front-end queues: command / legal-command / uop queue buffers (`multi_fifo.mem`). Three serial stages, each a different payload form, all injected. | TMR? |
| `compute_ctrl` | ROB control state: `entry_valid`, `uop_done`, `trap_flag`. ROB read/write pointers are excluded (fifo-style bookkeeping). | TMR |
| `execute` | All execution-unit pipeline FFs (`edff.q` + `cdffr.q`) across ALU / PMTRDT / MAC-MUL / DIV / FALU, plus the reservation-station buffers (`multi_fifo.mem`). | DMR |
| `storage` | Vector register file `vreg` (NUM_VRF × VLEN). | ECC |

**Full-module sampling.** Each experiment treats the concatenation of all
target widths as one flat fault space and picks a global bit uniformly. We do
*not* inject only the last pipeline stage: that would under-count a module's
vulnerable area (it drops intermediate-stage width and the masking
information of multi-stage logic) for no saving in setup or simulation count.

**Isolation rule (important).** The `multi_fifo` read/write pointers
(`wptr`/`rptr`/`entry_count`) are `cdffr` instances, but they are fifo-internal
bookkeeping, not control- or data-path state. They are **excluded** from every
module. The collector enforces this by taking only `mem` from inside a
`multi_fifo` subtree, and by walking only execution-unit subtrees (which
contain no fifo) for `q`.

---

## 2. The three fault models

Selected with `FI_FAULT_TYPE`. Each models a distinct physical threat the FT
schemes defend against.

| `FI_FAULT_TYPE` | Physical threat | Behaviour |
|---|---|---|
| `seu` | Radiation-induced single-event upset on a storage cell | Flip one bit once; leave it flipped until the design naturally overwrites the cell. The canonical SEU model for FFs / SRAM. |
| `set` | Combinational single-event transient (glitch) | Flip the bit, hold one clock, flip back — the net effect of a glitch mis-sampled by one FF. |
| `stuck` | Permanent stuck-at (broken cell) | From the inject cycle to run end, force the bit to `FI_STUCK_VAL` (0 or 1) every clock, overriding design writes. |

### Why FF/SRAM granularity (not combinational nets)

Combinational logic holds no state — a value forced onto a comb net is
recomputed and overwritten on the next delta cycle, so it does not persist.
A combinational fault only does harm if it is latched by a flip-flop; the
`set` model captures exactly that net effect by depositing on the downstream
FF. Every targeted cell is a sequential element, which is also the standard
granularity for RTL SEU studies (enumerable, locatable, repeatable).

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
| `FI_MODULE` | `all` | `decode_path` \| `compute_ctrl` \| `execute` \| `storage` \| `all` |
| `FI_FAULT_TYPE` | `all` | `seu` \| `set` \| `stuck` \| `all` |
| `FI_STUCK_VAL` | `0` | Polarity for the `stuck` model (0 or 1). Ignored otherwise. |
| `FI_N` | `50` | Runs per group. Total runs = `FI_N × |modules| × |fault_types|`. |
| `FI_SEED` | `0xC0DE` | RNG seed (deterministic re-runs). |
| `FI_DUMP_HIERARCHY` | unset | If set, dump the cocotb-visible hierarchy (depth 4) before running. Use when bringing up registry paths. |

The experiment matrix is `4 modules × 3 fault types = 12 groups`; each group
is `FI_N` single-fault runs.

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
| `inject_cycle` / `halt_cycle` | Cycle of the flip; cycle the run ended. |
| `halted` / `faulted` / `hung` / `status` | Raw run state. |
| `output_bitexact` / `argmax_match` | Output comparison vs golden. |
| `outcome` | One of the six buckets. |

### `fi_summary.csv` — one row per group (12 rows for a full matrix)

Per group: the three-layer shares (`MASKED_pct` / `SDC_pct` / `DUE_pct`) and
the six-bucket shares (`MASKED_b_pct`, `SDC-benign_pct`, `SDC-critical_pct`,
`DUE-hang_pct`, `DUE-crash_pct`, `DUE-detected_pct`). The same per-group
counts are also logged.

---

## 6. How to read the results

The headline per-group number is the **SDC rate**. Build the comparison table:

| Module | seu SDC% | seu DUE% | set … | stuck … | Reading |
|---|---|---|---|---|---|
| decode_path | | | | | high DUE → recovery / TMR |
| compute_ctrl | | | | | high DUE-hang → TMR + watchdog |
| execute | | | | | high SDC → DMR |
| storage | | | | | high SDC → ECC |

Decision rule: **high-SDC modules want ECC/DMR/TMR (kill silent errors);
high-DUE modules want recovery hooks (lower priority — at least they were
detected).**

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

## 7. Layout

```
tests/cocotb/fault_injection/        <- this package (framework only)
  fi_utils.py      module registry, hierarchy collector, bit-flip
                   primitives, outcome classifier
  fi_campaign.py   run_campaign(): golden + determinism gate, 4x3 driver,
                   per-run CSV + per-group summary
  BUILD            py_library exports: fi_utils, fi_campaign

tests/cocotb/tflite/arm_ml_zoo/<model>/
  cocotb_<model>_fi.py   thin shell: load ELF + reference IO, call run_campaign
  BUILD                  cocotb_test_suite target (tags=["manual"])

rules/default.vlt.tpl    public_flat_rw exposures: ROB control regs,
                         edff.q, cdffr.q, multi_fifo.mem
```

### Adding the FI targets (already in `rules/default.vlt.tpl`)

```
public_flat_rw -module "rvv_backend_rob"     -var "uop_done"   (and trap_flag, entry_valid)
public_flat_rw -module "edff"                -var "q"
public_flat_rw -module "cdffr"               -var "q"
public_flat_rw -module "multi_fifo"          -var "mem"
```

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

## 8. Quick start

```bash
DNN=//tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi

# Full matrix: 4 modules x 3 fault types, 50 runs each.
bazel test $DNN

# One module, one fault type (storage SEU smoke, 10 runs).
bazel test $DNN --test_env=FI_MODULE=storage --test_env=FI_FAULT_TYPE=seu --test_env=FI_N=10

# Permanent stuck-at-1 on the execution units.
bazel test $DNN --test_env=FI_MODULE=execute --test_env=FI_FAULT_TYPE=stuck --test_env=FI_STUCK_VAL=1

# Bring up registry paths: dump the cocotb hierarchy.
bazel test $DNN --test_env=FI_DUMP_HIERARCHY=1 --test_env=FI_N=0

# Locate outputs after a run.
find bazel-out -name 'fi_results.csv' -o -name 'fi_summary.csv'
```

## 9. Scope / non-goals (current phase)

- **Single model.** Wired to `dnn_small_int8` to bound runtime. RVV bare-op
  workloads need a different (exact-vector) classifier with no benign/critical
  split — deferred.
- **execute not yet split into control vs data path.** The `edff`/`cdffr`
  primitive type does *not* equal data/control semantics (an `edff` can hold a
  ROB-entry pointer; a `cdffr` can hold a divider operand), so a clean split
  needs per-signal semantic tagging — deferred to a second pass.
- **Verilator only.** VCS exposes the same paths via `+access+rw` but is
  untested here.
- **Result fifo excluded from `execute`.** Verilator inlines the `gen_res_ff`
  generate block (no clean scope), and its buffer is a one-cycle downstream
  copy of the execution-unit results already covered by the `edff`/`cdffr`
  cells. Injecting it would be near-redundant, so it is not a target.

