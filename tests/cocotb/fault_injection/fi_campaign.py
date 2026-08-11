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

"""Module-level fault-injection campaign driver for the RVV core.

Runs the 4-sub-module x 3-fault-type vulnerability matrix against one model. Each
(module, fault_type) pair is one experiment group: FI_N runs, each with a
single fault at a random bit (uniform over the module's whole fault space) and
a random cycle. Outcomes are bucketed into the three-layer / six-bucket
taxonomy (see fi_utils). Per-run rows go to fi_results.csv; per-group layer &
bucket shares go to fi_summary.csv and the log.

Env knobs (all optional):
    FI_MODULE      decode_path | compute_ctrl | execute | storage        (all)
                   'all'   = those four, the analysis matrix
                   'every' = plus the diagnostics (rob_data, fifo_ptr),
                             which are otherwise reachable only by name
    FI_FAULT_TYPE  seu | set | stuck | all                               (all)
    FI_STUCK_VAL   0 | 1   (polarity for the stuck model)                (0)
    FI_N           runs per group                                        (50)
    FI_SEED        RNG seed (deterministic re-runs)                      (0xC0DE)
    FI_DUMP_HIERARCHY  if set, dump cocotb hierarchy (depth 4) then run.
    FI_TMR_DEFEAT  reverse control: strike TWO TMR copies of one logical
                   bit per run instead of one cell. Requires an FT_ON
                   build; asserts that the runs DO break the design. Not
                   a fault model and not part of the matrix -- see
                   _make_defeat_cb and fi_utils.tmr_logical_map.

Design notes:
  * Single fault per run only. Cumulative-fault stress and activity-gating
    from the previous prototype were removed: this flow measures per-module
    architectural vulnerability, where one fault per run is the clean unit.
  * Thresholds were removed entirely. MASKED requires a bit-exact output, so
    the golden run must be deterministic; run_campaign verifies this by
    running golden twice and asserting the two outputs are identical.
"""

import csv
import os
import random
import re

import cocotb
import numpy as np
from cocotb.triggers import ClockCycles, with_timeout

import fi_utils

_DEFAULT_N = 50
_DEFAULT_SEED = 0xC0DE
_INJECT_CYCLE_MIN = 1000
_TIMEOUT_CYCLES = 50_000_000
_HANG_MARGIN_MULT = 2.0
# Hard ceiling on the post-halt readback waits. A fault that corrupts core
# state could in principle wedge an AXI readback forever; the cycle watchdog
# in _wait_for_outcome only covers the run-to-halt phase, not these reads. We
# bound them so a run can never hang the campaign with no feedback.
_READBACK_TIMEOUT_NS = 5_000_000


def _outputs_dir():
    d = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if d:
        os.makedirs(d, exist_ok=True)
        return d
    return os.getcwd()


async def _wait_for_outcome(dut, timeout_cycles):
    """Tick until io_halted, io_fault, or timeout.

    Returns (cycles, halted, faulted, hung)."""
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
    """Load ELF, stage input, kick off, optionally inject, observe one run.

    `inject_cb(dut, clock, holders)` is spawned after execute_from; it waits
    its target cycle and performs the flip(s). `holders` collects long-lived
    stuck-at tasks so we can kill them at end of run.

    Returns a metrics dict."""
    await fixture.load_elf_and_lookup_symbols(
        elf_path,
        ["inference_status", "inference_status_message",
         "inference_input", "inference_output"],
    )
    await fixture.write("inference_input", input_data)
    await fixture.write("inference_output",
                        np.zeros(expected_output.size, dtype=np.int8))

    await fixture.core_mini_axi.execute_from(fixture.entry_point)

    spawned_holders = []
    inject_task = None
    if inject_cb is not None:
        inject_task = cocotb.start_soon(
            inject_cb(dut, dut.io_aclk, spawned_holders))

    cycles, halted, faulted, hung = await _wait_for_outcome(
        dut, timeout_cycles)

    # Attribute the hang BEFORE tearing anything down -- the ROB state that
    # says which kind of hang this is only exists while the sim is still up,
    # and the stuck-at holders must stay alive or the fault would lift and the
    # design could start moving again mid-snapshot. DUE-hang alone cannot
    # price a watchdog: only the `omission` class is recoverable by one.
    hang_info = None
    if hung:
        hang_info = await fi_utils.hang_snapshot(dut, dut.io_aclk)

    # cancel(), not kill(): the latter is deprecated in cocotb 2.0.
    if inject_task is not None and not inject_task.done():
        inject_task.cancel()
    for t in spawned_holders:
        if not t.done():
            t.cancel()

    status = None
    actual_output = None
    bitexact = None
    argmax_match = None
    if halted:
        try:
            status = int((await with_timeout(
                fixture.read_word("inference_status"),
                _READBACK_TIMEOUT_NS, "ns")).view(np.int32)[0])
            actual_output = (await with_timeout(
                fixture.read("inference_output", expected_output.size),
                _READBACK_TIMEOUT_NS, "ns")).view(np.int8)
            bitexact = bool(np.array_equal(actual_output, expected_output))
            argmax_match = (int(np.argmax(actual_output))
                            == int(np.argmax(expected_output)))
        except Exception as e:  # noqa: BLE001  (incl. cocotb SimTimeoutError)
            # A wedged readback is itself a fault effect: leave halted=True but
            # treat the run as a detected/hung failure downstream (no bitexact).
            cocotb.log.warning("fi: readback failed/timed out after halt: %s", e)

    return {
        "cycles": cycles, "halted": halted, "faulted": faulted, "hung": hung,
        "status": status, "bitexact": bitexact, "argmax_match": argmax_match,
        "actual_output": (actual_output.tolist()
                          if actual_output is not None else None),
        "hang_info": hang_info,
    }


