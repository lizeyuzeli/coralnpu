# Copyright 2026 Li Zeyu <lizeyuzeli000lzy@gmail.com>
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

"""Reusable RTL fault-injection campaign driver.

This module is the model-agnostic core of the FI flow. A per-model
cocotb test only has to:

    1. Build a `Fixture` (`highmem=True` is the common case).
    2. Resolve the ELF path and reference input/output `numpy` arrays
       from its own runfiles.
    3. ``await fi_campaign.run_campaign(dut, fixture, elf, x, y,
                                        model_name="<my_model>")``

Everything else (golden run, three campaign schedulers, target /group
sweep, soft / hard fault dispatch, classification, CSV output, summary
log) lives here so adding a new model does not require copying ~500
lines of harness code.

Configuration is via env vars:

    FI_FAULT_CLASS  : soft | hard
        soft : transient/SEU-style; design eventually overwrites the bit
        hard : permanent stuck-at; bit is forced every cycle from the
               injection point until the run ends.
        Default = the target's `default_class` (soft for every current
        target).
    FI_FAULT_MODEL  : (depends on class)
        soft -> seu (default) | set
        hard -> stuck0 (default) | stuck1
    FI_CAMPAIGN  : A | B | C
        A: single fault per inference. FI_N inferences.
        B: cumulative-SEU stress (soft only). FI_FAULTS_PER_RUN faults
           per inference, spaced >= FI_MIN_GAP cycles apart, FI_N runs.
        C: active-window single fault (gated on RVV ROB activity).
    FI_TARGET  : a target key (see fi_utils.TARGETS) OR a group/class
        key (see fi_utils.TARGET_GROUPS, e.g. 'data', 'control', 'mac',
        'all'). When a group is given, the framework runs FI_N injection
        runs against EACH target in the group; the golden run is shared.
        Default = 'vrf_storage'.
    FI_N               : number of injected inferences per target.
    FI_FAULTS_PER_RUN  : faults per inference (campaign B only).
    FI_MIN_GAP         : minimum cycle gap between consecutive faults (B).
    FI_SEED            : RNG seed (deterministic re-runs).
    FI_DUMP_HIERARCHY  : if set, dump cocotb-visible hierarchy at depth 4.

Rejected combinations:
    - FI_FAULT_CLASS=hard with FI_CAMPAIGN=B (multi-simultaneous stuck-at
      is not a physically meaningful model; one die is one die).
"""

import csv
import os
import random

import cocotb
import numpy as np
from cocotb.triggers import ClockCycles

import fi_utils

# Default campaign tuning. All overridable via env vars.
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
    # a Bazel test harness.
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
    will be spawned after `execute_from`. It is responsible for waiting
    the target cycle count and performing the flip(s).

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

    # The schedule callback may itself spawn long-lived holder tasks
    # (one per permanent stuck-at). We track every spawned task here so
    # we can kill the whole tree at end-of-run.
    spawned_holders = []
    inject_task = None
    if inject_cb is not None:
        inject_task = cocotb.start_soon(inject_cb(dut, dut.io_aclk,
                                                  spawned_holders))

    cycles, halted, fault, hung = await _wait_for_outcome(
        dut, timeout_cycles)

    # Stop the scheduler first, then any permanent-fault holder it spawned.
    if inject_task is not None and not inject_task.done():
        inject_task.kill()
    for t in spawned_holders:
        if not t.done():
            t.kill()

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


async def _do_soft_fault(clock, signal, bit_index, fault_model):
    """Soft-class injection: returns when the (one-shot) effect is done."""
    if fault_model == "seu":
        await fi_utils.persistent_bit_flip(clock, signal, bit_index)
    elif fault_model == "set":
        await fi_utils.transient_bit_flip(
            clock, signal, bit_index, hold_cycles=1)
    else:
        raise ValueError(
            f"unknown soft fault_model '{fault_model}' "
            f"(expected one of {fi_utils.FAULT_MODELS_BY_CLASS['soft']})")


