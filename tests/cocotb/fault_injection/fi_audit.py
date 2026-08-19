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

"""Runtime half of the fault-space reconciliation: of the cells this build
actually publishes to VPI, which ones does the campaign registry collect?

The registry (fi_utils.MODULES) is a hand-written list of hierarchical roots.
`space_bits` catches one failure mode of a hand-written list -- a path that
stops resolving -- and nothing catches the other: a cell that was never listed.
That gap is invisible in the results, because a cell no campaign ever strikes
reads as "this state is not vulnerable". `trap_ready` sat outside every
module's fault space for two stages exactly this way.

Pairs with fi_seq_audit.py, and neither side replaces the other:

  static (fi_seq_audit)  reads the preprocessed RTL, so it sees sequential
                         cells the simulator does NOT publish -- the class of
                         gap that needs a new vlt directive before any registry
                         line can help. Cannot evaluate generate conditions, so
                         it over-reports (multi_fifo `dataout` is flopped only
                         under DATAOUT_REG, which no instance sets).
  runtime (this module)  walks the live hierarchy, so it is ground truth for
                         what elaborated, at exact widths and real paths. Blind
                         to anything without a vlt directive: a cell that is
                         not exposed is not "missing from the registry" here,
                         it simply does not exist as far as VPI is concerned.

The audit reports three things, and the third is the one that has actually
found bugs:

  1. exposed-but-unregistered -- silicon inside rvv_backend that no campaign
     can select. Needs a registry line.
  2. per-module fault-space sizes -- the denominators every AVF number in the
     thesis divides by, printed together so they can be checked against the
     RTL by hand once, rather than trusted forever.
  3. overlap between modules -- INV-3 says the partition is disjoint. Two
     modules collecting the same cell would double-count it in the union and
     attribute the same failure to two blocks.

Read-side exposures (`edff.e`, `cdffr.e`, `cdffr.c`) are deliberately excluded
from "unregistered": the vlt template exposes them so the deposit probe can
read a write-enable, and they are inputs, not state. Counting them as gaps
would put three permanent false entries at the top of every report.
"""

import collections

import cocotb

import fi_utils


# Exposed for reading, never fault targets. See module docstring.
READ_SIDE_NAMES = ("e", "c")

# Exposed cells the registry leaves out ON PURPOSE, with the reason. Without
# this list the FT_ON report carries two permanent entries, and a report that
# always shows gaps is a report nobody reads the day a real gap appears.
#
# Each entry is (path suffix under rvv_backend, ft_on_only, reason). The audit
# asserts every one of them is actually present among the exposed leaves for
# the builds it claims to apply to: an exclusion that stops matching would
# otherwise silently become an exclusion of nothing, and the cell it was
# hiding would slip back in unnoticed.
EXCLUDED_BY_DESIGN = (
    ("u_rob.uop_done", True,
     "FT_ON: this name is the majority voter's combinational output, not "
     "storage. A deposit on it is recomputed the same cycle (INV-1); the "
     "three copies in uop_done_tmr are what compute_ctrl injects."),
    ("u_rob.trap_flag", True,
     "FT_ON: voted net, same as uop_done -- storage is trap_flag_tmr."),
)

# Depth cap for the subtree walk. The registry's own walker uses 28; the audit
# starts two levels higher (at rvv_backend rather than at a module root), so it
# gets the same reach with 30.
MAX_DEPTH = 30


def _def_name(node):
    """Module type of a hierarchy node ('edff', 'rvv_backend_rob'), or ''.

    Best-effort: cocotb exposes vpiDefName for hierarchy handles, but the
    attribute has moved between releases and is absent for non-hierarchy
    handles. The report degrades to paths-only without it, so a miss must not
    be an error."""
    for attr in ("_def_name", "_definition_name"):
        try:
            v = getattr(node, attr)
        except Exception:  # noqa: BLE001 - cocotb internals can raise
            continue
        if isinstance(v, str) and v:
            return v
    return ""


