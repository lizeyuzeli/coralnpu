# RVV Fault-Injection Framework (Phase 1)

A cocotb-based fault-injection (FI) harness for the coralnpu RVV backend.
The framework runs a real TFLite-Micro inference (currently `dnn_small_int8`)
on the `RvvCoreMiniHighmemAxi` toplevel, deposits SEU/SET bit-flips into
selected RVV internal state via Verilator's VPI, and classifies each run as
**MASKED / SDC / DUE / HANG**.

It is designed to be the evaluation engine for the upcoming fault-tolerance
work on the RVV core (VRF ECC / ROB protection / pipeline parity, etc.).

---

## 1. Files

| Path | Role |
|---|---|
| `fi_utils.py` | Reusable primitives: target registry, hierarchy walker, bit-flip implementations (persistent SEU / transient SET), outcome classifier. |
| `cocotb_dnn_small_int8_fi.py` | Top-level cocotb test. Orchestrates golden + N injected inferences, writes `fi_results.csv`. |
| `BUILD` | Bazel `cocotb_test_suite` target. Currently uses the existing `rvv_core_mini_highmem_axi_model` Verilator build. |
| `../../../rules/default.vlt.tpl` | Verilator config that exposes the targeted internal signals as `public_flat_rw` so they can be deposited via VPI. |
| `../tflite/dnn_small_int8/BUILD` | Reused upstream: provides the test ELF + reference IO `.npy` files. |

---

## 2. Architecture

```
   ┌───────────────────────────────────────────────────┐
   │  cocotb_dnn_small_int8_fi.py                      │
   │  ─────────────────────────                        │
   │  1. Load ELF + golden IO from runfiles            │
   │  2. Resolve target handle via fi_utils registry   │
   │  3. Run golden inference → record halt cycle      │
   │  4. For run_id in 1..FI_N:                        │
   │       reset → execute_from(elf) → schedule flips  │
   │       wait halt / fault / timeout                 │
   │       compare output → classify outcome           │
   │  5. Write fi_results.csv                          │
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
bazel test //tests/cocotb/fault_injection:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi \
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
bazel test //tests/cocotb/fault_injection:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi \
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
bazel test //tests/cocotb/fault_injection:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi \
  --test_env=FI_CAMPAIGN=C \
  --test_env=FI_N=50 \
  --test_env=FI_TARGET=vrf_storage
```

---

## 4. Fault models