def _locate_global_bit(targets, global_bit):
    """Map a global bit index (over concatenated target widths) to the owning
    target dict and its local bit index."""
    acc = 0
    for t in targets:
        if global_bit < acc + t["width"]:
            return t, global_bit - acc
        acc += t["width"]
    raise IndexError(f"global_bit {global_bit} exceeds fault space {acc}")


def _make_inject_cb(target, local_bit, inject_cycle, fault_type, stuck_val,
                    set_timeout=None):
    """Build the coroutine that waits `inject_cycle` then performs the flip.

    Returns (cb, state). `state["fired"]` reports whether the fault was
    actually expressed; only `set` can come back False, when the cell is never
    written again before the run ends (see fi_utils.transient_bit_flip)."""
    signal = target["handle"]
    e_handle, _ = fi_utils.enable_handles(target)
    state = {"fired": True}

    async def _cb(dut, clock, holders):
        if inject_cycle > 0:
            await ClockCycles(clock, inject_cycle)
        if fault_type == "seu":
            await fi_utils.persistent_bit_flip(clock, signal, local_bit)
        elif fault_type == "set":
            await fi_utils.transient_bit_flip(
                clock, signal, local_bit, e_handle=e_handle,
                timeout_cycles=set_timeout, state=state)
        elif fault_type == "stuck":
            holders.append(cocotb.start_soon(
                fi_utils.permanent_stuck_at(clock, signal, local_bit,
                                            stuck_val)))
        else:
            raise ValueError(f"unknown fault_type '{fault_type}'")
    return _cb, state


def _make_defeat_cb(cells, inject_cycle, fault_type, stuck_val,
                    set_timeout=None):
    """Reverse control: inject the same logical bit in TWO TMR copies at once.

    `cells` is [(target, local_bit), ...] for one logical bit, of which the
    first two are struck. Breaking the majority is not a fault model -- it is
    the only way to prove the injector is bound to the redundant storage rather
    than to the voter's output, since both bindings otherwise yield an all-
    MASKED table. See fi_utils.tmr_logical_map.

    `state["fired"]` is AND-ed across the copies: for `set`, a defeat only
    counts if every strike actually landed on a write."""
    state = {"fired": True}
    sub = []
    for target, lbit in cells:
        e_handle, _ = fi_utils.enable_handles(target)
        sub.append((target["handle"], lbit, e_handle, {"fired": True}))

    async def _cb(dut, clock, holders):
        if inject_cycle > 0:
            await ClockCycles(clock, inject_cycle)
        # One task covering every struck cell, never one task per cell. The
        # copies of a packed register (uop_done_tmr holds all three) live in a
        # single handle, so two independent read-modify-writes at the same edge
        # would clobber each other and land only ONE bit -- a single-copy
        # strike, which the voter correctly masks, making a broken control look
        # like a working scheme. See fi_utils.persistent_multi_flip.
        cells_hb = [(signal, lbit) for signal, lbit, _e, _st in sub]
        if fault_type == "stuck":
            holders.append(cocotb.start_soon(
                fi_utils.permanent_multi_stuck_at(clock, cells_hb, stuck_val)))
            return
        if fault_type == "seu":
            # Both copies in the same cycle: a voter sees two-of-three wrong.
            # Sequential strikes would let the scrub repair copy 0 before copy
            # 1 is hit, which is single-fault tolerance working, not a defeat.
            await fi_utils.persistent_multi_flip(clock, cells_hb)
            return
        if fault_type == "set":
            tasks = [cocotb.start_soon(
                fi_utils.transient_bit_flip(clock, signal, lbit,
                                            e_handle=e, timeout_cycles=set_timeout,
                                            state=st))
                for signal, lbit, e, st in sub]
            for t in tasks:
                await t
            state["fired"] = all(st.get("fired", False)
                                 for _s, _b, _e, st in sub)
            return
        raise ValueError(f"unknown fault_type '{fault_type}'")
    return _cb, state