def enumerate_exposed(dut, max_depth=MAX_DEPTH):
    """Every depositable leaf under rvv_backend, as the simulator publishes it.

    Returns [{path, width, name, owner_def}] with `path` in the same form the
    registry produces (full dotted path from `dut`), so the two sets diff
    directly. Leaves are flattened with the registry's own `_flatten_targets`,
    which is what makes an unpacked `mem[8]` come out as eight paths on both
    sides instead of one on one side and eight on the other."""
    root = fi_utils.descend(dut, fi_utils.RVV_BACKEND_PREFIX)
    if root is None:
        raise AssertionError(
            "fi_audit: rvv_backend not reachable at "
            f"{'.'.join(fi_utils.RVV_BACKEND_PREFIX)} -- the audit would "
            "report an empty design, which looks exactly like a design with "
            "no exposed state.")
    out = []
    _walk(root, fi_utils.RVV_BACKEND_PREFIX, out, max_depth, 0)
    return out


def _walk(node, base, out, max_depth, depth):
    """DFS collecting depositable leaves; does not descend past a leaf."""
    if depth > max_depth:
        return
    owner = _def_name(node)
    for name, child in fi_utils._attr_children(node):
        path = base + (name,)
        if fi_utils._is_depositable(child):
            out.append({"path": fi_utils._path_str(path, ()),
                        "width": len(child), "name": name,
                        "owner_def": owner})
            continue
        # Not a flat cell: either an unpacked array of cells (fifo `mem`) or a
        # hierarchy scope. `_flatten_targets` settles the first case; if it
        # yields nothing, recurse as a scope.
        leaves = []
        fi_utils._flatten_targets(child, path, leaves)
        if leaves:
            for t in leaves:
                out.append({"path": t["path"], "width": t["width"],
                            "name": name, "owner_def": owner})
            continue
        _walk(child, path, out, max_depth, depth + 1)
    for idx, child in fi_utils._index_children(node):
        _walk(child, base + (f"[{idx}]",), out, max_depth, depth + 1)


def registry_union(dut, ft_on=None):
    """{path: [module names]} over every registry entry, analysis + diagnostic.

    The union is what decides "unregistered", so it must include the
    diagnostic items: fifo pointers ARE collected by fifo_ptr, and reporting
    them as gaps would bury the real ones."""
    if ft_on is None:
        ft_on = fi_utils.ft_build_is_on(dut)
    owners = collections.defaultdict(list)
    widths = {}
    per_module = {}
    for name in fi_utils.MODULE_NAMES:
        targets = fi_utils.collect_targets(dut, name, ft_on=ft_on)
        per_module[name] = sum(t["width"] for t in targets)
        for t in targets:
            owners[t["path"]].append(name)
            widths[t["path"]] = t["width"]
    return owners, widths, per_module


def _group_key(path):
    """Top-level instance under rvv_backend that a path belongs to.

    Grouping by the first component below the backend is what turns a 300-line
    leaf list into a decision: '250 bits under u_falu' is one question about
    one block, not 250 questions."""
    tail = path.split(".")[len(fi_utils.RVV_BACKEND_PREFIX):]
    return tail[0].split("[")[0] if tail else "<root>"