def _spawn_hard_fault(clock, signal, bit_index, fault_model, holders):
    """Hard-class injection: spawn a permanent-fault holder coroutine.

    Returns immediately; the holder runs until killed by `_run_once`.
    """
    if fault_model == "stuck0":
        value = 0
    elif fault_model == "stuck1":
        value = 1
    else:
        raise ValueError(
            f"unknown hard fault_model '{fault_model}' "
            f"(expected one of {fi_utils.FAULT_MODELS_BY_CLASS['hard']})")
    t = cocotb.start_soon(
        fi_utils.permanent_stuck_at(clock, signal, bit_index, value))
    holders.append(t)


def _make_schedule_cb(target_signal, schedule, fault_class, fault_model):
    """Build a coroutine that injects every (cycle, bit) in `schedule`.

    `schedule` must be a list of (inject_cycle, bit_index) sorted by
    inject_cycle (ascending). Cycles are absolute (counted from when the
    callback starts running, i.e. just after `execute_from`).

    For soft faults the injection is awaited (one-shot effect). For hard
    faults a long-lived stuck-at holder is spawned at the inject cycle
    and added to `holders` so `_run_once` can kill it at run end.
    """
    async def _cb(dut, clock, holders):
        prev_cycle = 0
        for cyc, bit in schedule:
            delta = cyc - prev_cycle
            if delta > 0:
                await ClockCycles(clock, delta)
            if fault_class == "soft":
                await _do_soft_fault(clock, target_signal, bit, fault_model)
            else:  # 'hard'
                _spawn_hard_fault(
                    clock, target_signal, bit, fault_model, holders)
            prev_cycle = cyc
    return _cb


def _make_gated_cb(target_signal, gate_signal, schedule, fault_class,
                   fault_model, max_wait_cycles=200_000):
    """Like `_make_schedule_cb`, but each fault is deferred until the
    activity gate signal is non-zero.

    The schedule cycles act as the *earliest* time we are willing to fire;
    the actual fire time is the first cycle >= scheduled_cycle at which
    `int(gate_signal.value) != 0`. If the gate never opens within
    `max_wait_cycles` cycles past the schedule, the fault is dropped and
    a warning logged.
    """
    async def _cb(dut, clock, holders):
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
            if fault_class == "soft":
                await _do_soft_fault(clock, target_signal, bit, fault_model)
                elapsed += 1  # soft helper consumed one rising edge
            else:  # 'hard'
                _spawn_hard_fault(
                    clock, target_signal, bit, fault_model, holders)
    return _cb