# Per-run CSV. Columns carry the group key (module + fault_type), the exact
# injection site (target signal path + local/global bit), run status, and the
# six-bucket outcome. Per-group layer/bucket shares live in fi_summary.csv.
RESULT_FIELDS = [
    "model", "module", "ft_scheme", "fault_type", "stuck_val", "ft_build",
    "run_id", "tag", "target_path", "local_bit", "global_bit",
    "fault_space_bits", "bit_live", "inject_cycle", "fault_fired", "halt_cycle",
    "halted", "faulted", "hung", "status",
    "output_bitexact", "argmax_match", "outcome",
    # DUE-hang attribution (blank for every non-hung run). A watchdog can only
    # recover hang_class == "omission"; see fi_utils.hang_snapshot.
    "hang_class", "stuck_n", "stuck_entries", "stuck_units", "stuck_is_ft",
    "rob_busy", "rob_wptr", "rob_rptr", "rs_occupancy",
]

_HANG_FIELDS = ("hang_class", "stuck_n", "stuck_entries", "stuck_units",
                "stuck_is_ft", "rob_busy", "rob_wptr", "rob_rptr",
                "rs_occupancy")

SUMMARY_FIELDS = [
    "model", "module", "ft_scheme", "fault_type", "stuck_val", "ft_build",
    "n_runs",
    # three-layer shares over the whole fault space (raw AVF)
    "MASKED_pct", "SDC_pct", "DUE_pct",
    # six-bucket shares
    "MASKED_b_pct", "SDC-benign_pct", "SDC-critical_pct",
    "DUE-hang_pct", "DUE-crash_pct", "DUE-detected_pct",
    # conditional AVF: same layers, but normalised over the bits this workload
    # actually exercises (see _liveness_watcher)
    "live_bits", "live_frac_pct", "n_runs_live",
    "MASKED_pct_live", "SDC_pct_live", "DUE_pct_live",
    # runs whose fault was never expressed (set on a cell never written again);
    # they are NOT evidence of tolerance and are excluded from *_fired shares
    "n_runs_not_fired", "MASKED_pct_fired", "SDC_pct_fired", "DUE_pct_fired",
    # How the DUE-hang runs break down. n_hang_omission is the only column a
    # watchdog could turn into a recovered run; the rest are what it cannot
    # help with, and reporting them together is the point (a bare DUE-hang
    # count silently credits a watchdog with all three).
    "n_hang", "n_hang_omission", "n_hang_starvation", "n_hang_deadlock",
    "n_hang_external", "n_hang_progressing", "n_hang_unknown",
    "omission_pct_of_hang",
]

_HANG_CLASSES = ("omission", "starvation", "deadlock", "external",
                 "progressing", "unknown")


def _pct(n, total):
    return round(100.0 * n / total, 2) if total else 0.0


def _layers(counts):
    layer = {"MASKED": 0, "SDC": 0, "DUE": 0}
    for bucket, c in counts.items():
        layer[fi_utils.LAYER_OF[bucket]] += c
    return layer


def _summary_row(model, module, ft_scheme, fault_type, stuck_val, counts,
                 counts_live=None, live_bits=0, space=0,
                 counts_fired=None, n_not_fired=0, ft_build="",
                 hang_counts=None):
    n = sum(counts.values())
    layer = _layers(counts)
    counts_live = counts_live or {}
    n_live = sum(counts_live.values())
    layer_live = _layers(counts_live)
    counts_fired = counts_fired if counts_fired is not None else counts
    n_fired = sum(counts_fired.values())
    layer_fired = _layers(counts_fired)
    hc = hang_counts or {c: 0 for c in _HANG_CLASSES}
    n_hang = sum(hc.values())
    return {
        "model": model, "module": module, "ft_scheme": ft_scheme,
        "fault_type": fault_type, "stuck_val": stuck_val,
        "ft_build": ft_build, "n_runs": n,
        "MASKED_pct": _pct(layer["MASKED"], n),
        "SDC_pct": _pct(layer["SDC"], n),
        "DUE_pct": _pct(layer["DUE"], n),
        "MASKED_b_pct": _pct(counts["MASKED"], n),
        "SDC-benign_pct": _pct(counts["SDC-benign"], n),
        "SDC-critical_pct": _pct(counts["SDC-critical"], n),
        "DUE-hang_pct": _pct(counts["DUE-hang"], n),
        "DUE-crash_pct": _pct(counts["DUE-crash"], n),
        "DUE-detected_pct": _pct(counts["DUE-detected"], n),
        "live_bits": live_bits, "live_frac_pct": _pct(live_bits, space),
        "n_runs_live": n_live,
        "MASKED_pct_live": _pct(layer_live["MASKED"], n_live),
        "SDC_pct_live": _pct(layer_live["SDC"], n_live),
        "DUE_pct_live": _pct(layer_live["DUE"], n_live),
        "n_runs_not_fired": n_not_fired,
        "MASKED_pct_fired": _pct(layer_fired["MASKED"], n_fired),
        "SDC_pct_fired": _pct(layer_fired["SDC"], n_fired),
        "DUE_pct_fired": _pct(layer_fired["DUE"], n_fired),
        "n_hang": n_hang,
        "n_hang_omission": hc["omission"],
        "n_hang_starvation": hc["starvation"],
        "n_hang_deadlock": hc["deadlock"],
        "n_hang_external": hc["external"],
        "n_hang_progressing": hc["progressing"],
        "n_hang_unknown": hc["unknown"],
        "omission_pct_of_hang": _pct(hc["omission"], n_hang),
    }


