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

Runs the 4-module x 3-fault-type vulnerability matrix against one model. Each
(module, fault_type) pair is one experiment group: FI_N runs, each with a
single fault at a random bit (uniform over the module's whole fault space) and
a random cycle. Outcomes are bucketed into the three-layer / six-bucket
taxonomy (see fi_utils). Per-run rows go to fi_results.csv; per-group layer &
bucket shares go to fi_summary.csv and the log.

Env knobs (all optional):
    FI_MODULE      decode_path | compute_ctrl | execute | storage | all  (all)
    FI_FAULT_TYPE  seu | set | stuck | all                               (all)
    FI_STUCK_VAL   0 | 1   (polarity for the stuck model)                (0)
    FI_N           runs per group                                        (50)
    FI_SEED        RNG seed (deterministic re-runs)                      (0xC0DE)
    FI_DUMP_HIERARCHY  if set, dump cocotb hierarchy (depth 4) then run.

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

    if inject_task is not None and not inject_task.done():
        inject_task.kill()
    for t in spawned_holders:
        if not t.done():
            t.kill()

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


def _make_inject_cb(target, local_bit, inject_cycle, fault_type, stuck_val):
    """Build the coroutine that waits `inject_cycle` then performs the flip."""
    signal = target["handle"]

    async def _cb(dut, clock, holders):
        if inject_cycle > 0:
            await ClockCycles(clock, inject_cycle)
        if fault_type == "seu":
            await fi_utils.persistent_bit_flip(clock, signal, local_bit)
        elif fault_type == "set":
            await fi_utils.transient_bit_flip(clock, signal, local_bit)
        elif fault_type == "stuck":
            holders.append(cocotb.start_soon(
                fi_utils.permanent_stuck_at(clock, signal, local_bit,
                                            stuck_val)))
        else:
            raise ValueError(f"unknown fault_type '{fault_type}'")
    return _cb


# Per-run CSV. Columns carry the group key (module + fault_type), the exact
# injection site (target signal path + local/global bit), run status, and the
# six-bucket outcome. Per-group layer/bucket shares live in fi_summary.csv.
RESULT_FIELDS = [
    "model", "module", "ft_scheme", "fault_type", "stuck_val",
    "run_id", "tag", "target_path", "local_bit", "global_bit",
    "fault_space_bits", "inject_cycle", "halt_cycle",
    "halted", "faulted", "hung", "status",
    "output_bitexact", "argmax_match", "outcome",
]

SUMMARY_FIELDS = [
    "model", "module", "ft_scheme", "fault_type", "stuck_val", "n_runs",
    # three-layer shares
    "MASKED_pct", "SDC_pct", "DUE_pct",
    # six-bucket shares
    "MASKED_b_pct", "SDC-benign_pct", "SDC-critical_pct",
    "DUE-hang_pct", "DUE-crash_pct", "DUE-detected_pct",
]


def _pct(n, total):
    return round(100.0 * n / total, 2) if total else 0.0


def _summary_row(model, module, ft_scheme, fault_type, stuck_val, counts):
    n = sum(counts.values())
    layer = {"MASKED": 0, "SDC": 0, "DUE": 0}
    for bucket, c in counts.items():
        layer[fi_utils.LAYER_OF[bucket]] += c
    return {
        "model": model, "module": module, "ft_scheme": ft_scheme,
        "fault_type": fault_type, "stuck_val": stuck_val, "n_runs": n,
        "MASKED_pct": _pct(layer["MASKED"], n),
        "SDC_pct": _pct(layer["SDC"], n),
        "DUE_pct": _pct(layer["DUE"], n),
        "MASKED_b_pct": _pct(counts["MASKED"], n),
        "SDC-benign_pct": _pct(counts["SDC-benign"], n),
        "SDC-critical_pct": _pct(counts["SDC-critical"], n),
        "DUE-hang_pct": _pct(counts["DUE-hang"], n),
        "DUE-crash_pct": _pct(counts["DUE-crash"], n),
        "DUE-detected_pct": _pct(counts["DUE-detected"], n),
    }


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

    # CSV tag: identifies this target's group so parallel runs don't collide.
    tag_parts = []
    if module is not None:
        tag_parts.append(module)
    if fault_type is not None:
        tag_parts.append(fault_type)
    csv_tag = ("_" + "_".join(tag_parts)) if tag_parts else ""

    cocotb.log.info(
        "fi: model=%s seed=%d modules=%s fault_types=%s N=%d stuck_val=%d",
        model_name, seed, modules, fault_types, n_runs, stuck_val)

    if os.environ.get("FI_DUMP_HIERARCHY"):
        cocotb.log.info("fi: dumping cocotb hierarchy (depth=4)")
        fi_utils.dump_hierarchy(dut, max_depth=4)

    # ---- Golden run x2 (determinism gate for the threshold-free taxonomy) --
    cocotb.log.info("fi: ===== golden run #1 (model=%s) =====", model_name)
    g1 = await _run_once(dut, fixture, elf_path, input_data, expected_output)
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
        targets = fi_utils.collect_targets(dut, module)
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

        for fault_type in fault_types:
            counts = {b: 0 for b in fi_utils.OUTCOMES}
            cocotb.log.info(
                "fi: ===== group module=%s fault_type=%s : %d runs =====",
                module, fault_type, n_runs)
            for _ in range(n_runs):
                run_id += 1
                gbit = rng.randrange(space)
                inj_cycle = rng.randint(_INJECT_CYCLE_MIN, upper_cycle)
                target, lbit = _locate_global_bit(targets, gbit)
                cb = _make_inject_cb(target, lbit, inj_cycle,
                                     fault_type, stuck_val)
                res = await _run_once(
                    dut, fixture, elf_path, input_data, expected_output,
                    inject_cb=cb, timeout_cycles=hang_timeout)
                outcome = fi_utils.classify_outcome(
                    hung=res["hung"], faulted=res["faulted"],
                    status=(res["status"] if res["status"] is not None else -1),
                    output_bitexact=bool(res["bitexact"]),
                    argmax_match=bool(res["argmax_match"]))
                counts[outcome] += 1
                res_w.writerow({
                    "model": model_name, "module": module,
                    "ft_scheme": ft_scheme, "fault_type": fault_type,
                    "stuck_val": (stuck_val if fault_type == "stuck" else ""),
                    "run_id": run_id, "tag": "inject",
                    "target_path": target["path"], "local_bit": lbit,
                    "global_bit": gbit, "fault_space_bits": space,
                    "inject_cycle": inj_cycle, "halt_cycle": res["cycles"],
                    "halted": res["halted"], "faulted": res["faulted"],
                    "hung": res["hung"],
                    "status": (res["status"]
                               if res["status"] is not None else ""),
                    "output_bitexact": (res["bitexact"]
                                        if res["bitexact"] is not None else ""),
                    "argmax_match": (res["argmax_match"]
                                     if res["argmax_match"] is not None else ""),
                    "outcome": outcome,
                })
                res_f.flush()
            sum_w.writerow(_summary_row(
                model_name, module, ft_scheme, fault_type, stuck_val, counts))
            sum_f.flush()
            cocotb.log.info("fi: group module=%s fault_type=%s -> %s",
                            module, fault_type, counts)

    res_f.close()
    sum_f.close()
    cocotb.log.info("fi: done. %d injected runs. results=%s summary=%s",
                    run_id, res_path, sum_path)
