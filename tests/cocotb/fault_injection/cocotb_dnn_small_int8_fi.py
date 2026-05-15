# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Phase-1 RTL fault-injection campaign on the DNN-Small INT8 model.

Plan (kept intentionally tiny so we can iterate):
- Run one fault-free "golden" inference to measure halt latency and capture
  the reference output.
- Run 5 randomized single-bit transient flips of the RVV vector register
  file (`rvv_backend_vrf_reg.vreg`). Each flip XORs one bit at a random
  cycle in [1000, golden_halt_cycle], holds for 1 cycle, then XORs back.
- Classify each run as MASKED / SDC / DUE / HANG and append to a CSV at
  $TEST_UNDECLARED_OUTPUTS_DIR/fi_results.csv (Bazel-friendly).

The first time we wire this up against a new simulator build, set the
env var FI_DUMP_HIERARCHY=1 to dump the discovered VPI tree -- this is how
we'll confirm the actual exposed path to `vreg` before relying on it.
"""

import os
import random
import csv
import cocotb
import numpy as np

from bazel_tools.tools.python.runfiles import runfiles
from cocotb.triggers import ClockCycles, First, Timer

from coralnpu_test_utils.sim_test_fixture import Fixture

import fi_utils


_RUNFILES_PREFIX = "coralnpu_hw/tests/cocotb/tflite/dnn_small_int8/"
_ELF = "run_dnn_small_int8_binary.elf"
_INPUT_NPY = "test_data/input_0.npy"
_EXPECTED_NPY = "test_data/expected_output_0.npy"

# Phase-1 campaign defaults. All overridable via env vars so we can sweep
# without recompiling.
#   FI_CAMPAIGN: A | B | C
#     A: single-SEU baseline. Each inference receives 1 SEU. FI_N inferences.
#     B: cumulative-SEU. Each inference receives FI_FAULTS_PER_RUN SEUs
#        spaced >= FI_MIN_GAP cycles apart. FI_N inferences (default 1).
#     C: active-window SEU. Same as A but only fires when the activity
#        gate (RVV ROB has at least one valid entry) is high.
#   FI_TARGET: see fi_utils.TARGETS.
#   FI_N: number of inferences (i.e. CSV rows tagged 'inject').
#   FI_FAULTS_PER_RUN (B only): faults per inference.
#   FI_MIN_GAP (B only): minimum cycle gap between consecutive faults.
#   FI_FAULT_MODEL: persistent | transient. Default = target's catalog
#     value (storage cells default to persistent).
#   FI_SEED: RNG seed.
_DEFAULT_CAMPAIGN = "A"
_DEFAULT_TARGET = "vrf_storage"
_DEFAULT_N = {"A": 50, "B": 1, "C": 50}
_DEFAULT_FAULTS_PER_RUN = 50
_DEFAULT_MIN_GAP = 100

_INJECT_CYCLE_MIN = 1000
# Conservative simulation budget per run. The golden run sets the upper
# bound for random injection cycle; this constant only caps total wait.
_TIMEOUT_CYCLES = 50_000_000
# Hang detection: how many cycles past golden_halt before we declare HANG.
_HANG_MARGIN_MULT = 2.0

_DEFAULT_SEED = 0xC0DE


def _outputs_dir():
    d = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if d:
        os.makedirs(d, exist_ok=True)
        return d
    # Fall back to CWD so the test still produces a CSV when run outside
    # bazel test harness.
    return os.getcwd()


async def _wait_for_outcome(dut, timeout_cycles):
    """Tick the clock until io_halted, io_fault, or timeout.

    Returns (cycle_count, halted_bool, fault_bool, hung_bool).
    Does not assert; the caller decides how to react.
    """
    cycles = 0
    while cycles < timeout_cycles:
        if int(dut.io_halted.value) == 1:
            return cycles, True, False, False
        if int(dut.io_fault.value) == 1:
            return cycles, False, True, False
        await ClockCycles(dut.io_aclk, 1)
        cycles += 1
    return cycles, False, False, True


async def _run_once(dut, fixture, elf_path, input_data, expected_output,
                   *, inject_cb=None, timeout_cycles=_TIMEOUT_CYCLES):
    """Load ELF, stage input, kick off, optionally inject mid-run, observe.

    `inject_cb` is an awaitable coroutine factory taking (dut, clock) that
    will be spawned after `execute_from`. It is responsible for waiting the
    target cycle count and performing the flip.

    Returns a dict with run metrics.
    """
    await fixture.load_elf_and_lookup_symbols(
        elf_path,
        ["inference_status", "inference_status_message",
         "inference_input", "inference_output"],
    )
    await fixture.write("inference_input", input_data)
    await fixture.write(
        "inference_output",
        np.zeros(expected_output.size, dtype=np.int8))

    # Kick off execution. We do not use fixture.run_to_halt because it
    # asserts on timeout; we want to classify timeout as HANG instead.
    await fixture.core_mini_axi.execute_from(fixture.entry_point)

    inject_task = None
    if inject_cb is not None:
        inject_task = cocotb.start_soon(inject_cb(dut, dut.io_aclk))

    cycles, halted, fault, hung = await _wait_for_outcome(
        dut, timeout_cycles)

    if inject_task is not None and not inject_task.done():
        inject_task.kill()

    status = None
    max_abs_diff = None
    argmax_match = None
    actual_output = None
    message = ""
    if halted:
        try:
            status_word = (await fixture.read_word("inference_status")
                           ).view(np.int32)[0]
            status = int(status_word)
            message = bytes(
                await fixture.read("inference_status_message", 31)
            ).split(b"\x00", 1)[0].decode(errors="replace")
            actual_output = (
                await fixture.read("inference_output",
                                   expected_output.size)
            ).view(np.int8)
            diff = np.abs(actual_output.astype(np.int32) -
                          expected_output.astype(np.int32))
            max_abs_diff = int(diff.max()) if diff.size else 0
            argmax_match = (int(np.argmax(actual_output))
                            == int(np.argmax(expected_output)))
        except Exception as e:  # noqa: BLE001
            cocotb.log.warning("readback failed after halt: %s", e)

    return {
        "cycles": cycles,
        "halted": halted,
        "fault": fault,
        "hung": hung,
        "status": status,
        "message": message,
        "max_abs_diff": max_abs_diff,
        "argmax_match": argmax_match,
        "actual_output": (actual_output.tolist()
                          if actual_output is not None else None),
    }


async def _do_flip(clock, signal, bit_index, fault_model):
    """Dispatch to the appropriate flip primitive based on `fault_model`."""
    if fault_model == "persistent":
        await fi_utils.persistent_bit_flip(clock, signal, bit_index)
    elif fault_model == "transient":
        await fi_utils.transient_bit_flip(
            clock, signal, bit_index, hold_cycles=1)
    else:
        raise ValueError(f"unknown fault_model '{fault_model}'")


def _make_schedule_cb(target_signal, schedule, fault_model):
    """Build a coroutine that injects every (cycle, bit) in `schedule`.

    `schedule` must be a list of (inject_cycle, bit_index) sorted by
    inject_cycle (ascending). Cycles are absolute (counted from when the
    callback starts running, i.e. just after `execute_from`).
    """
    async def _cb(dut, clock):
        prev_cycle = 0
        for cyc, bit in schedule:
            delta = cyc - prev_cycle
            if delta > 0:
                await ClockCycles(clock, delta)
            await _do_flip(clock, target_signal, bit, fault_model)
            prev_cycle = cyc
    return _cb


def _make_gated_cb(target_signal, gate_signal, schedule, fault_model,
                   max_wait_cycles=200_000):
    """Like `_make_schedule_cb`, but each fault is deferred until the
    activity gate signal is non-zero.

    The schedule cycles act as the *earliest* time we are willing to fire;
    the actual fire time is the first cycle >= scheduled_cycle at which
    `int(gate_signal.value) != 0`. If the gate never opens within
    `max_wait_cycles` cycles past the schedule, the fault is dropped and a
    warning logged.
    """
    async def _cb(dut, clock):
        elapsed = 0
        for cyc, bit in schedule:
            delta = cyc - elapsed
            if delta > 0:
                await ClockCycles(clock, delta)
                elapsed = cyc
            # Wait for gate to open.
            waited = 0
            while int(gate_signal.value) == 0 and waited < max_wait_cycles:
                await ClockCycles(clock, 1)
                waited += 1
                elapsed += 1
            if int(gate_signal.value) == 0:
                cocotb.log.warning(
                    "fi: gate never opened for fault @ cycle>=%d, dropping",
                    cyc)
                continue
            await _do_flip(clock, target_signal, bit, fault_model)
            elapsed += 1  # _do_flip consumed one rising edge
    return _cb


def _resolve_target_handle(dut, target_name):
    """Resolve the cocotb handle for the named FI target.

    Tries the catalog path first; if that does not resolve (e.g. Verilator
    inlined a wrapper module away), falls back to a DFS search for the leaf
    signal name and returns the first match.
    """
    tgt = fi_utils.get_target(target_name)
    handle = fi_utils.resolve_handle(dut, tgt["path"], tgt["signal"])
    if handle is not None:
        return handle, tgt
    cocotb.log.warning(
        "fi: catalog path dut.%s.%s not visible, searching...",
        ".".join(tgt["path"]), tgt["signal"])
    found = fi_utils.search_for_signal(dut, tgt["signal"], max_depth=14)
    if found is None:
        raise RuntimeError(
            f"fi: could not locate signal '{tgt['signal']}' anywhere under "
            f"dut for target '{target_name}'. Re-run with "
            f"FI_DUMP_HIERARCHY=1 and inspect the log.")
    cocotb.log.info("fi: located %s at dut.%s", tgt["signal"],
                    ".".join(found))
    node = dut
    for step in found[:-1]:
        node = getattr(node, step)
    return getattr(node, found[-1]), tgt


def _gen_schedule(rng, n_faults, target_width, lo_cycle, hi_cycle,
                  min_gap):
    """Pick `n_faults` (cycle, bit) tuples with cycles strictly increasing
    and at least `min_gap` cycles apart. Returns a list sorted by cycle.
    """
    if hi_cycle - lo_cycle < (n_faults - 1) * min_gap + 1:
        # Not enough room; fall back to evenly spaced cycles.
        if n_faults <= 1:
            cycles = [rng.randint(lo_cycle, max(lo_cycle, hi_cycle - 1))]
        else:
            step = max(1, (hi_cycle - lo_cycle) // n_faults)
            cycles = [lo_cycle + i * step for i in range(n_faults)]
    else:
        cycles = sorted(rng.sample(
            range(lo_cycle, hi_cycle), n_faults))
        # Enforce min_gap by greedy adjustment.
        for i in range(1, len(cycles)):
            if cycles[i] - cycles[i - 1] < min_gap:
                cycles[i] = cycles[i - 1] + min_gap
        # Clamp to hi_cycle - 1 if we overflowed.
        for i in range(len(cycles)):
            if cycles[i] >= hi_cycle:
                cycles[i] = hi_cycle - 1
    bits = [rng.randrange(0, target_width) for _ in range(n_faults)]
    return list(zip(cycles, bits))


@cocotb.test()
async def core_mini_rvv_dnn_small_int8_fi(dut):
    seed = int(os.environ.get("FI_SEED", _DEFAULT_SEED))
    campaign = os.environ.get("FI_CAMPAIGN", _DEFAULT_CAMPAIGN).upper()
    if campaign not in ("A", "B", "C"):
        raise ValueError(f"FI_CAMPAIGN must be A|B|C, got '{campaign}'")
    target_name = os.environ.get("FI_TARGET", _DEFAULT_TARGET)
    num_injections = int(os.environ.get(
        "FI_N", _DEFAULT_N.get(campaign, 1)))
    faults_per_run = int(os.environ.get(
        "FI_FAULTS_PER_RUN", _DEFAULT_FAULTS_PER_RUN))
    min_gap = int(os.environ.get("FI_MIN_GAP", _DEFAULT_MIN_GAP))
    if campaign != "B":
        faults_per_run = 1  # A and C are single-fault per inference
    rng = random.Random(seed)
    cocotb.log.info(
        "fi: seed=%d campaign=%s target=%s N=%d faults/run=%d min_gap=%d",
        seed, campaign, target_name, num_injections, faults_per_run,
        min_gap)

    fixture = await Fixture.Create(dut, highmem=True)
    r = runfiles.Create()
    elf_path = r.Rlocation(_RUNFILES_PREFIX + _ELF)
    input_path = r.Rlocation(_RUNFILES_PREFIX + _INPUT_NPY)
    expected_path = r.Rlocation(_RUNFILES_PREFIX + _EXPECTED_NPY)

    input_data = np.load(input_path).astype(np.int8).flatten()
    expected_output = np.load(expected_path).astype(np.int8).flatten()

    # Optional one-shot hierarchy dump to discover VPI paths.
    if os.environ.get("FI_DUMP_HIERARCHY"):
        cocotb.log.info("fi: dumping cocotb-visible hierarchy (depth=4)")
        fi_utils.dump_hierarchy(dut, max_depth=4)

    target_handle, target_meta = _resolve_target_handle(dut, target_name)
    try:
        target_width = len(target_handle)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"fi: target handle has no length: {e}") from e
    row_bits = max(1, int(target_meta.get("row_bits", 1)))
    num_rows = max(1, target_width // row_bits)
    fault_model = os.environ.get(
        "FI_FAULT_MODEL", target_meta.get("fault_model", "persistent"))
    cocotb.log.info(
        "fi: target '%s' width=%d bits, row_bits=%d, rows=%d, model=%s",
        target_name, target_width, row_bits, num_rows, fault_model)

    # Resolve activity-gate handle for Campaign C.
    gate_handle = None
    if campaign == "C":
        gate_handle = fi_utils.resolve_handle(
            dut, fi_utils.ACTIVITY_GATE["path"],
            fi_utils.ACTIVITY_GATE["signal"])
        if gate_handle is None:
            raise RuntimeError(
                "fi: campaign C requires the activity gate signal "
                f"({fi_utils.ACTIVITY_GATE['signal']}) to be visible.")

    csv_path = os.path.join(_outputs_dir(), "fi_results.csv")
    cocotb.log.info("fi: results CSV -> %s", csv_path)
    csv_fields = [
        "run_id", "fault_id", "tag", "campaign", "target",
        "fault_model", "n_faults", "row_idx", "bit_in_row",
        "global_bit_index", "inject_cycle", "halt_cycle", "halted",
        "fault", "hung", "status", "max_abs_diff", "argmax_match",
        "outcome",
    ]
    rows = []

    # 1) Golden run.
    cocotb.log.info("fi: ===== golden run =====")
    golden = await _run_once(
        dut, fixture, elf_path, input_data, expected_output,
        inject_cb=None, timeout_cycles=_TIMEOUT_CYCLES)
    assert golden["halted"] and golden["status"] == 0, (
        f"golden run failed: halted={golden['halted']} "
        f"fault={golden['fault']} hung={golden['hung']} "
        f"status={golden['status']} msg='{golden['message']}'")
    assert golden["max_abs_diff"] is not None and golden["max_abs_diff"] <= 1
    assert golden["argmax_match"], "golden argmax mismatch"
    golden_halt = golden["cycles"]
    cocotb.log.info("fi: golden halt_cycle=%d", golden_halt)
    rows.append({
        "run_id": 0, "fault_id": 0, "tag": "golden",
        "campaign": campaign, "target": target_name,
        "fault_model": fault_model, "n_faults": 0,
        "row_idx": -1, "bit_in_row": -1, "global_bit_index": -1,
        "inject_cycle": -1, "halt_cycle": golden_halt,
        "halted": True, "fault": False, "hung": False,
        "status": 0, "max_abs_diff": golden["max_abs_diff"],
        "argmax_match": True, "outcome": "MASKED",
    })

    hang_timeout = max(
        _INJECT_CYCLE_MIN + 10_000,
        int(golden_halt * _HANG_MARGIN_MULT))
    cocotb.log.info("fi: per-run timeout=%d cycles", hang_timeout)

    upper_cycle = max(_INJECT_CYCLE_MIN + faults_per_run * min_gap,
                      golden_halt)

    for run_id in range(1, num_injections + 1):
        schedule = _gen_schedule(
            rng, faults_per_run, target_width,
            _INJECT_CYCLE_MIN, upper_cycle, min_gap)
        cocotb.log.info(
            "fi: ===== run %d/%d (%s,%s,%s): %d faults scheduled =====",
            run_id, num_injections, campaign, target_name, fault_model,
            len(schedule))
        for fid, (cyc, bit) in enumerate(schedule):
            cocotb.log.info(
                "fi:   fault %d -> cycle=%d bit=%d (row=%d bit_in_row=%d)",
                fid, cyc, bit, bit // row_bits, bit % row_bits)

        if campaign == "C":
            cb = _make_gated_cb(
                target_handle, gate_handle, schedule, fault_model)
        else:
            cb = _make_schedule_cb(target_handle, schedule, fault_model)

        res = await _run_once(
            dut, fixture, elf_path, input_data, expected_output,
            inject_cb=cb, timeout_cycles=hang_timeout)

        outcome = fi_utils.classify_outcome(
            fault_flag=res["fault"],
            status=(res["status"] if res["status"] is not None else -1),
            max_abs_diff=res["max_abs_diff"],
            argmax_match=(res["argmax_match"] if res["argmax_match"]
                          is not None else False),
            hung=res["hung"])
        cocotb.log.info(
            "fi: run %d -> outcome=%s halted=%s fault=%s hung=%s "
            "status=%s diff=%s argmax_match=%s halt_cycle=%d",
            run_id, outcome, res["halted"], res["fault"], res["hung"],
            res["status"], res["max_abs_diff"], res["argmax_match"],
            res["cycles"])

        # One CSV row per (run, fault). The run-level outcome is repeated
        # so analysis is straightforward (groupby run_id for run-level
        # stats, or filter by fault_id==0 for one-row-per-run).
        for fid, (cyc, bit) in enumerate(schedule):
            rows.append({
                "run_id": run_id, "fault_id": fid, "tag": "inject",
                "campaign": campaign, "target": target_name,
                "fault_model": fault_model,
                "n_faults": len(schedule),
                "row_idx": bit // row_bits,
                "bit_in_row": bit % row_bits,
                "global_bit_index": bit,
                "inject_cycle": cyc,
                "halt_cycle": res["cycles"],
                "halted": res["halted"], "fault": res["fault"],
                "hung": res["hung"],
                "status": (res["status"]
                           if res["status"] is not None else ""),
                "max_abs_diff": (res["max_abs_diff"]
                                 if res["max_abs_diff"] is not None else ""),
                "argmax_match": (res["argmax_match"]
                                 if res["argmax_match"] is not None
                                 else ""),
                "outcome": outcome,
            })

    # Write CSV.
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    cocotb.log.info("fi: wrote %d rows -> %s", len(rows), csv_path)

    # Per-run outcome summary (for B/C, multiple fault rows share one run
    # outcome; we de-dup by run_id here).
    run_outcomes = {}
    for row in rows:
        if row["tag"] == "inject":
            run_outcomes[row["run_id"]] = row["outcome"]
    counts = {"MASKED": 0, "ACC_DEGRADED": 0, "SDC": 0,
              "DETECTED": 0, "CRASH": 0, "HANG": 0}
    for o in run_outcomes.values():
        counts[o] = counts.get(o, 0) + 1
    cocotb.log.info(
        "fi: campaign=%s target=%s model=%s -- per-run summary over %d "
        "runs (faults/run=%d): %s",
        campaign, target_name, fault_model, num_injections,
        faults_per_run, counts)