# ---------------------------------------------------------------------------
# Bit liveness / dead silicon (A-1.5).
#
# A uniform pick over the fault space spends most of its budget on bits this
# workload never exercises (div / falu / pmtrdt are ~60% of the execute space
# and this keyword-spotting model never issues those ops). Those bits are
# MASKED by construction, so the raw AVF is a real number about THIS workload
# but a misleading one about the hardware: it moves whenever the unused-unit
# area moves, for reasons that have nothing to do with vulnerability.
#
# So we report both. Liveness is sampled on the golden run -- which the
# campaign already performs for the determinism gate -- by periodically
# snapshotting every target and OR-ing value ^ first_value. A bit that never
# toggles over the whole uninjected run is dead for this workload. Conditional
# AVF then normalises over the live bits only, and both numbers ship together.
#
# This observes the golden trajectory, it does not alter it: reads only.
# ---------------------------------------------------------------------------
_DEFAULT_LIVENESS_PERIOD = 200


async def _liveness_watcher(clock, targets, period, out):
    """Snapshot targets every `period` cycles; accumulate per-bit toggle masks
    into `out` (list of ints, one per target, index-aligned with `targets`)."""
    first = [None] * len(targets)
    while True:
        await ClockCycles(clock, period)
        for i, t in enumerate(targets):
            v = fi_utils.read_int(t["handle"])
            if v is None:
                continue
            if first[i] is None:
                first[i] = v
            else:
                out[i] |= (v ^ first[i])


# ---------------------------------------------------------------------------
# Deposit positive control (A-1.1). Separate entry point from run_campaign:
# this measures the INSTRUMENT, not the design, and must never feed the
# vulnerability data. One short simulation, many probes, no run-to-halt --
# ~200 probes x 1000 cycles is under one golden run yet gives direct evidence,
# where sampling full campaigns would only give indirect evidence at 12x the
# cost. See fi_utils.probe_deposit for what landed/survived mean.
# ---------------------------------------------------------------------------
PROBE_FIELDS = [
    "model", "module", "target_class", "target_path", "local_bit",
    "global_bit", "phase", "probe_cycle", "pre", "mid", "post", "e", "c",
    "landed", "survived",
]

_DEFAULT_PROBE_N = 200
_DEFAULT_PROBE_STRIDE = 1000


def _rate(num, den):
    return f"{100.0 * num / den:5.1f}%({num}/{den})" if den else "    -    "


def _probe_table(rows):
    """Group probe rows by (module, target_class, phase) and render the
    acceptance table. `survived` is split by `e` because a flip that vanishes
    while the write enable was high is correct behaviour, not a bug."""
    keys = []
    for r in rows:
        k = (r["module"], r["target_class"], r["phase"])
        if k not in keys:
            keys.append(k)
    lines = [
        "fi-probe: %-13s %-15s %-5s %6s  %-15s %-15s %-15s %-15s" % (
            "module", "target_class", "phase", "n", "landed", "survived",
            "survived|e=0", "survived|e=1"),
    ]
    for k in sorted(keys):
        grp = [r for r in rows
               if (r["module"], r["target_class"], r["phase"]) == k]
        land = [r for r in grp if r["landed"] is not None]
        surv = [r for r in grp if r["survived"] is not None]
        e0 = [r for r in surv if r["e"] == 0]
        e1 = [r for r in surv if r["e"] == 1]
        lines.append(
            "fi-probe: %-13s %-15s %-5s %6d  %-15s %-15s %-15s %-15s" % (
                k[0], k[1], k[2], len(grp),
                _rate(sum(1 for r in land if r["landed"]), len(land)),
                _rate(sum(1 for r in surv if r["survived"]), len(surv)),
                _rate(sum(1 for r in e0 if r["survived"]), len(e0)),
                _rate(sum(1 for r in e1 if r["survived"]), len(e1))))
    return lines