def audit_fault_space(dut, ft_on=None, log=None):
    """Reconcile exposed silicon against the registry; return a report dict.

    Logs the full report. Raises nothing on gaps -- a gap is a finding to act
    on, not a broken run -- but DOES raise if the hierarchy itself is
    unreachable or if two modules claim the same cell (INV-3)."""
    log = log or cocotb.log
    if ft_on is None:
        ft_on = fi_utils.ft_build_is_on(dut)

    exposed = enumerate_exposed(dut)
    owners, widths, per_module = registry_union(dut, ft_on=ft_on)

    prefix = ".".join(fi_utils.RVV_BACKEND_PREFIX) + "."
    excluded = {prefix + suffix: reason
                for suffix, ft_only, reason in EXCLUDED_BY_DESIGN
                if ft_on or not ft_only}

    injectable = [e for e in exposed if e["name"] not in READ_SIDE_NAMES]
    read_side = [e for e in exposed if e["name"] in READ_SIDE_NAMES]
    registered = [e for e in injectable if e["path"] in owners]
    missing = [e for e in injectable
               if e["path"] not in owners and e["path"] not in excluded]

    exposed_by_path = {e["path"]: e for e in exposed}
    rotten = sorted(p for p in excluded if p not in exposed_by_path)
    claimed = sorted(p for p in excluded if p in owners)

    # Registry paths that no longer exist among the exposed leaves. collect_
    # targets already warns per source, but a source can resolve and still lose
    # individual leaves, and nothing else prints that.
    exposed_paths = {e["path"] for e in exposed}
    stale = sorted(p for p in owners if p not in exposed_paths)
    overlap = {p: m for p, m in owners.items() if len(m) > 1}

    tot_bits = sum(e["width"] for e in injectable)
    reg_bits = sum(e["width"] for e in registered)
    miss_bits = sum(e["width"] for e in missing)

    log.info("=" * 72)
    log.info("fi_audit: fault-space reconciliation (FAULT_TOLERANT_ON=%s)",
             ft_on)
    log.info("  exposed depositable leaves : %d (%d bit)", len(injectable),
             tot_bits)
    log.info("  read-side leaves (e/c)     : %d (excluded by design)",
             len(read_side))
    log.info("  collected by the registry  : %d (%d bit)", len(registered),
             reg_bits)
    log.info("  NOT collected              : %d (%d bit, %.1f%% of exposed)",
             len(missing), miss_bits,
             100.0 * miss_bits / tot_bits if tot_bits else 0.0)

    log.info("-- per-module fault space (the AVF denominators) --")
    for name in fi_utils.MODULE_NAMES:
        kind = "analysis " if fi_utils.MODULES[name].get("analysis", True) \
            else "diagnostic"
        log.info("  %-12s %-10s %6d bit", name, kind, per_module[name])

    if excluded:
        log.info("-- exposed, excluded from the fault space ON PURPOSE --")
        for path in sorted(excluded):
            log.info("  %s (%d bit)", path,
                     exposed_by_path[path]["width"]
                     if path in exposed_by_path else 0)
            log.info("      %s", excluded[path])

    log.info("-- exposed but NOT in any module's fault space --")
    if not missing:
        log.info("  (none)")
    by_group = collections.defaultdict(list)
    for e in missing:
        by_group[_group_key(e["path"])].append(e)
    for grp in sorted(by_group, key=lambda g: -sum(x["width"]
                                                   for x in by_group[g])):
        items = by_group[grp]
        defs = collections.Counter(x["owner_def"] for x in items)
        log.info("  %-16s %4d leaf, %6d bit   [%s]", grp, len(items),
                 sum(x["width"] for x in items),
                 ", ".join(f"{d or '?'}x{n}" for d, n in defs.most_common(4)))
        for e in items[:4]:
            log.info("      e.g. %s  (%d bit)", e["path"], e["width"])
        if len(items) > 4:
            log.info("      ... %d more", len(items) - 4)

    if stale:
        log.error("-- registry paths that are no longer exposed (%d) --",
                  len(stale))
        for p in stale[:20]:
            log.error("    %s", p)

    log.info("=" * 72)

    assert not rotten, (
        "fi_audit: an EXCLUDED_BY_DESIGN entry no longer matches an exposed "
        f"cell ({rotten}). The exclusion now hides nothing, so whatever it "
        "was justifying has changed name -- and the cell it used to cover "
        "would come back as an unreported gap.")
    assert not claimed, (
        "fi_audit: these cells are both excluded by design and collected by "
        f"the registry ({claimed}). One of the two is wrong, and the "
        "registry wins silently.")
    assert not overlap, (
        "fi_audit: INV-3 violated -- these cells are collected by more than "
        f"one module, so their failures are counted twice: {overlap}")
    assert not stale, (
        "fi_audit: registry paths no longer resolve to an exposed cell "
        f"({len(stale)} of them, e.g. {stale[:3]}). An unbound path injects "
        "nothing, and injecting nothing looks exactly like perfect "
        "protection.")

    return {"ft_on": ft_on, "exposed": len(injectable), "exposed_bits":
            tot_bits, "registered": len(registered), "registered_bits":
            reg_bits, "missing": missing, "missing_bits": miss_bits,
            "per_module": per_module}