Selected with `FI_FAULT_MODEL={persistent,transient}` (default = the
target's catalog value, which is `persistent` for every current target).

| Model | Behaviour | Use for |
|---|---|---|
| `persistent` (SEU) | XOR the target bit once; leave it. The bit stays flipped until the design naturally overwrites it. | Flip-flops, SRAM cells, register file storage. **The canonical model for storage SEUs.** |
| `transient`  (SET) | XOR the bit, wait 1 clock, XOR back. | Combinational glitches / SET on a wire. Use *only* when modeling combinational FI; for FFs this drastically under-estimates damage. |

The earlier "transient" prototype produced uniformly MASKED results on
`vrf_storage` because the flip was restored before the cell was read.
Switching the default to `persistent` aligns with the SEU model used in
the academic literature (CARRV, DSN, DATE FI campaigns).

---

## 5. Target registry

Defined in `fi_utils.TARGETS`. Each entry has:

| Field | Meaning |
|---|---|
| `path` | Tuple of attribute names from the cocotb `dut` handle down to the module containing the target. Integer entries are array indices for SV `generate` blocks (Verilator's `INST_MAC[0]` style). |
| `signal` | Leaf signal name *inside* the module. Most entries are `q` (the output of an `edff` instance). |
| `row_bits` | Logical row width used only for CSV reporting (e.g. 128 = VLEN per VRF). |
| `fault_model` | Default fault model for this target. Overridden by `FI_FAULT_MODEL` env. |

### Current targets (10)

| Name | Width | Type | Description |
|---|---|---|---|
| `vrf_storage` | 4096 | **data** | RVV vector register file storage, 32 × VLEN(=128) bits |
| `rob_uop_done` | 8 | **control** | ROB per-entry completion bit. Highly sensitive. |
| `rob_trap_flag` | 8 | **control** | ROB per-entry trap flag |
| `rob_entry_valid` | 8 | **control** | ROB per-entry valid bit |
| `mac0_addsrc_d1` | 128 | **data** | MAC lane 0: VLEN-wide accumulator-add source D1 register |
| `mac0_rob_entry_d1` | 3 | **control** | MAC lane 0: ROB-entry pointer for writeback |
| `mac1_addsrc_d1` | 128 | **data** | MAC lane 1: accumulator-add source D1 |
| `mac1_rob_entry_d1` | 3 | **control** | MAC lane 1: ROB-entry pointer |
| `alu0_uop_p1` | 505 | **data+control** | ALU CMP unit: P1 pipeline payload `PIPE_DATA_t` (opcode + vs1 + vs2 + vd + rob_entry + ...) |
| `div_res_info` | 47 | **data+control** | DIV unit: result-info struct register (w_data + rob_entry + meta) |

`row_bits` is informational only for the CSV; it does **not** restrict
which bits get hit — every fault picks a global bit uniformly across the
full width.

### Adding a new target

For any pipeline FF inside the RVV backend that is implemented with `edff`
or `cdffr`, only **one** line in `fi_utils.TARGETS` is needed:

```python
"alu0_p1_result": {
    "path": _RVV_BACKEND_PREFIX + (
        "u_alu", "u_alu_cmp_unit", "u_alu_p1", "u_result_dly"),
    "signal": "q",
    "row_bits": 8,
    "fault_model": "persistent",
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
| `FI_CAMPAIGN` | `A` | Campaign A / B / C |
| `FI_TARGET` | `vrf_storage` | Any key in `fi_utils.TARGETS` |
| `FI_N` | A=50, B=1, C=50 | Number of inferences |
| `FI_FAULTS_PER_RUN` | 50 | Only used in Campaign B; A and C are forced to 1 |
| `FI_MIN_GAP` | 100 | Min cycle gap between consecutive faults (Campaign B) |
| `FI_FAULT_MODEL` | (target default) | `persistent` or `transient` |
| `FI_SEED` | `0xC0DE` | RNG seed (deterministic re-runs) |
| `FI_DUMP_HIERARCHY` | unset | If set, dump cocotb-visible hierarchy at depth 4 before running. Useful when adding new targets. |

---

## 7. CSV schema (`fi_results.csv`)

Written to `$TEST_UNDECLARED_OUTPUTS_DIR/fi_results.csv` (Bazel) or `./fi_results.csv` (standalone). One row per (run, fault).

| Column | Meaning |
|---|---|
| `run_id` | 0 = golden reference, 1..N = injected runs |
| `fault_id` | Position of this fault within the run (0..n_faults-1) |
| `tag` | `golden` or `inject` |
| `campaign` | `A` / `B` / `C` |
| `target` | Target name from registry |
| `fault_model` | `persistent` / `transient` |
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

## 9. Limitations & non-goals (current phase)

- **Single workload.** Only `dnn_small_int8` is wired in. Adding
  `ds_cnn_small_int8` / `kws_micronet_small_int8` is a copy of the test
  module with different runfile paths and a new `cocotb_test_suite`
  target.
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

## 10. Phase-2 roadmap (suggested)

1. **More targets.** Decode / dispatch / retire FFs (need additional
   `public_flat_rw` declarations because they use `always_ff`, not
   `edff`). Frontend `RvvFrontEnd` for completeness.
2. **Workload sweep.** Parameterize the test module on
   `(elf, input_npy, expected_npy)`; instantiate one `cocotb_test_suite`
   per (model, target).
3. **Driver + aggregator.** A shell or python driver that iterates
   `(model × target × campaign)`, collects per-run CSVs, produces a
   single aggregated table + heatmap.
4. **Fault-tolerance evaluation hook.** Once VRF ECC / ROB parity are
   implemented, re-run the *exact same* seed sweep and diff the SDC/DUE
   columns. Same framework, no new code.
5. **Coverage-aware sampling.** Skip bits in obviously-unused state
   (e.g. `vreg` rows that the model never writes); only useful when
   pushing N very high.

---

## 11. Quick-start cheatsheet

```bash
# 1) Default: 50 single-SEU runs on VRF, Campaign A baseline.
bazel test //tests/cocotb/fault_injection:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi

# 2) Stress: 50 SEUs in one inference, hit ROB done bits.
bazel test //tests/cocotb/fault_injection:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi \
  --test_env=FI_CAMPAIGN=B --test_env=FI_FAULTS_PER_RUN=50 \
  --test_env=FI_TARGET=rob_uop_done

# 3) ALU pipeline payload, only when the backend is actually active.
bazel test //tests/cocotb/fault_injection:cocotb_dnn_small_int8_fi_core_mini_rvv_dnn_small_int8_fi \
  --test_env=FI_CAMPAIGN=C --test_env=FI_N=20 \
  --test_env=FI_TARGET=alu0_uop_p1

# 4) When adding a new target, first dump the cocotb-visible hierarchy:
bazel test ... --test_env=FI_DUMP_HIERARCHY=1 --test_env=FI_N=0 ...

# 5) Locate the CSV after a run:
find bazel-out -name fi_results.csv -path '*fault_injection*'
```