def _resolve_target_handle(dut, target_name):
    """Resolve the cocotb handle for the named FI target.

    Tries the catalog path first; if that does not resolve (e.g. Verilator
    inlined a wrapper module away), falls back to a DFS search for the
    leaf signal name and returns the first match.
    """
    tgt = fi_utils.get_target(target_name)
    handle = fi_utils.resolve_handle(dut, tgt["path"], tgt["signal"])
    if handle is not None:
        return handle, tgt
    cocotb.log.warning(
        "fi: catalog path dut.%s.%s not visible, searching...",
        ".".join(str(s) for s in tgt["path"]), tgt["signal"])
    found = fi_utils.search_for_signal(dut, tgt["signal"], max_depth=14)
    if found is None:
        raise RuntimeError(
            f"fi: could not locate signal '{tgt['signal']}' anywhere "
            f"under dut for target '{target_name}'. Re-run with "
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


# CSV schema. `model` was added when the campaign driver was extracted so
# multi-model sweep CSVs can be concatenated and grouped cleanly. The
# `target_class` / `target_group` columns enable per-class and per-unit
# rollups (the central paper plot is groupby `target_class`,
# stacked-bar of outcome shares).
CSV_FIELDS = [
    "model", "run_id", "fault_id", "tag",
    "campaign", "target", "target_class", "target_group",
    "fault_class", "fault_model", "n_faults",
    "row_idx", "bit_in_row", "global_bit_index", "inject_cycle",
    "halt_cycle", "halted", "fault", "hung", "status",
    "max_abs_diff", "argmax_match", "outcome",
]


def _resolve_fault_class_and_model(target_default_class):
    """Resolve (fault_class, fault_model) from env, with sensible defaults
    keyed off the target's natural class.
    """
    fc = os.environ.get("FI_FAULT_CLASS", target_default_class)
    if fc not in fi_utils.FAULT_MODELS_BY_CLASS:
        raise ValueError(
            f"FI_FAULT_CLASS must be one of "
            f"{sorted(fi_utils.FAULT_MODELS_BY_CLASS)}, got '{fc}'")
    fm = os.environ.get("FI_FAULT_MODEL",
                        fi_utils.FAULT_CLASS_DEFAULT_MODEL[fc])
    if fm not in fi_utils.FAULT_MODELS_BY_CLASS[fc]:
        raise ValueError(
            f"FI_FAULT_MODEL '{fm}' not valid for class '{fc}'. "
            f"Valid: {fi_utils.FAULT_MODELS_BY_CLASS[fc]}")
    return fc, fm


async def run_campaign(dut, fixture, elf_path, input_data, expected_output,
                       *, model_name, masked_tolerance=1,
                       acc_degraded_threshold=4,
                       golden_max_abs_diff_tolerance=1,
                       require_golden_argmax_match=True,
                       recovery_cb=None):
    """Run the full FI campaign for a single model.

    Args:
        dut: cocotb top-level handle (`RvvCoreMiniHighmemAxi`).
        fixture: a constructed
            `coralnpu_test_utils.sim_test_fixture.Fixture`.
        elf_path: absolute path to the model's ELF binary.
        input_data: 1-D `np.int8` array, written to `inference_input`.
        expected_output: 1-D `np.int8` array, compared to `inference_output`.
        model_name: short identifier baked into every CSV row.
        masked_tolerance: max int8 |diff| still considered MASKED.
        acc_degraded_threshold: above this diff, classify as ACC_DEGRADED
            even if argmax matches.
        golden_max_abs_diff_tolerance: max |diff| permitted in the golden
            (no-injection) run before we abort.
        require_golden_argmax_match: if True, abort if golden argmax
            disagrees with the reference.
        recovery_cb: PLACEHOLDER. Reserved for the phase-2 hard-fault
            functional-degradation flow (FT scheme detects the permanent
            fault and reduces effective lane count / VL via a custom CSR
            on the live DUT). Will be called as
            ``await recovery_cb(dut, fixture, fault_info)`` after the
            fault is injected. Currently unused; pass None until the RTL
            recovery interface is defined.
    """
    # base=0 lets the user pass either '4242' or '0xC0DE' style.
    seed = int(str(os.environ.get("FI_SEED", _DEFAULT_SEED)), 0)
    campaign = os.environ.get("FI_CAMPAIGN", _DEFAULT_CAMPAIGN).upper()
    if campaign not in ("A", "B", "C"):
        raise ValueError(f"FI_CAMPAIGN must be A|B|C, got '{campaign}'")
    target_spec = os.environ.get("FI_TARGET", _DEFAULT_TARGET)
    target_names = fi_utils.expand_target_spec(target_spec)
    num_injections = int(os.environ.get(
        "FI_N", _DEFAULT_N.get(campaign, 1)))
    faults_per_run = int(os.environ.get(
        "FI_FAULTS_PER_RUN", _DEFAULT_FAULTS_PER_RUN))
    min_gap = int(os.environ.get("FI_MIN_GAP", _DEFAULT_MIN_GAP))
    if campaign != "B":
        faults_per_run = 1  # A and C are single-fault per inference

    rng = random.Random(seed)
    cocotb.log.info(
        "fi: model=%s seed=%d campaign=%s target_spec=%s -> %d target(s) "
        "N=%d faults/run=%d min_gap=%d",
        model_name, seed, campaign, target_spec, len(target_names),
        num_injections, faults_per_run, min_gap)
    cocotb.log.info("fi: target list: %s", target_names)

    # Optional one-shot hierarchy dump to discover VPI paths.
    if os.environ.get("FI_DUMP_HIERARCHY"):
        cocotb.log.info("fi: dumping cocotb-visible hierarchy (depth=4)")
        fi_utils.dump_hierarchy(dut, max_depth=4)

    # Resolve handles + per-target fault class/model up front. This lets
    # us fail fast on bad env settings before doing any simulation.
    target_specs = []  # list of dicts, one per target
    for tn in target_names:
        handle, meta = _resolve_target_handle(dut, tn)
        try:
            width = len(handle)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"fi: target '{tn}' handle has no length: {e}") from e
        fault_class, fault_model = _resolve_fault_class_and_model(
            meta.get("default_class", "soft"))
        if fault_class == "hard" and campaign == "B":
            raise ValueError(
                f"fi: target '{tn}' campaign=B + fault_class=hard is "
                "not supported -- multiple simultaneous independent "
                "permanent stuck-at faults are not a meaningful model. "
                "Use campaign A or C, or set FI_FAULT_CLASS=soft.")
        if fault_class == "hard" and recovery_cb is None:
            cocotb.log.info(
                "fi: target '%s' running hard-fault baseline "
                "(no recovery_cb registered) -- this measures the "
                "raw permanent-fault impact on the unprotected DUT",
                tn)
        row_bits = max(1, int(meta.get("row_bits", 1)))
        target_specs.append({
            "name": tn,
            "handle": handle,
            "meta": meta,
            "width": width,
            "row_bits": row_bits,
            "num_rows": max(1, width // row_bits),
            "fault_class": fault_class,
            "fault_model": fault_model,
        })
        cocotb.log.info(
            "fi: target '%s' class=%s group=%s width=%d row_bits=%d "
            "rows=%d fault=(%s,%s)",
            tn, meta["class"], meta["group"], width, row_bits,
            max(1, width // row_bits), fault_class, fault_model)

    # Resolve activity-gate handle for Campaign C (shared across targets).
    gate_handle = None
    if campaign == "C":
        gate_handle = fi_utils.resolve_handle(
            dut, fi_utils.ACTIVITY_GATE["path"],
            fi_utils.ACTIVITY_GATE["signal"])
        if gate_handle is None:
            raise RuntimeError(
                "fi: campaign C requires the activity gate signal "
                f"({fi_utils.ACTIVITY_GATE['signal']}) to be visible.")

    # CSV writing is INCREMENTAL with per-row flush. If a downstream
    # SVA $finish (e.g. scalar core regfile assertion fired by a control-
    # path SEU) kills Verilator mid-campaign, every completed run up to
    # the crash is still on disk.
    #
    # We mirror to /tmp as well because Bazel does not always package
    # TEST_UNDECLARED_OUTPUTS_DIR on a failed test, and the sandbox is
    # destroyed before we can recover anything from it.
    csv_path = os.path.join(_outputs_dir(), "fi_results.csv")
    # Bazel may not package TEST_UNDECLARED_OUTPUTS_DIR into outputs.zip
    # when the test FAILS (rules_hdl/cocotb wrapper specific). To make
    # partial CSVs from SVA-killed control-path runs recoverable, mirror
    # to a path the user can promote out of the sandbox via
    #   `--sandbox_writable_path=$FI_FALLBACK_DIR`.
    fallback_dir = os.environ.get("FI_FALLBACK_DIR", "/tmp")
    try:
        os.makedirs(fallback_dir, exist_ok=True)
    except OSError:
        pass  # If unwritable we'll still see it explode on open() below.
    fallback_path = os.path.join(
        fallback_dir,
        f"fi_results_{model_name}_{os.getpid()}.csv")
    cocotb.log.info("fi: results CSV -> %s", csv_path)
    cocotb.log.info("fi: fallback CSV -> %s "
                    "(in case bazel drops sandbox outputs on FAIL)",
                    fallback_path)
    csv_file = open(csv_path, "w", newline="")
    fallback_file = open(fallback_path, "w", newline="")
    csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    fallback_writer = csv.DictWriter(fallback_file, fieldnames=CSV_FIELDS)
    csv_writer.writeheader()
    fallback_writer.writeheader()
    csv_file.flush()
    fallback_file.flush()
    row_count = 0

    def _emit(row):
        nonlocal row_count
        csv_writer.writerow(row)
        fallback_writer.writerow(row)
        csv_file.flush()
        fallback_file.flush()
        row_count += 1

    # 1) Golden run. Shared across the whole sweep.
    cocotb.log.info("fi: ===== golden run (model=%s) =====", model_name)
    golden = await _run_once(
        dut, fixture, elf_path, input_data, expected_output,
        inject_cb=None, timeout_cycles=_TIMEOUT_CYCLES)
    assert golden["halted"] and golden["status"] == 0, (
        f"golden run failed: halted={golden['halted']} "
        f"fault={golden['fault']} hung={golden['hung']} "
        f"status={golden['status']} msg='{golden['message']}'")
    assert (golden["max_abs_diff"] is not None
            and golden["max_abs_diff"] <= golden_max_abs_diff_tolerance), (
        f"golden max_abs_diff={golden['max_abs_diff']} exceeds tolerance "
        f"{golden_max_abs_diff_tolerance}")
    if require_golden_argmax_match:
        assert golden["argmax_match"], "golden argmax mismatch"
    golden_halt = golden["cycles"]
    cocotb.log.info("fi: golden halt_cycle=%d", golden_halt)
    _emit({
        "model": model_name,
        "run_id": 0, "fault_id": 0, "tag": "golden",
        "campaign": campaign, "target": "(golden)",
        "target_class": "(golden)", "target_group": "(golden)",
        "fault_class": "(none)", "fault_model": "(none)", "n_faults": 0,
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

    # 2) For each target, FI_N injection runs.
    summary = {}  # (target -> {outcome -> count})
    global_run_id = 0
    for tspec in target_specs:
        tn = tspec["name"]
        meta = tspec["meta"]
        target_handle = tspec["handle"]
        target_width = tspec["width"]
        row_bits = tspec["row_bits"]
        fault_class = tspec["fault_class"]
        fault_model = tspec["fault_model"]
        target_class = meta["class"]
        target_group = meta["group"]

        cocotb.log.info(
            "fi: ===== target '%s' (class=%s, group=%s, fault=%s/%s): "
            "%d run(s) =====",
            tn, target_class, target_group, fault_class, fault_model,
            num_injections)
        target_outcomes = {"MASKED": 0, "ACC_DEGRADED": 0, "SDC": 0,
                           "DETECTED": 0, "CRASH": 0, "HANG": 0}

        for run_idx in range(1, num_injections + 1):
            global_run_id += 1
            schedule = _gen_schedule(
                rng, faults_per_run, target_width,
                _INJECT_CYCLE_MIN, upper_cycle, min_gap)
            cocotb.log.info(
                "fi: --- run %d/%d on '%s' (%s/%s, %s/%s): %d fault(s) ---",
                run_idx, num_injections, tn, campaign, target_class,
                fault_class, fault_model, len(schedule))
            for fid, (cyc, bit) in enumerate(schedule):
                cocotb.log.info(
                    "fi:     fault %d -> cycle=%d bit=%d "
                    "(row=%d bit_in_row=%d)",
                    fid, cyc, bit, bit // row_bits, bit % row_bits)

            if campaign == "C":
                cb = _make_gated_cb(
                    target_handle, gate_handle, schedule,
                    fault_class, fault_model)
            else:
                cb = _make_schedule_cb(
                    target_handle, schedule, fault_class, fault_model)

            # A control-path SEU can put the design into a state that
            # trips a downstream SVA `assert` inside Chisel-generated
            # RTL, which Verilator turns into `$finish`. That kills the
            # simulator beyond recovery: subsequent _run_once calls
            # would all fail. We log a CRASH_SIM row for the offending
            # schedule, close the CSV cleanly, then re-raise so cocotb
            # marks the test as FAIL (the CSV is already complete on
            # disk thanks to per-row flushing + the /tmp fallback).
            try:
                res = await _run_once(
                    dut, fixture, elf_path, input_data, expected_output,
                    inject_cb=cb, timeout_cycles=hang_timeout)
            except BaseException as e:  # noqa: BLE001
                cocotb.log.error(
                    "fi: simulator crashed during run %d/%d on '%s' "
                    "(likely RTL SVA $finish triggered by the injected "
                    "fault). Recording CRASH_SIM and aborting campaign.\n"
                    "    %s: %s",
                    run_idx, num_injections, tn, type(e).__name__, e)
                for fid, (cyc, bit) in enumerate(schedule):
                    _emit({
                        "model": model_name,
                        "run_id": global_run_id, "fault_id": fid,
                        "tag": "inject",
                        "campaign": campaign, "target": tn,
                        "target_class": target_class,
                        "target_group": target_group,
                        "fault_class": fault_class,
                        "fault_model": fault_model,
                        "n_faults": len(schedule),
                        "row_idx": bit // row_bits,
                        "bit_in_row": bit % row_bits,
                        "global_bit_index": bit,
                        "inject_cycle": cyc, "halt_cycle": -1,
                        "halted": "", "fault": "", "hung": "",
                        "status": "", "max_abs_diff": "",
                        "argmax_match": "", "outcome": "CRASH_SIM",
                    })
                target_outcomes["CRASH_SIM"] = (
                    target_outcomes.get("CRASH_SIM", 0) + 1)
                summary[tn] = target_outcomes
                csv_file.close()
                fallback_file.close()
                cocotb.log.error(
                    "fi: wrote %d rows before sim crash -> %s "
                    "(fallback: %s)",
                    row_count, csv_path, fallback_path)
                raise

            outcome = fi_utils.classify_outcome(
                fault_flag=res["fault"],
                status=(res["status"]
                        if res["status"] is not None else -1),
                max_abs_diff=res["max_abs_diff"],
                argmax_match=(res["argmax_match"]
                              if res["argmax_match"] is not None
                              else False),
                hung=res["hung"],
                masked_tolerance=masked_tolerance,
                acc_degraded_threshold=acc_degraded_threshold)
            target_outcomes[outcome] = target_outcomes.get(outcome, 0) + 1
            cocotb.log.info(
                "fi: run %d/%d on '%s' -> %s halted=%s fault=%s hung=%s "
                "status=%s diff=%s argmax_match=%s halt_cycle=%d",
                run_idx, num_injections, tn, outcome, res["halted"],
                res["fault"], res["hung"], res["status"],
                res["max_abs_diff"], res["argmax_match"], res["cycles"])

            for fid, (cyc, bit) in enumerate(schedule):
                _emit({
                    "model": model_name,
                    "run_id": global_run_id, "fault_id": fid,
                    "tag": "inject",
                    "campaign": campaign, "target": tn,
                    "target_class": target_class,
                    "target_group": target_group,
                    "fault_class": fault_class,
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
                                     if res["max_abs_diff"] is not None
                                     else ""),
                    "argmax_match": (res["argmax_match"]
                                     if res["argmax_match"] is not None
                                     else ""),
                    "outcome": outcome,
                })
        summary[tn] = target_outcomes

    csv_file.close()
    fallback_file.close()
    cocotb.log.info("fi: wrote %d rows -> %s (fallback: %s)",
                    row_count, csv_path, fallback_path)

    # Per-target outcome summary.
    cocotb.log.info(
        "fi: ===== campaign=%s model=%s summary (%d run(s)/target) =====",
        campaign, model_name, num_injections)
    for tn, counts in summary.items():
        meta = fi_utils.TARGETS[tn]
        cocotb.log.info(
            "fi:   target=%-22s class=%-7s group=%-3s -> %s",
            tn, meta["class"], meta["group"], counts)
