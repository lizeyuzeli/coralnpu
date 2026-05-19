# RVV Fault-Injection Framework (Phase 1)

A cocotb-based fault-injection (FI) harness for the coralnpu RVV backend.
For each integrated model, the framework runs a real TFLite-Micro
inference on the `RvvCoreMiniHighmemAxi` toplevel, deposits SEU/SET
bit-flips into selected RVV internal state via Verilator's VPI, and
classifies each run as **MASKED / ACC_DEGRADED / SDC / DETECTED / CRASH /
HANG**.

It is designed to be the evaluation engine for the upcoming
fault-tolerance work on the RVV core (VRF ECC / ROB protection / pipeline
parity, etc.).

---

## 1. Layout

This package is **framework-only**. The per-model FI cocotb tests and
their `cocotb_test_suite` targets live next to each model so that adding
a new model does not touch this directory.

```
tests/cocotb/fault_injection/      <- this package (framework)
  fi_utils.py                       target registry, handle resolver,
                                    bit-flip primitives, outcome classifier
  fi_campaign.py                    run_campaign() driver: golden run,
                                    A/B/C scheduler, CSV writer, summary
  BUILD                             py_library: fi_utils, fi_campaign

tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8/
  cocotb_dnn_small_int8_fi.py       thin shell -> fi_campaign.run_campaign
  BUILD                             cocotb_test_suite: cocotb_dnn_small_int8_fi
```

| File | Role |
|---|---|
| `fi_utils.py` | Reusable primitives: target registry, hierarchy walker, bit-flip implementations (persistent SEU / transient SET), outcome classifier. |
| `fi_campaign.py` | Model-agnostic campaign driver. Exposes `run_campaign(dut, fixture, elf, x, y, *, model_name, ...)`. |
| `BUILD` | Bazel `py_library` exports for `fi_utils` and `fi_campaign`. |
| `../../../rules/default.vlt.tpl` | Verilator config that exposes the targeted internal signals as `public_flat_rw` so they can be deposited via VPI. |
| `../tflite/.../<model>/cocotb_<model>_fi.py` | Per-model thin shell (loads ELF + IO from runfiles, calls `run_campaign`). |
| `../tflite/.../<model>/BUILD` | Per-model `cocotb_test_suite` target named `cocotb_<model>_fi`. |

---

## 2. Architecture

```
   ┌───────────────────────────────────────────────────┐
   │  cocotb_<model>_fi.py   (per-model thin shell)    │
   │  ──────────────────────────────────────           │
   │  Load ELF + reference IO from runfiles, then:     │
   │     await fi_campaign.run_campaign(...)           │
   └─────────────────────┬─────────────────────────────┘
                         │ calls
   ┌─────────────────────▼─────────────────────────────┐
   │  fi_campaign.py                                   │
   │    1. Parse env (FI_CAMPAIGN/TARGET/N/...)        │
   │    2. Resolve target + (optional) gate handle    │
   │    3. Golden inference → record halt cycle        │
   │    4. For run_id in 1..FI_N:                      │
   │         reset → execute_from(elf) → schedule flips│
   │         wait halt / fault / timeout               │
   │         compare output → classify outcome         │
   │    5. Write fi_results.csv + per-run summary log  │
   └─────────────────────┬─────────────────────────────┘
                         │ uses
   ┌─────────────────────▼─────────────────────────────┐
   │  fi_utils.py                                      │
   │    TARGETS registry (10 entries)                  │
   │    ACTIVITY_GATE (for Campaign C)                 │
   │    resolve_handle(dut, path, signal)              │
   │    persistent_bit_flip / transient_bit_flip       │
   │    classify_outcome(...)                          │
   └─────────────────────┬─────────────────────────────┘
                         │ deposits into
   ┌─────────────────────▼─────────────────────────────┐
   │  Verilator model (public_flat_rw variables)       │
   │    rvv_backend_vrf_reg.vreg                       │
   │    rvv_backend_rob.{uop_done,trap_flag,entry_valid}│
   │    edff.q  (covers every RVV pipeline FF)         │
   └───────────────────────────────────────────────────┘
```