async def run_probe(dut, fixture, elf_path, input_data, expected_output, *,
                    model_name, n_probes=None, stride=None, seed=None,
                    phases=("pose", "nege")):
    """Probe the deposit mechanism across every module's target classes.

    Env knobs: FI_PROBE_N (200), FI_PROBE_STRIDE (1000), FI_SEED, and
    FI_PROBE_STRICT (if set, a landed rate below 100% fails the test instead
    of only being reported -- for use as a standing guard once A-1.1 is
    closed; the diagnostic run wants the table, not an exception)."""
    n_probes = int(os.environ.get("FI_PROBE_N", n_probes or _DEFAULT_PROBE_N))
    stride = int(os.environ.get("FI_PROBE_STRIDE",
                                stride or _DEFAULT_PROBE_STRIDE))
    seed = int(str(os.environ.get("FI_SEED", seed or _DEFAULT_SEED)), 0)
    rng = random.Random(seed)

    modules = list(fi_utils.MODULES)
    cocotb.log.info("fi-probe: model=%s seed=%d n=%d stride=%d phases=%s",
                    model_name, seed, n_probes, stride, phases)

    # Same bring-up as an injected run, but we never wait for the halt: the
    # probe only needs the design to be actively clocking real work.
    await fixture.load_elf_and_lookup_symbols(
        elf_path,
        ["inference_status", "inference_status_message",
         "inference_input", "inference_output"],
    )
    await fixture.write("inference_input", input_data)
    await fixture.write("inference_output",
                        np.zeros(expected_output.size, dtype=np.int8))
    await fixture.core_mini_axi.execute_from(fixture.entry_point)

    space = {}
    for m in modules:
        tg = fi_utils.collect_targets(dut, m)
        space[m] = (tg, sum(t["width"] for t in tg))
        cocotb.log.info("fi-probe: module '%s': %d targets, %d bits",
                        m, len(tg), space[m][1])
        # Composition of the fault space, indices collapsed. The campaign only
        # ever reports the total, which hides both missing cells (A-1.3/A-1.4)
        # and cells that appear or vanish when Verilator's inlining decisions
        # change -- exposing a wrapper port is enough to move that line.
        comp = {}
        for t in tg:
            pat = re.sub(r"\[\d+\]", "[]", t["path"])
            n, b = comp.get(pat, (0, 0))
            comp[pat] = (n + 1, b + t["width"])
        for pat, (n, b) in sorted(comp.items(), key=lambda kv: -kv[1][1]):
            cocotb.log.info("fi-probe:   %6d b  x%-4d  %s", b, n, pat)

    await ClockCycles(dut.io_aclk, _INJECT_CYCLE_MIN)
    cycle = _INJECT_CYCLE_MIN

    rows = []
    for i in range(n_probes):
        await ClockCycles(dut.io_aclk, stride)
        cycle += stride
        if int(dut.io_halted.value) == 1 or int(dut.io_fault.value) == 1:
            cocotb.log.info(
                "fi-probe: run ended at cycle %d after %d probes; stopping "
                "(probe perturbs the design, an early end is expected)",
                cycle, i)
            break
        module = modules[i % len(modules)]
        targets, bits = space[module]
        if bits == 0:
            continue
        gbit = rng.randrange(bits)
        target, lbit = _locate_global_bit(targets, gbit)
        e_h, c_h = fi_utils.enable_handles(target)
        # Phase advances once per full pass over the modules; stepping it per
        # probe would alias against the module rotation and pin each module to
        # a single phase (len(phases) divides len(modules)).
        phase = phases[(i // len(modules)) % len(phases)]
        r = await fi_utils.probe_deposit(
            dut.io_aclk, target["handle"], lbit,
            phase=phase, e_handle=e_h, c_handle=c_h)
        cycle += 2
        r.update({"model": model_name, "module": module,
                  "target_class": fi_utils.target_class(target),
                  "target_path": target["path"], "local_bit": lbit,
                  "global_bit": gbit, "probe_cycle": cycle})
        rows.append(r)

    out_dir = _outputs_dir()
    path = os.path.join(out_dir, "fi_probe.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PROBE_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k])
                        for k in PROBE_FIELDS})
    for line in _probe_table(rows):
        cocotb.log.info("%s", line)
    cocotb.log.info("fi-probe: %d probes -> %s", len(rows), path)

    landed = [r for r in rows if r["landed"] is not None]
    n_bad = sum(1 for r in landed if not r["landed"])
    if n_bad:
        msg = (f"fi-probe: {n_bad}/{len(landed)} deposits did NOT land -- the "
               "injection mechanism is broken for those targets, campaign "
               "results covering them are not interpretable")
        if os.environ.get("FI_PROBE_STRICT"):
            raise AssertionError(msg)
        cocotb.log.error("%s", msg)
    else:
        cocotb.log.info("fi-probe: all %d measured deposits landed", len(landed))
    return rows


async def run_campaign(dut, fixture, elf_path, input_data, expected_output,
                       *, model_name, module=None, fault_type=None,
                       stuck_val=None):
    """Run the FI matrix for a single model.

    Group selection precedence: explicit args (module/fault_type/stuck_val,
    used by the per-group thin-shell testcases so each bazel target runs ONE
    group and they parallelize across cores) override the FI_* env vars, which
    in turn fall back to 'all'. CSV filenames are tagged with the resolved
    group so parallel targets never clobber one another's output.

    Writes fi_results[_tag].csv (one row per run) and fi_summary[_tag].csv (one
    row per group). Outcomes use the threshold-free three-layer taxonomy, so
    the golden run must be deterministic; we verify that up front."""
    seed = int(str(os.environ.get("FI_SEED", _DEFAULT_SEED)), 0)
    module_spec = module if module is not None else os.environ.get(
        "FI_MODULE", "all")
    modules = fi_utils.expand_module_spec(module_spec)
    ft_spec = fault_type if fault_type is not None else os.environ.get(
        "FI_FAULT_TYPE", "all")
    fault_types = (list(fi_utils.FAULT_TYPES) if ft_spec == "all"
                   else [ft_spec])
    for ft in fault_types:
        if ft not in fi_utils.FAULT_TYPES:
            raise ValueError(f"fault_type '{ft}' not in {fi_utils.FAULT_TYPES}")
    stuck_val = (stuck_val if stuck_val is not None
                 else int(os.environ.get("FI_STUCK_VAL", "0")))
    n_runs = int(os.environ.get("FI_N", _DEFAULT_N))
    rng = random.Random(seed)

    # Which RTL build is under us. Probed from the hierarchy, never assumed:
    # the FT switch is a `define in the RTL, so anything else could disagree
    # with the model actually being simulated. It selects the FT_ON registry
    # paths and is recorded in both CSVs -- an FT_ON and an FT_OFF campaign
    # produce identically-shaped files, and telling them apart after the fact
    # is the whole point of the comparison.
    ft_on = fi_utils.ft_build_is_on(dut)
    ft_build = "FT_ON" if ft_on else "FT_OFF"
    # Reverse control (see fi_utils.tmr_logical_map): strike two TMR copies of
    # one logical bit, which MUST break the design. Not a fault model, so it
    # never runs as part of the matrix and its CSVs are tagged apart.
    defeat = bool(os.environ.get("FI_TMR_DEFEAT"))
    if defeat and not ft_on:
        raise AssertionError(
            "FI_TMR_DEFEAT needs a FAULT_TOLERANT_ON build: there is no "
            "redundancy to defeat on a baseline build.")

    # CSV tag: identifies this target's group so parallel runs don't collide.
    tag_parts = []
    if module is not None:
        tag_parts.append(module)
    if fault_type is not None:
        tag_parts.append(fault_type)
    if defeat:
        tag_parts.append("tmrdefeat")
    csv_tag = ("_" + "_".join(tag_parts)) if tag_parts else ""

    cocotb.log.info(
        "fi: model=%s seed=%d modules=%s fault_types=%s N=%d stuck_val=%d "
        "build=%s%s",
        model_name, seed, modules, fault_types, n_runs, stuck_val, ft_build,
        " TMR-DEFEAT(reverse control)" if defeat else "")

    if os.environ.get("FI_DUMP_HIERARCHY"):
        cocotb.log.info("fi: dumping cocotb hierarchy (depth=4)")
        fi_utils.dump_hierarchy(dut, max_depth=4)

    # ---- Targets, collected up front so the golden run can measure liveness -
    collected = {m: fi_utils.collect_targets(dut, m, ft_on=ft_on)
                 for m in modules}
    live_masks = {m: [0] * len(collected[m]) for m in modules}
    live_period = int(os.environ.get("FI_LIVENESS_PERIOD",
                                     _DEFAULT_LIVENESS_PERIOD))

    async def _watch_liveness(_dut, clock, holders):
        """Piggy-backs on the inject_cb hook of the golden run: spawns one
        read-only watcher per module and lets _run_once kill them at the end."""
        if live_period <= 0:
            return
        for m in modules:
            holders.append(cocotb.start_soon(
                _liveness_watcher(clock, collected[m], live_period,
                                  live_masks[m])))

    # ---- Golden run x2 (determinism gate for the threshold-free taxonomy) --
    cocotb.log.info("fi: ===== golden run #1 (model=%s, +liveness) =====",
                    model_name)
    g1 = await _run_once(dut, fixture, elf_path, input_data, expected_output,
                         inject_cb=_watch_liveness)
    assert g1["halted"] and g1["status"] == 0 and g1["bitexact"], (
        f"golden#1 bad: halted={g1['halted']} status={g1['status']} "
        f"bitexact={g1['bitexact']} faulted={g1['faulted']} hung={g1['hung']}")
    cocotb.log.info("fi: ===== golden run #2 (determinism check) =====")
    g2 = await _run_once(dut, fixture, elf_path, input_data, expected_output)
    assert g2["actual_output"] == g1["actual_output"], (
        "golden runs are NOT bit-identical across repetitions; the "
        "threshold-free MASKED=bit-exact rule needs a deterministic golden. "
        "Investigate before trusting outcomes.")
    golden_halt = g1["cycles"]
    hang_timeout = max(_INJECT_CYCLE_MIN + 10_000,
                       int(golden_halt * _HANG_MARGIN_MULT))
    upper_cycle = max(_INJECT_CYCLE_MIN + 1, golden_halt)
    cocotb.log.info("fi: golden halt=%d, per-run timeout=%d",
                    golden_halt, hang_timeout)

    # Attach the measured liveness to the targets and report the dead-silicon
    # share per module. A 0% live figure means the watcher never ran (period
    # disabled) -- conditional AVF is then simply absent, not zero.
    live_bits = {}
    for m in modules:
        for t, mask in zip(collected[m], live_masks[m]):
            t["live_mask"] = mask
        live_bits[m] = sum(bin(mask).count("1") for mask in live_masks[m])
        space_m = sum(t["width"] for t in collected[m])
        cocotb.log.info(
            "fi: module '%s' liveness: %d/%d bits toggled during golden "
            "(%.1f%% live, %.1f%% dead for this workload)",
            m, live_bits[m], space_m, _pct(live_bits[m], space_m),
            _pct(space_m - live_bits[m], space_m))

    # ---- Open CSVs (incremental, per-row flush so partial data survives) ---
    out_dir = _outputs_dir()
    res_path = os.path.join(out_dir, f"fi_results{csv_tag}.csv")
    sum_path = os.path.join(out_dir, f"fi_summary{csv_tag}.csv")
    res_f = open(res_path, "w", newline="")
    sum_f = open(sum_path, "w", newline="")
    res_w = csv.DictWriter(res_f, fieldnames=RESULT_FIELDS)
    sum_w = csv.DictWriter(sum_f, fieldnames=SUMMARY_FIELDS)
    res_w.writeheader()
    sum_w.writeheader()
    res_f.flush()
    sum_f.flush()
    cocotb.log.info("fi: results -> %s ; summary -> %s", res_path, sum_path)

    run_id = 0
    # ---- 4 modules x 3 fault types --------------------------------------
    for module in modules:
        targets = collected[module]
        space = sum(t["width"] for t in targets)
        ft_scheme = fi_utils.MODULES[module]["ft_scheme"]
        if space == 0:
            cocotb.log.error(
                "fi: module '%s' has EMPTY fault space (no targets resolved). "
                "Check vlt exposure / registry paths. Skipping.", module)
            continue
        cocotb.log.info(
            "fi: module '%s' (%s): %d targets, %d-bit fault space",
            module, ft_scheme, len(targets), space)

        groups = None
        if defeat:
            groups = fi_utils.tmr_logical_map(targets)
            expected_groups = space // fi_utils.FT_TMR_COPIES
            assert len(groups) == expected_groups, (
                f"fi: TMR-defeat found {len(groups)} complete logical bits in "
                f"module '{module}', expected {expected_groups} "
                f"({space} bit / {fi_utils.FT_TMR_COPIES} copies). The copy "
                "annotation no longer matches the RTL, and an incomplete group "
                "would strike fewer copies than the majority needs -- i.e. it "
                "would silently become a single-fault run and 'prove' the "
                "opposite of what this control is for.")
            cocotb.log.info(
                "fi: TMR-defeat: %d logical bits, striking %d of %d copies "
                "each (reverse control -- non-MASKED outcomes are the PASS "
                "criterion here, not a vulnerability)",
                len(groups), 2, fi_utils.FT_TMR_COPIES)

        for fault_type in fault_types:
            counts = {b: 0 for b in fi_utils.OUTCOMES}
            counts_live = {b: 0 for b in fi_utils.OUTCOMES}
            counts_fired = {b: 0 for b in fi_utils.OUTCOMES}
            n_not_fired = 0
            hang_counts = {c: 0 for c in _HANG_CLASSES}
            cocotb.log.info(
                "fi: ===== group module=%s fault_type=%s : %d runs =====",
                module, fault_type, n_runs)
            for _ in range(n_runs):
                run_id += 1
                inj_cycle = rng.randint(_INJECT_CYCLE_MIN, upper_cycle)
                if defeat:
                    gidx = rng.randrange(len(groups))
                    cells = groups[gidx][:2]
                    target, lbit = cells[0]
                    gbit = gidx
                    cb, inj_state = _make_defeat_cb(
                        cells, inj_cycle, fault_type, stuck_val,
                        set_timeout=hang_timeout)
                else:
                    gbit = rng.randrange(space)
                    target, lbit = _locate_global_bit(targets, gbit)
                    cb, inj_state = _make_inject_cb(
                        target, lbit, inj_cycle, fault_type, stuck_val,
                        set_timeout=hang_timeout)
                res = await _run_once(
                    dut, fixture, elf_path, input_data, expected_output,
                    inject_cb=cb, timeout_cycles=hang_timeout)
                outcome = fi_utils.classify_outcome(
                    hung=res["hung"], faulted=res["faulted"],
                    status=(res["status"] if res["status"] is not None else -1),
                    output_bitexact=bool(res["bitexact"]),
                    argmax_match=bool(res["argmax_match"]))
                counts[outcome] += 1
                bit_live = bool((target.get("live_mask", 0) >> lbit) & 1)
                if bit_live:
                    counts_live[outcome] += 1
                fired = bool(inj_state.get("fired", True))
                if fired:
                    counts_fired[outcome] += 1
                else:
                    n_not_fired += 1
                hang_info = res.get("hang_info")
                if hang_info is not None:
                    hang_counts[hang_info["hang_class"]] += 1
                res_w.writerow({
                    "model": model_name, "module": module,
                    "ft_scheme": ft_scheme, "fault_type": fault_type,
                    "stuck_val": (stuck_val if fault_type == "stuck" else ""),
                    "ft_build": ft_build,
                    "run_id": run_id,
                    "tag": ("tmr_defeat" if defeat else "inject"),
                    # In defeat mode the row describes a logical bit, so the
                    # path names every struck copy and global_bit is its index
                    # among the logical bits, not among the physical cells.
                    "target_path": (
                        " + ".join(f"{t['path']}[{b}]" for t, b in cells)
                        if defeat else target["path"]),
                    "local_bit": lbit,
                    "global_bit": gbit, "fault_space_bits": space,
                    "bit_live": bit_live,
                    "inject_cycle": inj_cycle, "fault_fired": fired,
                    "halt_cycle": res["cycles"],
                    "halted": res["halted"], "faulted": res["faulted"],
                    "hung": res["hung"],
                    "status": (res["status"]
                               if res["status"] is not None else ""),
                    "output_bitexact": (res["bitexact"]
                                        if res["bitexact"] is not None else ""),
                    "argmax_match": (res["argmax_match"]
                                     if res["argmax_match"] is not None else ""),
                    "outcome": outcome,
                    **{k: (hang_info[k] if hang_info is not None else "")
                       for k in _HANG_FIELDS},
                })
                res_f.flush()
            sum_w.writerow(_summary_row(
                model_name, module, ft_scheme, fault_type, stuck_val, counts,
                counts_live=counts_live, live_bits=live_bits.get(module, 0),
                space=space, counts_fired=counts_fired,
                n_not_fired=n_not_fired, ft_build=ft_build,
                hang_counts=hang_counts))
            sum_f.flush()
            cocotb.log.info("fi: group module=%s fault_type=%s -> %s",
                            module, fault_type, counts)
            cocotb.log.info("fi:   conditional (live bits only, n=%d) -> %s",
                            sum(counts_live.values()), counts_live)
            if sum(hang_counts.values()):
                cocotb.log.info(
                    "fi:   hang attribution -> %s  (only 'omission' is "
                    "watchdog-recoverable)", hang_counts)
            if n_not_fired:
                cocotb.log.info(
                    "fi:   %d/%d runs never expressed the fault (cell not "
                    "written again before end of run); excluded from *_fired",
                    n_not_fired, n_runs)
            if defeat:
                # Inverted verdict. Every MASKED run here is a run in which two
                # of three copies were supposedly wrong and the design did not
                # notice -- which does not mean the design is robust, it means
                # the deposit did not reach the redundant storage. All-MASKED
                # is therefore the failure, and it is fatal: it would otherwise
                # be reported as a clean FT_ON campaign.
                n_bad = sum(c for b, c in counts.items() if b != "MASKED")
                cocotb.log.info(
                    "fi:   TMR-defeat verdict: %d/%d runs broke the design "
                    "(non-MASKED). Expected >0.", n_bad, n_runs)
                assert n_bad > 0, (
                    f"fi: TMR-defeat on '{module}'/{fault_type} produced "
                    f"{n_runs} MASKED runs. Two of three copies of one logical "
                    "bit were struck, so the majority voter had to yield a "
                    "wrong value; a design that shrugs that off is not "
                    "plausible. The injector is almost certainly not bound to "
                    "the TMR storage (e.g. it is depositing on the voter's "
                    "combinational output, where a deposit is recomputed "
                    "away). Any FT_ON campaign taken from this build would "
                    "report a perfect score for the wrong reason.")

    res_f.close()
    sum_f.close()
    cocotb.log.info("fi: done. %d injected runs. results=%s summary=%s",
                    run_id, res_path, sum_path)