### Key design choice: `edff.q` global exposure

The RVV backend uses two reusable D-FF primitives — `edff` (enable D-FF) and
`cdffr` (clear-on-c D-FF) — for **all** sequential logic in ALU, MAC, MUL,
DIV, PMTRDT, and dispatch pipelines. Direct `always_ff` blocks only appear
in `rvv_backend_rob` and the front end.

By marking one variable globally public — `public_flat_rw -module "edff"
-var "q"` — every `edff` instance in the design becomes deposit-able via
VPI. Adding a new pipeline FF as an FI target therefore only requires a new
entry in `TARGETS`; no Verilog or vlt edits.

---

## 3. Three campaigns

Selected with `FI_CAMPAIGN={A,B,C}`. All campaigns share the same target
registry and fault primitives — only the *scheduler* differs.

### Campaign A — single-SEU baseline (default)

Each inference gets exactly **one** random bit-flip at a random cycle.
Repeat `FI_N` times (default 50). This is the canonical *vulnerability
profile* campaign: each row of `fi_results.csv` is one independent
data-point you can attribute to a specific (target, bit, cycle).

```bash
bazel test //tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi \
  --test_env=FI_CAMPAIGN=A \
  --test_env=FI_N=50 \
  --test_env=FI_TARGET=vrf_storage
```

### Campaign B — cumulative-SEU stress

A single inference receives `FI_FAULTS_PER_RUN` faults (default 50) spaced
≥`FI_MIN_GAP` cycles apart (default 100). Repeat `FI_N` times (default 1).
Cheap (~1 inference of cost), useful as a *go/no-go stress test* during
fault-tolerance design iteration. Cannot attribute outcomes to specific
faults.

```bash
bazel test //tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi \
  --test_env=FI_CAMPAIGN=B \
  --test_env=FI_FAULTS_PER_RUN=50 \
  --test_env=FI_TARGET=rob_uop_done
```

### Campaign C — active-window SEU

Same as A, but each injection is deferred until the activity gate
(`rvv_backend_rob.entry_valid != 0`, i.e. the RVV backend has at least one
in-flight uop) is open. This eliminates the "fault fell into idle cycles"
masking source and concentrates statistics on the time the unit is
actually working.

```bash
bazel test //tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi \
  --test_env=FI_CAMPAIGN=C \
  --test_env=FI_N=50 \
  --test_env=FI_TARGET=vrf_storage
```

---

## 4. Fault classes & models

Faults are organised as a **two-level hierarchy**: pick a `FI_FAULT_CLASS`
(physical category) then a concrete `FI_FAULT_MODEL` inside that class.

| Class | Models | Behaviour | Maps to research target |
|---|---|---|---|
| `soft` | `seu` *(default)* | XOR the bit once; leaves it flipped until the design naturally overwrites the cell. **The canonical SEU model for FFs and SRAM.** | Radiation-induced transient upsets. NN-aware selective protection. |
| `soft` | `set` | XOR the bit, hold for 1 clock, XOR back. | Combinational glitch / SET on a wire. Use only on un-latched logic; under-estimates FF damage. |
| `hard` | `stuck0` *(default)* | From the inject cycle until run end, force the bit to `0` every clock. **Overrides design writes** -- a real broken cell. | Permanent stuck-at; motivates the `vl`-based functional-degradation FT scheme. |
| `hard` | `stuck1` | Same as `stuck0` but force to `1`. | Same as above. |

Default class per target is in `fi_utils.TARGETS[<name>]["default_class"]`
(currently `soft` for every target). Override with
`FI_FAULT_CLASS=hard` to inject a permanent fault on the same target.

**Rejected combination**: `FI_FAULT_CLASS=hard` + `FI_CAMPAIGN=B`. Multiple
simultaneous independent permanent stuck-at faults are not a meaningful
physical model -- one die is one die. The framework asserts.

**Backwards-incompatible note**: the phase-1 prototype used
`FI_FAULT_MODEL=persistent|transient`. Both values were *soft*; the
rename to `seu` / `set` is cosmetic but explicit. Old test_envs need to
set `FI_FAULT_MODEL=seu` (was `persistent`) or `set` (was `transient`).

---

## 5. Target registry

Defined in `fi_utils.TARGETS`. Each entry has:

| Field | Meaning |
|---|---|
| `path` | Tuple of attribute names from the cocotb `dut` handle down to the module containing the target. Integer entries are array indices for SV `generate` blocks (Verilator's `INST_MAC[0]` style). |
| `signal` | Leaf signal name *inside* the module. Most entries are `q` (the output of an `edff` instance). |
| `row_bits` | Logical row width used only for CSV reporting (e.g. 128 = VLEN per VRF). |
| `class` | `data` / `control` / `mixed`. Drives the central paper plot and is the key for `FI_TARGET=data` / `FI_TARGET=control` sweeps. |
| `group` | Functional unit (`vrf` / `rob` / `mac` / `alu` / `div`). Key for unit-level sweeps. |
| `default_class` | `soft` or `hard`; the natural fault class for the target if `FI_FAULT_CLASS` is unset. Currently `soft` for all targets (storage cells / FFs). |

### Current targets (10)

| Name | Width | Class | Group | Description |
|---|---|---|---|---|
| `vrf_storage` | 4096 | data | vrf | RVV vector register file storage, 32 × VLEN(=128) bits |
| `rob_uop_done` | 8 | control | rob | ROB per-entry completion bit. Highly sensitive. |
| `rob_trap_flag` | 8 | control | rob | ROB per-entry trap flag |
| `rob_entry_valid` | 8 | control | rob | ROB per-entry valid bit |
| `mac0_addsrc_d1` | 128 | data | mac | MAC lane 0: VLEN-wide accumulator-add source D1 register |
| `mac0_rob_entry_d1` | 3 | control | mac | MAC lane 0: ROB-entry pointer for writeback |
| `mac1_addsrc_d1` | 128 | data | mac | MAC lane 1: accumulator-add source D1 |
| `mac1_rob_entry_d1` | 3 | control | mac | MAC lane 1: ROB-entry pointer |
| `alu0_uop_p1` | 505 | mixed | alu | ALU CMP unit: P1 pipeline payload `PIPE_DATA_t` (opcode + vs1 + vs2 + vd + rob_entry + ...) |
| `div_res_info` | 47 | mixed | div | DIV unit: result-info struct register (w_data + rob_entry + meta) |

`row_bits` is informational only for the CSV; it does **not** restrict
which bits get hit — every fault picks a global bit uniformly across the
full width.

> **Caveat for `mixed` targets.** `alu0_uop_p1` and `div_res_info` pack
> data and control bits into one struct register; uniform bit sampling
> hits both. When the data-vs-control story needs a clean split for
> these, add `data_bit_ranges` / `control_bit_ranges` lists to the
> registry entry and have the schedule generator pick within the
> requested class. Not implemented yet.

### Target groups

`FI_TARGET` accepts either a single target key or one of the following
group / class names; the framework expands a group into its members and
runs `FI_N` injection runs against each member. The golden run is
shared across the whole sweep.

| Group | Members |
|---|---|
| `vrf` | `vrf_storage` |
| `rob` | `rob_uop_done`, `rob_trap_flag`, `rob_entry_valid` |
| `mac` | `mac0_addsrc_d1`, `mac0_rob_entry_d1`, `mac1_addsrc_d1`, `mac1_rob_entry_d1` |
| `alu` | `alu0_uop_p1` |
| `div` | `div_res_info` |
| `data` | every target with `class=data` |
| `control` | every target with `class=control` |
| `mixed` | every target with `class=mixed` |
| `all` | all 10 targets |

### Adding a new target

For any pipeline FF inside the RVV backend that is implemented with `edff`
or `cdffr`, only **one** line in `fi_utils.TARGETS` is needed:

```python
"alu0_p1_result": {
    "path": _RVV_BACKEND_PREFIX + (
        "u_alu", "u_alu_cmp_unit", "u_alu_p1", "u_result_dly"),
    "signal": "q",
    "row_bits": 8,
    "class": "data",          # data | control | mixed
    "group": "alu",           # vrf | rob | mac | alu | div
    "default_class": "soft",  # soft | hard
    "description": "ALU lane 0: P1 result delay register",
},
```

For state that lives in an `always_ff` block (e.g. additional ROB
registers, dispatch FIFO state), a `public_flat_rw -module "X" -var "y"`
directive must also be added to `rules/default.vlt.tpl` and the
Verilator model rebuilt.

---

## 6. Environment-variable reference

| Variable | Default | Notes |
|---|---|---|
| `FI_CAMPAIGN` | `A` | Campaign `A` / `B` / `C` |
| `FI_TARGET` | `vrf_storage` | A target key, group key, class key, or `all`. See §5. |
| `FI_FAULT_CLASS` | target's `default_class` | `soft` or `hard` |
| `FI_FAULT_MODEL` | class default (`seu` / `stuck0`) | soft: `seu` \| `set`. hard: `stuck0` \| `stuck1`. |
| `FI_N` | A=50, B=1, C=50 | Number of injection runs **per target**. With a group spec, total runs = `FI_N × |group|`. |
| `FI_FAULTS_PER_RUN` | 50 | Only used in Campaign B; A and C are forced to 1. |
| `FI_MIN_GAP` | 100 | Min cycle gap between consecutive faults (Campaign B). |
| `FI_SEED` | `0xC0DE` | RNG seed (deterministic re-runs). |
| `FI_DUMP_HIERARCHY` | unset | If set, dump cocotb-visible hierarchy at depth 4 before running. Useful when adding new targets. |
| `FI_FALLBACK_DIR` | `/tmp` | Directory for the per-process mirror CSV (`fi_results_<model>_<pid>.csv`). Bazel does not always package `TEST_UNDECLARED_OUTPUTS_DIR` on test failure (e.g. when an SVA `$finish` aborts the run); this mirror keeps partial results recoverable. Add `--sandbox_writable_path=$FI_FALLBACK_DIR` if running under a strict sandbox. |

Rejected combinations:

- `FI_FAULT_CLASS=hard` + `FI_CAMPAIGN=B` (multi-simultaneous stuck-at
  is not modelled).

---

## 7. CSV schema (`fi_results.csv`)

Written to `$TEST_UNDECLARED_OUTPUTS_DIR/fi_results.csv` (Bazel) or `./fi_results.csv` (standalone). One row per (run, fault).

| Column | Meaning |
|---|---|
| `model` | Model identifier passed to `run_campaign(model_name=...)` (e.g. `dnn_small_int8`). Lets you concatenate CSVs from multiple models and groupby. |
| `run_id` | 0 = golden reference, 1..N = injected runs (monotonic across the whole sweep, including multi-target sweeps) |
| `fault_id` | Position of this fault within the run (0..n_faults-1) |
| `tag` | `golden` or `inject` |
| `campaign` | `A` / `B` / `C` |
| `target` | Target name from registry (or `(golden)` for the golden row) |
| `target_class` | `data` / `control` / `mixed` (or `(golden)`). The key column for the central paper plot. |
| `target_group` | `vrf` / `rob` / `mac` / `alu` / `div` (or `(golden)`). |
| `fault_class` | `soft` / `hard` (or `(none)` for the golden row) |
| `fault_model` | `seu` / `set` / `stuck0` / `stuck1` (or `(none)`) |
| `n_faults` | Total faults injected in this run |
| `row_idx` | `global_bit_index // row_bits` (for reporting) |
| `bit_in_row` | `global_bit_index % row_bits` |
| `global_bit_index` | Absolute bit index inside the flat target vector |
| `inject_cycle` | Cycle (counted from `execute_from`) at which the flip occurred |
| `halt_cycle` | Total cycles when the run terminated (halt / fault / timeout) |
| `halted` | `io_halted=1` reached (normal program finish) |
| `fault` | `io_fault=1` reached (exception path) |
| `hung` | Timed out (neither halted nor faulted) |
| `status` | `inference_status` word; 0 = OK |
| `max_abs_diff` | max |actual - expected| over the 12 int8 output classes |
| `argmax_match` | Top-1 class matches golden |
| `outcome` | One of the 6 buckets below (run-level; repeated for every fault row of the run) |

### Outcome taxonomy

Six fine-grained buckets, designed so each maps to a different
fault-tolerance design lever:

| Bucket | Meaning | What it tells the FT designer |
|---|---|---|
| `MASKED` | Output ≡ golden within int8 quantization noise | Don't bother protecting this state |
| `ACC_DEGRADED` | Top-1 class correct but logits shifted noticeably | May be fine for inference; bad for regression / multi-class |
| `SDC` | Halted, status OK, but top-1 class wrong | **Worst case** – silent. Needs ECC / redundant exec / output check |
| `DETECTED` | Halted but app status != 0 (software self-detected) | Software check already caught it; recovery hooks needed |
| `CRASH` | Hardware fault path (`io_fault=1`, e.g. illegal instr / access fault) | Needs trap handler / process restart |
| `HANG` | Run timed out without halt or fault | Needs watchdog / forward-progress monitor |

Classification rules (in fi_utils.classify_outcome):

```
if hung:                                                  → HANG
if fault_flag:                                            → CRASH
if status != 0:                                           → DETECTED
if not argmax_match:                                      → SDC
if max_abs_diff > acc_degraded_threshold (default 4):     → ACC_DEGRADED
if max_abs_diff > masked_tolerance      (default 1):      → ACC_DEGRADED
else:                                                     → MASKED
```

The 1-LSB tolerance handles legitimate int8 quantization noise observed
even in the golden run. Tune thresholds via the function's kwargs if your
workload's golden run is noisier (FP models, larger output ranges).

---

## 8. Results so far (phase-1 sanity data, dnn_small_int8)

All Campaign B, persistent SEU model. `dnn_small_int8` is a tiny
fully-connected model (~84KB ELF); golden inference is **291,472** cycles.

| Target | Faults/run | Outcome | Halt cycle |
|---|---|---|---|
| `vrf_storage` (4096b, data) | 50 | **MASKED** | 291,472 (= golden) |
| `mac0_addsrc_d1` (128b, data) | 50 | **MASKED** | 291,475 (+3) |
| `mac0_rob_entry_d1` (3b, ctl) | 20 | **HANG** | timed out at 582,944 |
| `rob_uop_done` (8b, ctl) | 10 | **HANG** | timed out at 582,944 |

Phase-1 conclusion: **data-path SEUs are masked at >98% rate; control-path
SEUs cause hangs/exceptions at <20 faults**. This matches RVV-FT
literature and motivates protecting control state (ROB pointers, valid
bits, pipeline rob_entry references) first.

---

## 9. Adding a new model

With the framework / per-model split, wiring a new model into the FI flow
is purely a model-package edit. No changes to this directory are needed.

1. Drop a thin shell next to the model, e.g.
   `tests/cocotb/tflite/arm_ml_zoo/<model>/cocotb_<model>_fi.py`. The
   shell stays unchanged across every taxonomy / campaign change because
   all knobs are env-driven:

   ```python
   import cocotb, numpy as np
   from bazel_tools.tools.python.runfiles import runfiles
   from coralnpu_test_utils.sim_test_fixture import Fixture
   import fi_campaign

   _PREFIX = "coralnpu_hw/tests/cocotb/tflite/arm_ml_zoo/<model>/"

   @cocotb.test()
   async def core_mini_rvv_<model>_fi(dut):
       fixture = await Fixture.Create(dut, highmem=True)
       r = runfiles.Create()
       elf = r.Rlocation(_PREFIX + "run_<model>_binary.elf")
       x = np.load(r.Rlocation(_PREFIX + "test_data/input_0.npy")
                  ).astype(np.int8).flatten()
       y = np.load(r.Rlocation(_PREFIX + "test_data/expected_output_0.npy")
                  ).astype(np.int8).flatten()
       await fi_campaign.run_campaign(
           dut, fixture, elf, x, y, model_name="<model>")
   ```

2. Add a `cocotb_test_suite` to the model's BUILD with deps on
   `//tests/cocotb/fault_injection:fi_utils` and
   `//tests/cocotb/fault_injection:fi_campaign`. See
   `tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8/BUILD` for a working
   example to copy.

3. Override accuracy thresholds via the kwargs of `run_campaign` if your
   model is noisier than the int8 KWS workloads (e.g. larger output
   range, FP outputs):

   ```python
   await fi_campaign.run_campaign(
       dut, fixture, elf, x, y, model_name="<model>",
       masked_tolerance=2, acc_degraded_threshold=8,
       golden_max_abs_diff_tolerance=2)
   ```

---

## 10. Limitations & non-goals (current phase)

- **Per-target test runs.** Sweeping all targets requires N invocations
  of `bazel test`; there's no aggregate driver yet.
- **No analysis tooling.** CSV analysis (heatmap / pivot by target ×
  outcome) is left for `pandas`/external scripts.
- **VCS path untested.** Targets are validated on Verilator only;
  VCS exposes the same hierarchical paths (`+access+rw`) but has not
  been smoked.
- **Reset interaction.** Each injection run does a full reset before
  `execute_from`. Persistent flips therefore do not survive across runs.
- **Activity-gate granularity.** Campaign C uses `entry_valid` (any
  in-flight uop) as the gate. Per-unit gates (only ALU-active, only
  MAC-active) would give sharper conditional vulnerability data.

---

## 11. Phase-2 roadmap (suggested)

1. **More targets.** Decode / dispatch / retire FFs (need additional
   `public_flat_rw` declarations because they use `always_ff`, not
   `edff`). Frontend `RvvFrontEnd` for completeness.
2. **Multi-model sweep.** Wire the remaining ML-zoo / ST-AI-zoo workloads
   through `run_campaign` (one thin shell each, see §9).
3. **Driver + aggregator.** A shell or python driver that iterates
   `(model × target × campaign)`, collects per-run CSVs, produces a
   single aggregated table + heatmap. The CSV `model` column is in place
   to make this trivial.
4. **Fault-tolerance evaluation hook.** Once VRF ECC / ROB parity are
   implemented, re-run the *exact same* seed sweep and diff the SDC/DUE
   columns. Same framework, no new code.
5. **Coverage-aware sampling.** Skip bits in obviously-unused state
   (e.g. `vreg` rows that the model never writes); only useful when
   pushing N very high.

---

## 12. Quick-start cheatsheet

Note: the FI test target now lives next to the model, not in this
package. The label format is
`//tests/cocotb/tflite/.../<model>:cocotb_<model>_fi_core_mini_rvv_<model>_fi`.

```bash
DNN=//tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi

# 1) Default: 50 SEU runs on VRF, Campaign A baseline.
bazel test $DNN

# 2) Sweep every data-class target with one bazel invocation.
bazel test $DNN --test_env=FI_TARGET=data --test_env=FI_N=100

# 3) Same sweep on the control side.
bazel test $DNN --test_env=FI_TARGET=control --test_env=FI_N=100

# 4) Hard fault: stuck-at-0 on MAC lane 1 (single permanent fault).
#    This is the baseline (no FT recovery) for the vl-degradation story.
bazel test $DNN \
  --test_env=FI_FAULT_CLASS=hard --test_env=FI_FAULT_MODEL=stuck0 \
  --test_env=FI_TARGET=mac1_addsrc_d1 --test_env=FI_N=20

# 5) Cumulative-SEU stress on ROB done bits.
bazel test $DNN \
  --test_env=FI_CAMPAIGN=B --test_env=FI_FAULTS_PER_RUN=50 \
  --test_env=FI_TARGET=rob_uop_done

# 6) ALU pipeline payload, only when the backend is actually active.
bazel test $DNN \
  --test_env=FI_CAMPAIGN=C --test_env=FI_N=20 \
  --test_env=FI_TARGET=alu0_uop_p1

# 7) When adding a new target, first dump the cocotb-visible hierarchy:
bazel test $DNN --test_env=FI_DUMP_HIERARCHY=1 --test_env=FI_N=0

# 8) Locate the CSV after a run:
find bazel-out -name fi_results.csv
```
