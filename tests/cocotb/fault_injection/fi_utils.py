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

"""Primitives for RVV-core module-level fault-injection (FI).

This is the model-agnostic toolbox for the vulnerability-analysis flow. The
RVV core is partitioned into FOUR modules; each is fault-injected as one
self-contained experiment whose injection space is *every* targeted flip-flop
/ SRAM cell inside that module (full-module sampling, not just the last
pipeline stage). The four modules map one-to-one onto the planned fault-
tolerance schemes:

    decode_path   front-end instruction queues  -> (TMR candidate)
    compute_ctrl  ROB control state + trap ff   -> TMR
    execute       all execution-unit datapath/  -> DMR
                  control FFs + RS + result fifo
    storage       vector register file          -> ECC

A cell earns its place in this partition by representing real circuit error
or by proving an FT mechanism works -- not by being a flip-flop. That is why
the ROB data plane (`rob_data`) and the fifo bookkeeping (`fifo_ptr`) are
registry entries but NOT analysis modules: see their `description` fields.

Injection targets are reached through three Verilator `public_flat_rw`
exposures (see rules/default.vlt.tpl):
    edff.q          enable-DFF storage   (execution pipeline regs)
    cdffr.q         clear-DFF storage    (div/falu regs, fifo pointers*)
    multi_fifo.mem  fifo buffer cell     (queues, RS, result fifo)
plus the ROB control regs (entry_valid/uop_done/trap_flag) and the VRF
`vreg`, which carry their own dedicated vlt directives.

  *Partition rule (INV-3):* the injection space is partitioned BY SUB-MODULE,
  not assembled by enumerating registers that looked interesting. Every
  sequential cell belongs to exactly one sub-module, and the sub-module is
  also the AVF denominator -- because the decision this whole flow feeds is
  "which sub-module gets which FT scheme", the denominator has to be the
  decision unit. Corollary: bit counts are NOT comparable across sub-modules
  (a control flop representing a runaway state machine and a buffer cell
  representing only itself do not belong in one weighted ranking).

  The multi_fifo read/write pointers (wptr/rptr/entry_count) are themselves
  cdffr instances. They are fifo-internal bookkeeping, NOT control- or data-
  path state, and are EXCLUDED from every analysis module; the collector
  enforces this by only taking `mem` (never `q`) from inside a multi_fifo
  subtree, and by only walking compute-unit subtrees (which contain no fifo)
  for `q`. They are measured on their own as the `fifo_ptr` diagnostic item.

Three fault models, matching the physical threats the FT schemes defend
against:
    seu    flip one bit once, leave it (persistent_bit_flip)
    set    flip, hold 1 cycle, flip back (transient_bit_flip)
    stuck  force the bit to 0/1 every cycle until run end (permanent_stuck_at)
"""

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge


# Hierarchical prefix from the cocotb `dut` (the `RvvCoreMiniHighmemAxi`
# toplevel, all-Chisel shell) down to the hand-written SV `rvv_backend`.
# Chisel instance names: Core -> CoreAxi.core, RvvCoreShim -> Core.rvvCore,
# RvvCoreWrapper -> RvvCoreShim.rvvCoreWrapper, SV RvvCore -> wrapper.core,
# rvv_backend -> RvvCore.backend.
RVV_BACKEND_PREFIX = ("core", "rvvCore", "rvvCoreWrapper", "core", "backend")


# Fault models, grouped by physical class. `stuck` carries a polarity chosen
# at run time via FI_STUCK_VAL (0/1); the two are not separate models here.
FAULT_TYPES = ("seu", "set", "stuck")


# ---------------------------------------------------------------------------
# Module registry. Each module lists `sources`: a root instance path (under
# the rvv_backend) plus how to collect targets from that subtree.
#
#   walk="q"     walk subtree, collect every edff/cdffr storage cell (`q`).
#   walk="mem"   walk subtree, collect every multi_fifo buffer (`mem`); each
#                buffer is an unpacked array, expanded to its per-entry cells.
#   names=[...]  collect these exact signals at the root (ROB control regs);
#                pointers (uop_wptr/uop_rptr) are deliberately omitted.
#
# The collector flattens multi-dimensional handles and descends through both
# named module/genblock children and indexable arrays (genblock arrays, fifo
# `mem`). Fifo pointers (wptr/rptr/entry_count) are cdffr `q` cells, but they
# live only under multi_fifo subtrees that we reach via `walk="mem"` (which
# matches `mem`, not `q`), so they are never collected -- keeping fifo
# bookkeeping out of every analysis module's fault space (INV-3). The
# `fifo_ptr` diagnostic entry at the end of the registry points `walk="q"` at
# those same fifo subtrees to measure them on their own.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# FT_ON build variant (FAULT_TOLERANT_ON + the Stage-3 ROB control TMR).
#
# Triplicating the ROB's 25 control bits changes their hierarchical names, so
# a registry written against the FT_OFF hierarchy would resolve to NOTHING on
# an FT_ON build -- and an empty fault space produces exactly the result the
# experiment hopes for ("the hardened module shows no vulnerability"). That
# failure mode is the whole reason `space_bits` below is a hard assertion and
# not a warning: a silently unbound path and a working TMR are indistinguishable
# from the outcome table alone.
#
# What changes, per rvv_backend_rob.sv:
#   uop_done   -> uop_done_tmr   [FT_TMR_COPIES][ROB_DEPTH], one packed vector
#   trap_flag  -> trap_flag_tmr  likewise
#   entry_valid-> gen_uop_valid_fifo_tmr[c].u_uop_valid_fifo.mem (3 fifo copies)
#   trap_ready -> gen_trap_ready_tmr[c].trap_ready.q (3 edff copies)
#
# The voted nets keep the ORIGINAL names (uop_done / trap_flag / entry_valid /
# trap_ready_rvv2rvs) so the RTL's readers did not change -- which means those
# names still resolve on an FT_ON build while being pure combinational voter
# outputs. Injecting them would violate INV-1 (a deposit is recomputed away),
# and would also silently measure the voter instead of the storage. We take the
# three copies, never the voted name.
FT_TMR_COPIES = 3

# ROB entry count (`ROB_DEPTH in rvv_backend_define.svh). Used by the hang
# attribution snapshot to walk entries; the fault-space sizes below are
# derived from the RTL independently, so a mismatch here cannot silently
# change any measured number.
ROB_DEPTH = 8

# Fault space accounting, FT_OFF vs FT_ON. 25 protected bits become 75 real
# flip-flops, and 75 is the honest denominator: TMR spends 3x the silicon, and
# every one of those cells is equally likely to be struck. Sampling only one
# copy would quietly assume the redundancy is free.
_COMPUTE_CTRL_BITS_OFF = 25
_COMPUTE_CTRL_BITS_ON = _COMPUTE_CTRL_BITS_OFF * FT_TMR_COPIES


def _ft_valid_fifo_root(copy):
    return ("u_rob", "gen_uop_valid_fifo_tmr", copy, "u_uop_valid_fifo")


def _ft_trap_ready_root(copy):
    return ("u_rob", "gen_trap_ready_tmr", copy, "trap_ready")


# FT_ON source list for compute_ctrl. The two packed *_tmr vectors carry all
# three copies in one handle (bit = copy*ROB_DEPTH + entry); the fifo and edff
# copies live in separate generate scopes and are listed per copy.
#
# `tmr` annotations are what lets the reverse control (see tmr_logical_map)
# find the other two copies of a given logical bit. "packed" means one handle
# holds every copy, `copy*stride + logical_bit`; "copy" means this whole source
# is one copy, and a target's offset within the source IS the logical bit.
_COMPUTE_CTRL_SOURCES_FT = (
    [{"root": ("u_rob",), "names": ["uop_done_tmr", "trap_flag_tmr"],
      "tmr": {"packed": True, "stride": 8}}]
    + [{"root": _ft_valid_fifo_root(c), "walk": "mem",
        "tmr": {"group": "entry_valid", "copy": c}}
       for c in range(FT_TMR_COPIES)]
    + [{"root": _ft_trap_ready_root(c), "names": ["q"],
        "tmr": {"group": "trap_ready", "copy": c}}
       for c in range(FT_TMR_COPIES)]
)

# Same story for the fifo-pointer diagnostic: u_uop_valid_fifo moved into a
# generate scope and got triplicated, so its pointers did too. No bit-count
# assertion here -- this item is diagnostic, its FT_ON size is a measurement
# rather than a known constant -- but the path must still resolve, or the item
# would silently shrink instead of reporting the larger pointer space.
_FIFO_PTR_SOURCES_FT_ROB = [{"root": _ft_valid_fifo_root(c), "walk": "q"}
                            for c in range(FT_TMR_COPIES)]


def ft_build_is_on(dut):
    """True if this build has FAULT_TOLERANT_ON (probed from the hierarchy).

    Probed, not configured by an env var, because the RTL switch is a commented
    `define in rvv_backend_define.svh: an env var would be a second source of
    truth that can disagree with the model actually being simulated, and the
    disagreement would show up as a wrong fault space rather than as an error.
    `uop_done_tmr` exists only inside `ifdef FAULT_TOLERANT_ON`."""
    return descend(dut, RVV_BACKEND_PREFIX + ("u_rob", "uop_done_tmr")) is not None


MODULES = {
    "decode_path": {
        "ft_scheme": "tmr",
        "description": "Front-end instruction decode path: command / legal-"
                       "command / uop queue buffers (three serial stages).",
        "sources": [
            {"root": ("u_command_queue",), "walk": "mem"},
            {"root": ("u_legal_command_queue",), "walk": "mem"},
            {"root": ("u_uop_queue",), "walk": "mem"},
        ],
    },
    "compute_ctrl": {
        "ft_scheme": "tmr",
        "description": "Computation control unit: ROB per-entry control state "
                       "(entry valid / uop_done / trap_flag) plus the trap "
                       "handshake register. Pointers excluded.",
        "sources": [
            {"root": ("u_rob",), "names": ["uop_done", "trap_flag"]},
            # Entry-valid state. NOT `u_rob.entry_valid`: that name is the
            # combinational `fifo_data` output of this fifo, so a deposit onto
            # it is recomputed away before it can do anything -- measured at
            # landed 0/4, survived 0/9 by the deposit probe. The state itself
            # lives in the fifo buffer, which is what we inject.
            {"root": ("u_rob", "u_uop_valid_fifo"), "walk": "mem"},
            # The trap handshake flop (rvv_backend_rob.sv, `edff trap_ready`).
            # One bit, and the single most consequential one in this module: it
            # drives `trap_flush_rvv`, which flushes the whole backend. It has
            # `.e(1'b1)`, so it is write-enabled every cycle -- a `set` upset
            # always lands. It was missing from every module's fault space
            # until Stage 3: the hand-written source list had drifted from the
            # RTL's actual sequential-cell list, which is exactly the drift
            # INV-3's "partition is exhaustive and disjoint" rule now forbids.
            # We take the edff's `q`, not the `trap_ready_rvv2rvs` net it
            # drives (INV-1: the net would be overwritten by the flop output).
            {"root": ("u_rob", "trap_ready"), "names": ["q"]},
        ],
        # FT_ON: the same 25 bits of state, now stored three times. See the
        # FT_TMR block above for the name mapping and for why the fault space
        # is the full 75 rather than one copy.
        "sources_ft": _COMPUTE_CTRL_SOURCES_FT,
        "space_bits": {False: _COMPUTE_CTRL_BITS_OFF,
                       True: _COMPUTE_CTRL_BITS_ON},
    },
    # ---- diagnostic item, NOT part of the analysis matrix (see `analysis`) --
    "rob_data": {
        "ft_scheme": "none",
        "analysis": False,
        "description": "ROB data plane: the result memory and the uop info "
                       "buffer. Kept separate from compute_ctrl because the FT "
                       "split is by semantics, not by physical block: ROB "
                       "control gets TMR, ROB data is deliberately left "
                       "unprotected. Demoted out of the analysis matrix in "
                       "Stage 3 for three independent reasons. (1) No FT scheme "
                       "will ever be decided from its number: an execution "
                       "error that reaches here is already caught upstream by "
                       "DMR's write-back comparison, and ECC here would protect "
                       "only this one register stage -- so the measurement "
                       "answers no question. (2) Residency time: an SEU cross "
                       "section scales as area x time-resident, and these are "
                       "pipeline buffers holding a result for a handful of "
                       "cycles, unlike the architecturally-visible VRF in "
                       "`storage` which holds one for thousands. (3) It is the "
                       "entry that made the Stage 2 bit-count-weighted ranking "
                       "misleading: 2904 bits of short-lived buffer outranked "
                       "24 bits of state-machine control, which inverts the "
                       "real protection priority. Still measurable by name so "
                       "the Stage 2 baseline stays reproducible.",
        "sources": [
            {"root": ("u_rob",), "names": ["res_mem"]},
            {"root": ("u_rob", "u_uop_info_fifo"), "walk": "mem"},
        ],
    },
    "execute": {
        "ft_scheme": "dmr",
        "description": "Execution units: all ALU/PMTRDT/MAC-MUL/DIV/FALU "
                       "pipeline FFs (edff+cdffr) plus their reservation "
                       "stations. Fifo pointers excluded.",
        "sources": [
            {"root": ("u_alu",), "walk": "q"},
            {"root": ("u_pmtrdt",), "walk": "q"},
            {"root": ("u_mulmac",), "walk": "q"},
            {"root": ("u_div",), "walk": "q"},
            {"root": ("u_falu",), "walk": "q"},
            {"root": ("u_alu_rs",), "walk": "mem"},
            {"root": ("u_pmtrdt_rs",), "walk": "mem"},
            {"root": ("u_mul_rs",), "walk": "mem"},
            {"root": ("u_div_rs",), "walk": "mem"},
            {"root": ("u_falu_rs",), "walk": "mem"},
            # Per-unit result fifos (gen_res_ff[i].u_res_ff, PU->arbiter).
            # These were excluded in Stage 1 on a "one-cycle downstream copy of
            # results already covered by the unit `q` cells, so near-redundant"
            # argument. That was wrong on both counts: they are real sequential
            # cells holding a result for an unbounded number of cycles while the
            # arbiter backpressures (so not a copy of anything still live in the
            # unit), and INV-2 sizes the fault space by silicon, not by whether
            # a cell's content correlates with another's. Leaving them out
            # silently shrank the execute fault space by ~18%.
            {"root": ("gen_res_ff",), "walk": "mem"},
        ],
    },
    "storage": {
        "ft_scheme": "ecc",
        "description": "Vector register file storage: the edff `q` cells that "
                       "make up vreg (NUM_VRF x VLEN). We inject the real "
                       "flip-flops, not the `vreg` net they drive (a deposit "
                       "onto that net would be overwritten by the edff output).",
        "sources": [
            {"root": ("u_vrf",), "walk": "q"},
        ],
    },
    # ---- diagnostic item, NOT part of the analysis matrix (see `analysis`) --
    "fifo_ptr": {
        "ft_scheme": "none",
        "analysis": False,
        "description": "Fifo bookkeeping: the write/read pointers and entry "
                       "counters of every multi_fifo (cdffr `q` cells living "
                       "under a fifo instance). INV-3 keeps these out of the "
                       "five analysis modules -- a pointer is not decode, "
                       "control, data, execute or storage state, and folding "
                       "it into any of them would attribute its failures to "
                       "the wrong block. But the silicon flips all the same, "
                       "so it is measured as its own item: this number says "
                       "what fifo bookkeeping is worth, and it is the one "
                       "thing no per-module FT scheme in this project covers.",
        "sources": [
            {"root": ("u_command_queue",), "walk": "q"},
            {"root": ("u_legal_command_queue",), "walk": "q"},
            {"root": ("u_uop_queue",), "walk": "q"},
            {"root": ("u_rob", "u_uop_valid_fifo"), "walk": "q"},
            {"root": ("u_rob", "u_uop_info_fifo"), "walk": "q"},
            {"root": ("u_alu_rs",), "walk": "q"},
            {"root": ("u_pmtrdt_rs",), "walk": "q"},
            {"root": ("u_mul_rs",), "walk": "q"},
            {"root": ("u_div_rs",), "walk": "q"},
            {"root": ("u_falu_rs",), "walk": "q"},
            {"root": ("gen_res_ff",), "walk": "q"},
        ],
        # FT_ON: u_uop_valid_fifo is triplicated inside a generate scope, so
        # its pointer set is too (+2 copies). Every other fifo is untouched.
        "sources_ft": [
            {"root": ("u_command_queue",), "walk": "q"},
            {"root": ("u_legal_command_queue",), "walk": "q"},
            {"root": ("u_uop_queue",), "walk": "q"},
        ] + _FIFO_PTR_SOURCES_FT_ROB + [
            {"root": ("u_rob", "u_uop_info_fifo"), "walk": "q"},
            {"root": ("u_alu_rs",), "walk": "q"},
            {"root": ("u_pmtrdt_rs",), "walk": "q"},
            {"root": ("u_mul_rs",), "walk": "q"},
            {"root": ("u_div_rs",), "walk": "q"},
            {"root": ("u_falu_rs",), "walk": "q"},
            {"root": ("gen_res_ff",), "walk": "q"},
        ],
    },
}

MODULE_NAMES = tuple(MODULES.keys())

# The four sub-modules the vulnerability matrix is defined over. Diagnostic
# entries (rob_data, fifo_ptr) are addressable by name but never swept by
# 'all', so adding one cannot silently change the matrix or the per-module
# numbers.
ANALYSIS_MODULE_NAMES = tuple(
    k for k, m in MODULES.items() if m.get("analysis", True))


def expand_module_spec(name):
    """Resolve FI_MODULE to an ordered list of module keys.

    'all' means the analysis matrix (ANALYSIS_MODULE_NAMES), not literally
    every registry entry; 'every' includes the diagnostic ones too."""
    if name == "all":
        return list(ANALYSIS_MODULE_NAMES)
    if name == "every":
        return list(MODULE_NAMES)
    if name in MODULES:
        return [name]
    raise KeyError(
        f"unknown FI_MODULE '{name}'. Known: {sorted(MODULE_NAMES)}, "
        "'all' (analysis matrix) or 'every'")


# ---------------------------------------------------------------------------
# Hierarchy navigation. cocotb exposes module/genblock children by attribute
# (HierarchyObject) and unpacked-array / genblock-array elements by
# subscription (ArrayObject / HierarchyArrayObject). A packed vector is a
# single flat LogicArrayObject whose len() is its bit width and whose value is
# an int we can XOR. We therefore distinguish:
#   * depositable leaf  -> len() works AND int(value) works  -> one target
#   * indexable scope    -> iterate elements (attr children + integer indices)
# ---------------------------------------------------------------------------
def _force_discovery(node):
    try:
        for _ in dir(node):
            pass
    except Exception:  # noqa: BLE001 - cocotb internals can raise
        pass


def _attr_children(node):
    """[(name, handle)] of attribute (named) sub-handles."""
    _force_discovery(node)
    try:
        return list(node._sub_handles.items())
    except Exception:  # noqa: BLE001
        return []


def _index_children(node):
    """[(idx, handle)] of an indexable handle (unpacked array / genblock array),
    or [] if it is not integer-indexable."""
    try:
        rng = list(node.range)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for i in rng:
        try:
            out.append((i, node[i]))
        except Exception:  # noqa: BLE001
            pass
    return out


def _is_depositable(handle):
    """True if `handle` is a flat cell we can read as int and XOR a bit into.
    Such a handle has a bit length and an int-castable value."""
    try:
        n = len(handle)
    except Exception:  # noqa: BLE001
        return False
    if not n:
        return False
    try:
        int(handle.value)
    except Exception:  # noqa: BLE001
        return False
    return True


def descend(dut, path):
    """Walk `dut` along `path` (strings = attr, ints = index). None if missing."""
    node = dut
    for step in path:
        try:
            node = node[step] if isinstance(step, int) else getattr(node, step)
        except Exception:  # noqa: BLE001
            return None
        if node is None:
            return None
    return node


def _path_str(prefix, suffix):
    return ".".join(str(s) for s in (tuple(prefix) + tuple(suffix)))


def _flatten_targets(handle, base_path, out, owner=None, kind=None):
    """Expand a (possibly multi-dimensional) handle into depositable leaves.

    A flat packed vector is taken as one target. An unpacked array (e.g. fifo
    `mem[DEPTH]` of packed entries) is recursed per index so a uniform bit pick
    spans every entry. Hierarchy scopes are not flattened here (the walker
    handles those).

    `owner` is the instance scope the matched variable lives in (the edff /
    cdffr / multi_fifo instance) and `kind` is how it was found. Neither
    affects target selection; they let the deposit positive control read the
    cell's write enable and report per exposure mechanism."""
    if _is_depositable(handle):
        out.append({"handle": handle, "width": len(handle),
                    "path": _path_str(base_path, ()),
                    "owner": owner, "kind": kind})
        return
    for idx, child in _index_children(handle):
        _flatten_targets(child, base_path + (f"[{idx}]",), out,
                         owner=owner, kind=kind)


def _walk_collect(node, want, base_path, out, max_depth=28, _depth=0):
    """DFS a subtree collecting every sub-handle named `want` ('q' or 'mem').

    Descends through both module/genblock attribute children and indexable
    arrays (genblock arrays such as gen_res_ff[i]). A matched handle is
    flattened to depositable leaves; we do not descend past a match."""
    if _depth > max_depth:
        return
    for name, child in _attr_children(node):
        if name == want:
            _flatten_targets(child, base_path + (name,), out,
                             owner=node, kind=want)
            continue
        _walk_collect(child, want, base_path + (name,), out,
                      max_depth=max_depth, _depth=_depth + 1)
    # Indexable scopes (genblock arrays) carry their instances by index, not
    # as named attributes; recurse into each element too.
    for idx, child in _index_children(node):
        _walk_collect(child, want, base_path + (f"[{idx}]",), out,
                      max_depth=max_depth, _depth=_depth + 1)


def collect_targets(dut, module_name, ft_on=None):
    """Build the flat list of depositable targets for one module.

    Returns a list of dicts {handle, width, path}. The campaign treats the
    concatenation of all widths as the module's fault space and picks a global
    bit uniformly across it.

    On an FT_ON build a module with a `sources_ft` entry uses that list
    instead, because triplication renamed its cells. If the module also
    declares `space_bits`, the resulting fault space is asserted against the
    expected count for this build -- a hard failure, deliberately. The
    signature of a registry path that no longer resolves is an empty or short
    fault space, which for a hardened module yields a flawless-looking result
    table; nothing downstream can tell that apart from the TMR working."""
    spec = MODULES[module_name]
    if ft_on is None:
        ft_on = ft_build_is_on(dut)
    sources = spec["sources_ft"] if (ft_on and "sources_ft" in spec) \
        else spec["sources"]
    targets = []
    for src in sources:
        base = RVV_BACKEND_PREFIX + src["root"]
        root = descend(dut, base)
        if root is None:
            # Diagnostic: list the parent scope's children so a missing
            # genblock / mangled instance name can be identified in one run.
            parent = descend(dut, base[:-1])
            avail = ([n for n, _ in _attr_children(parent)]
                     if parent is not None else "<parent unresolved>")
            cocotb.log.warning(
                "fi: module '%s' root %s not visible, skipping source. "
                "Parent children: %s",
                module_name, ".".join(str(s) for s in base), avail)
            continue
        first = len(targets)
        if "names" in src:
            for nm in src["names"]:
                h = getattr(root, nm, None)
                if h is not None and _is_depositable(h):
                    targets.append({"handle": h, "width": len(h),
                                    "path": _path_str(base, (nm,)),
                                    "owner": root, "kind": "name"})
                else:
                    cocotb.log.warning(
                        "fi: module '%s' signal %s.%s missing/not depositable",
                        module_name, ".".join(str(s) for s in base), nm)
        else:  # "walk": "q" | "mem"
            _walk_collect(root, src["walk"], base, targets)
        # Carry the source's TMR annotation onto the targets it produced, plus
        # each target's bit offset within the source, which is what identifies
        # the logical bit across copies.
        if "tmr" in src:
            off = 0
            for t in targets[first:]:
                t["tmr"] = dict(src["tmr"], src_offset=off)
                off += t["width"]

    expect = spec.get("space_bits", {}).get(bool(ft_on))
    if expect is not None:
        got = sum(t["width"] for t in targets)
        assert got == expect, (
            f"fi: module '{module_name}' fault space is {got} bit, expected "
            f"{expect} for a FAULT_TOLERANT_ON={bool(ft_on)} build. A registry "
            "path stopped resolving (renamed cell / new generate scope / "
            "missing vlt exposure). This is fatal by design: an unbound path "
            "injects nothing, and 'nothing was injected' is indistinguishable "
            "from 'every fault was corrected' in the outcome table.")
    return targets


# ---------------------------------------------------------------------------
# Reverse control for a TMR-hardened module (FI_TMR_DEFEAT).
#
# The positive control (probe_deposit) proves a deposit reaches a cell. It
# CANNOT prove the cell it reached is the right one. Suppose the FT_ON registry
# were mis-bound to the voted net `uop_done` instead of `uop_done_tmr`: the
# probe would report landed=100%, survived=0% -- which is exactly the signature
# of a correct deposit onto a real flop that the design then rewrote (and the
# scrub does rewrite these every cycle). Both stories end in a campaign of 100%
# MASKED, and no amount of positive control separates them.
#
# What does separate them is attacking the mechanism instead of the cell: flip
# the SAME logical bit in TWO copies at once. Two-of-three is now wrong, so a
# correctly-bound injector MUST produce non-MASKED outcomes; a mis-bound one
# still produces none. This mirrors the RTL-side self-test, where widening the
# sweep from one copy to two turned the regression from PASS to FAIL and thereby
# proved the single-copy PASS meant something.
#
# It is a control, not a fault model: defeating a scheme designed for single
# faults says nothing about vulnerability, so these runs never enter the
# matrix or the baseline CSVs.
# ---------------------------------------------------------------------------
def tmr_logical_map(targets):
    """Group a module's targets into logical bits -> [(target, local_bit)].

    Returns a list of groups, each holding the FT_TMR_COPIES physical cells
    that store one logical bit. Groups that do not come out at full width are
    dropped (and reported by the caller): a partial group means the annotation
    no longer matches the RTL, and injecting a partial group would defeat
    nothing while looking like it did."""
    groups = {}
    for t in targets:
        ann = t.get("tmr")
        if ann is None:
            continue
        for local in range(t["width"]):
            if ann.get("packed"):
                # One handle holds all copies of one register: within THIS
                # target (not the source -- uop_done_tmr and trap_flag_tmr are
                # two such targets), bit = copy*stride + logical.
                copy, logical = divmod(local, ann["stride"])
                key = (t["path"], logical)
            else:
                # One source per copy: position within the source is the
                # logical bit, and the source knows which copy it is.
                copy = ann["copy"]
                key = (ann["group"], ann["src_offset"] + local)
            groups.setdefault(key, {})[copy] = (t, local)
    out = []
    for key, by_copy in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if len(by_copy) == FT_TMR_COPIES:
            out.append([by_copy[c] for c in sorted(by_copy)])
    return out


def dump_hierarchy(node, max_depth=4, _depth=0, _prefix=""):
    """Recursively log the cocotb-visible sub-handle tree (path bring-up aid)."""
    name_self = getattr(node, "_name", repr(node))
    cocotb.log.info("%s%s  [%s]", _prefix, name_self, type(node).__name__)
    if _depth >= max_depth:
        return
    children = _attr_children(node) + [(f"[{i}]", c)
                                       for i, c in _index_children(node)]
    for child_name, child in children:
        try:
            dump_hierarchy(child, max_depth=max_depth, _depth=_depth + 1,
                           _prefix=_prefix + "  ")
        except Exception as e:  # noqa: BLE001
            cocotb.log.info("%s  <error descending %s: %s>",
                            _prefix, child_name, e)


# ---------------------------------------------------------------------------
# Bit-flip primitives. ALL THREE observe and deposit on the FALLING edge.
#
# This is not a style choice, it is the only correct point. Every primitive
# here performs a read-modify-write of the WHOLE handle -- VPI has no
# single-bit deposit, so injecting one bit means writing back all the others
# unchanged. That is harmless only if the value read is the one the design
# has settled on. A read at the RISING edge returns the PRE-edge value,
# because the NBA update for that cycle has not been applied yet, so the
# write-back reimposes the previous cycle's value on every other bit of the
# handle -- undoing the design's own update.
#
# On a packed multi-bit register that is a whole-register rollback, not a
# single-cell fault. It was measured directly (fi_tmr_diag, FT_ON build,
# `uop_done_tmr`, 24 bit): rising-edge reads were stale on 75152/75152 update
# cycles, and replaying the read-modify-write with an EMPTY mask -- flipping
# nothing at all -- still drove the register's update rate from 25.34% to
# 0.00%. Under the old rising-edge `stuck`, every TMR copy was frozen at once,
# which no voter can correct; the resulting 12/12 DUE-hang measured the
# injector rather than the design.
#
# `transient_bit_flip` was already written this way (it had to be, to align to
# a write edge); the reasoning applies just as much to the other two.
# ---------------------------------------------------------------------------
async def persistent_bit_flip(clock, signal, bit_index):
    """SEU: flip one bit once and leave it. The cell keeps the flipped value
    until the design naturally overwrites it (next write / scrub / reset).
    Canonical SEU model for flip-flops and SRAM bit cells.

    Falling edge, so the read-modify-write cannot roll the rest of the handle
    back to its pre-edge value (see the section comment above)."""
    await FallingEdge(clock)
    width = len(signal)
    if not 0 <= bit_index < width:
        raise IndexError(f"bit_index {bit_index} out of range [0,{width})")
    cur = int(signal.value)
    signal.value = cur ^ (1 << bit_index)


async def persistent_multi_flip(clock, cells):
    """SEU on several bits at once, correct even when they share a handle.

    `cells` is [(signal, bit_index), ...]. Two concurrent persistent_bit_flip
    tasks on the SAME handle would each read-modify-write the whole register at
    the same edge, so the second write overwrites the first and only one bit
    ends up flipped. That is exactly the situation the TMR reverse control is
    in: with `uop_done_tmr` all three copies live in ONE packed handle, and a
    silently-single strike would leave the majority intact and 'prove' the
    scheme works. Grouping the bits per handle makes it one masked write.
    """
    await FallingEdge(clock)
    per_handle = {}
    for signal, bit_index in cells:
        if not 0 <= bit_index < len(signal):
            raise IndexError(
                f"bit_index {bit_index} out of range [0,{len(signal)})")
        key = id(signal)
        h, m = per_handle.get(key, (signal, 0))
        per_handle[key] = (h, m | (1 << bit_index))
    for signal, mask in per_handle.values():
        signal.value = int(signal.value) ^ mask


async def permanent_multi_stuck_at(clock, cells, value):
    """Stuck-at on several bits at once, correct when they share a handle.

    Same rationale as persistent_multi_flip, but re-forced every cycle."""
    if value not in (0, 1):
        raise ValueError(f"stuck value must be 0 or 1, got {value!r}")
    per_handle = {}
    for signal, bit_index in cells:
        if not 0 <= bit_index < len(signal):
            raise IndexError(
                f"bit_index {bit_index} out of range [0,{len(signal)})")
        key = id(signal)
        h, m = per_handle.get(key, (signal, 0))
        per_handle[key] = (h, m | (1 << bit_index))
    while True:
        await FallingEdge(clock)
        for signal, mask in per_handle.values():
            try:
                cur = int(signal.value)
            except Exception:  # noqa: BLE001 - X during reset
                continue
            new = (cur | mask) if value else (cur & ~mask)
            if new != cur:
                signal.value = new


async def transient_bit_flip(clock, signal, bit_index, e_handle=None,
                             timeout_cycles=None, state=None):
    """SET: corrupt the value the cell captures at its next write.

    A combinational upset does not live in a flip-flop -- it lives in the cone
    feeding one, and its only lasting effect is that the flop latches a wrong
    value at ITS sampling moment. So the injection has to be aligned to a
    write, not to a random cycle. The previous model (flip at a random edge,
    flip back one cycle later) landed on an idle cell 98.5% of the time (the
    deposit probe measured a write-enable duty cycle under 1.5%), where it was
    erased with no lasting effect -- it modelled a transient ON the flop, and a
    mostly inert one, rather than a SET upstream of it.

    A write is detected exactly via `e` when the cell exposes one (edff/cdffr),
    and otherwise by observing the stored value change across the edge, which
    covers multi_fifo buffers and bare registers uniformly. A write that stores
    the same value is invisible to the fallback; it is also a write whose
    captured value we could not corrupt observably, so nothing is lost.

    Everything is observed and deposited at the FALLING edge, never at the
    rising one. Reading at the rising edge returns the pre-edge value (the NBA
    update has not settled yet), so a rising-edge comparison never sees a write
    and a rising-edge deposit races the design's own write. Mid-cycle both are
    unambiguous: `e` is settled for the coming edge, and the value read one
    falling edge later is what the cell actually captured.

    If the cell is never written before `timeout_cycles` elapse, NOTHING is
    injected. That is reported through `state["fired"]` rather than silently
    counted as a masked run: a fault that was never expressed is not evidence
    of tolerance."""
    width = len(signal)
    if not 0 <= bit_index < width:
        raise IndexError(f"bit_index {bit_index} out of range [0,{width})")
    mask = 1 << bit_index
    if state is not None:
        state["fired"] = False

    def _read():
        try:
            return int(signal.value)
        except Exception:  # noqa: BLE001 - X during reset
            return None

    waited = 0
    prev = None
    while timeout_cycles is None or waited < timeout_cycles:
        await FallingEdge(clock)  # mid-cycle: `e` is settled, value has settled
        waited += 1
        cur = _read()
        enabled = None
        if e_handle is not None:
            try:
                enabled = bool(int(e_handle.value))
            except Exception:  # noqa: BLE001
                enabled = None
        if enabled is None:
            # Fallback: a write shows up as the stored value changing between
            # two consecutive mid-cycles.
            written = prev is not None and cur is not None and cur != prev
            prev = cur
            if not written:
                continue
        elif enabled:
            # `e` is high for the edge that is COMING, so wait one more
            # mid-cycle: by then the capture has happened and we can corrupt
            # exactly what it captured.
            await FallingEdge(clock)
            waited += 1
            cur = _read()
            prev = cur
        else:
            prev = cur
            continue
        # We are past the capture edge: what the cell holds now is what it
        # latched, so flipping a bit of it IS the mis-sampled value.
        if cur is None:
            continue
        try:
            signal.value = cur ^ mask
        except Exception:  # noqa: BLE001
            return
        if state is not None:
            state["fired"] = True
        return


async def permanent_stuck_at(clock, signal, bit_index, value):
    """Hard fault: force the bit to `value` every cycle until cancelled. The
    design's writes are observed but immediately overwritten, simulating a
    broken cell. Spawn with cocotb.start_soon and kill at end of run.

    Falling edge, and this one matters most: it re-forces the bit on EVERY
    cycle, so a rising-edge read-modify-write would reimpose a stale value on
    the rest of the handle every cycle for the whole run -- freezing the
    entire register rather than breaking one cell (see the section comment
    above for the measurement)."""
    if value not in (0, 1):
        raise ValueError(f"stuck value must be 0 or 1, got {value!r}")
    width = len(signal)
    if not 0 <= bit_index < width:
        raise IndexError(f"bit_index {bit_index} out of range [0,{width})")
    mask = 1 << bit_index
    while True:
        await FallingEdge(clock)
        try:
            cur = int(signal.value)
        except Exception:  # noqa: BLE001 - X during reset; retry next cycle
            continue
        new = (cur | mask) if value else (cur & ~mask)
        if new != cur:
            signal.value = new


# ---------------------------------------------------------------------------
# Deposit positive control. NOT a fault model -- this is a self-test of the
# injection mechanism (see README "positive control"). It exists because a
# silently-failing injector and a genuinely well-masked design produce the
# exact same campaign CSV: all MASKED. The probe separates two questions that
# have two different fixes:
#
#   landed    did the VPI deposit reach the cell at all?
#             False -> vlt exposure / handle / width layer is broken.
#   survived  is the flipped value still there one rising edge later?
#             False with e=1 -> physically CORRECT, the design wrote the cell.
#             False with e=0 -> something re-drives the cell every eval; the
#                               injector cannot express a persistent upset.
#
# Hence `e`/`c` are sampled at the edge that decides survival, and both must
# be reported next to the rate or the rate cannot be interpreted.
# ---------------------------------------------------------------------------
def read_int(handle):
    """int(handle.value) or None (missing handle / X bits). Never raises."""
    if handle is None:
        return None
    try:
        return int(handle.value)
    except Exception:  # noqa: BLE001
        return None


def enable_handles(target):
    """(e, c) handles of a target's owning cell, or (None, None).

    edff has `e` only, cdffr has `e` and `c`, multi_fifo.mem and the bare ROB
    registers have neither. Requires the read-side vlt exposure; a None here
    means the directive did not take, which is itself worth reporting."""
    owner = target.get("owner")
    if owner is None:
        return None, None
    out = []
    for nm in ("e", "c"):
        try:
            out.append(getattr(owner, nm, None))
        except Exception:  # noqa: BLE001 - cocotb raises on unknown children
            out.append(None)
    return out[0], out[1]


def target_class(target):
    """Coarse exposure mechanism of a target -- the axis the positive control
    reports on, because the suspicion is per-mechanism (Verilator inlines the
    edff / cdffr / multi_fifo wrappers; the bare ROB registers it does not).

    edff vs cdffr is told apart by the presence of a `c` port, not by name:
    both expose their cell as `q`."""
    kind = target.get("kind")
    if kind == "mem":
        return "multi_fifo.mem"
    if kind == "name":
        return "rob.reg"
    _, c = enable_handles(target)
    return "cdffr.q" if c is not None else "edff.q"


async def probe_deposit(clock, signal, bit_index, *, phase="pose",
                        e_handle=None, c_handle=None, restore=True):
    """Deposit one bit flip, read it back twice, put it back. Returns a dict.

    Reproduces the write performed by persistent_bit_flip / transient_bit_flip
    bit-for-bit, including the injection phase, so that it is a valid control
    for them. `phase` selects where in the cycle the deposit happens:

      "nege"  right after FallingEdge  -- identical to the production
                                          primitives, all of which now strike
                                          mid-cycle
      "pose"  right after RisingEdge   -- the OLD production phase, kept only
                                          as a diagnostic. A read here returns
                                          the pre-edge value, so the whole-
                                          handle write-back rolls every other
                                          bit of the register back one cycle
                                          (see the bit-flip primitives section
                                          comment). Do not use it as the
                                          control for a campaign.

    Read-back uses clock edges only (no ReadOnly / delta assumptions): the
    falling edge is a point where the deposit has certainly been applied and
    no flip-flop update can have intervened. For "nege" that point is already
    behind us, so `landed` is not measurable there and comes back None --
    `pose` measures it, and both phases share the same write mechanism.

    `e`/`c` are sampled at the falling edge preceding the rising edge that
    decides survival, i.e. the values the cell will actually act on."""
    mask = 1 << bit_index
    width = len(signal)
    if not 0 <= bit_index < width:
        raise IndexError(f"bit_index {bit_index} out of range [0,{width})")

    if phase == "pose":
        await RisingEdge(clock)
    elif phase == "nege":
        await FallingEdge(clock)
    else:
        raise ValueError(f"phase must be 'pose' or 'nege', got {phase!r}")

    pre = read_int(signal)
    if pre is None:  # X this cycle (reset / uninitialised); nothing to measure
        return {"phase": phase, "pre": None, "mid": None, "post": None,
                "e": None, "c": None, "landed": None, "survived": None}
    signal.value = pre ^ mask

    mid = None
    landed = None
    if phase == "pose":
        await FallingEdge(clock)
        mid = read_int(signal)
        landed = None if mid is None else bool((mid ^ pre) & mask)

    e_val = read_int(e_handle)
    c_val = read_int(c_handle)

    await FallingEdge(clock)  # crosses exactly one rising edge
    post = read_int(signal)
    survived = None if post is None else bool((post ^ pre) & mask)

    # Undo, so ~200 probes can share one simulation without the design
    # drifting arbitrarily far from the golden trajectory.
    if restore and survived:
        signal.value = post ^ mask

    return {"phase": phase, "pre": pre, "mid": mid, "post": post,
            "e": e_val, "c": c_val, "landed": landed, "survived": survived}


# ---------------------------------------------------------------------------
# Outcome taxonomy: three layers, six fine buckets, ZERO thresholds.
#
#   layer MASKED : output bit-exact to golden                  -> no protection
#   layer SDC    : halt ok, status 0, output differs silently  -> ECC/DMR/TMR
#       SDC-benign    differs but top-1 (argmax) unchanged  (NN-tolerated HW error)
#       SDC-critical  differs and top-1 changed             (silent + decision flip)
#   layer DUE    : something signalled / failed to finish      -> recovery
#       DUE-hang      run timed out (io_halted never asserted)
#       DUE-crash     io_fault asserted (scalar-core exception path)
#       DUE-detected  halted but app inference_status != 0 (software self-check)
#
# All three DUE buckets are real coralnpu paths (verified, not assumed):
#   crash    -> Core.scala: io.fault := score.io.fault
#   detected -> run_<model>.cc sets inference_status nonzero on failure
#   hang     -> control-state upset deadlocks the pipeline; io_halted stalls
# ---------------------------------------------------------------------------
OUTCOMES = ("MASKED", "SDC-benign", "SDC-critical",
            "DUE-hang", "DUE-crash", "DUE-detected")
LAYER_OF = {
    "MASKED": "MASKED",
    "SDC-benign": "SDC", "SDC-critical": "SDC",
    "DUE-hang": "DUE", "DUE-crash": "DUE", "DUE-detected": "DUE",
}
LAYERS = ("MASKED", "SDC", "DUE")


def classify_outcome(*, hung, faulted, status, output_bitexact, argmax_match):
    """Map a run to one of the six buckets. Threshold-free: MASKED requires a
    bit-exact match, SDC is split purely by argmax. Order matters — DUE
    conditions are checked before output comparison because a hung/faulted run
    has no trustworthy output."""
    if hung:
        return "DUE-hang"
    if faulted:
        return "DUE-crash"
    if status != 0:
        return "DUE-detected"
    if output_bitexact:
        return "MASKED"
    return "SDC-benign" if argmax_match else "SDC-critical"


# ---------------------------------------------------------------------------
# Hang attribution: is a DUE-hang an OMISSION, or something else?
#
# `DUE-hang` only says the core stopped making progress. That is not enough to
# price a watchdog, because a watchdog can only recover ONE of the ways a core
# hangs:
#
#   omission  a uop was issued and its result never came back. The ROB entry
#             sits valid-but-not-done forever while the pipeline drains around
#             it. This is exactly what a per-entry timeout detects, and (for a
#             DMR-whitelisted uop) exactly what a re-issue can repair.
#   deadlock  the result did come back, but a handshake downstream is wedged
#             (RS/arbiter backpressure loop). Re-issuing adds traffic to a
#             blocked path; a watchdog makes this worse, not better.
#   external  the RVV backend is idle and the scalar core is what stopped.
#             Out of scope entirely.
#
# Reading DUE-hang as if it were all omission is the mistake this snapshot
# exists to prevent: it would credit a watchdog with recovering hangs no
# watchdog can touch.
#
# Physical vs program order: `uop_done` and the valid fifo's `mem` are BOTH
# physical-entry indexed (rvv_backend_rob.sv aligns res_mem/uop_done/trap_flag
# with the fifo storage), so pairing them needs no rptr windowing. `uop_info`'s
# fifo is windowed by rptr for its READERS, but we read its raw `mem` too and
# index it physically, keeping every field of one entry consistent.
# ---------------------------------------------------------------------------
_ROB = RVV_BACKEND_PREFIX + ("u_rob",)


def _read_int(dut, *paths):
    """First of `paths` that resolves to an int-castable handle, else None.

    Several of the values this snapshot wants (`entry_count`, `wptr`, `rptr`)
    are multi_fifo OUTPUT NETS, and the vlt template only exposes `multi_fifo
    -var mem` plus the DFF wrappers' `q` -- so the net name may not exist over
    VPI at all. The register behind it always does (multi_fifo.sv drives each
    from a cdffr: u_entry_count_reg / u_wptr_reg / u_rptr_reg), so we try the
    net first and fall back to the cell. Returning None on both is reported as
    `unknown`, never guessed around."""
    for path in paths:
        h = descend(dut, path)
        if h is None:
            continue
        try:
            return int(h.value)
        except Exception:  # noqa: BLE001
            continue
    return None


def _fifo_count(dut, root):
    """Occupancy of the multi_fifo at `root` (net, else its cdffr)."""
    return _read_int(dut, root + ("entry_count",),
                     root + ("u_entry_count_reg", "q"))


def _rob_ptrs(dut):
    """(wptr, rptr) of the ROB, which are u_uop_info_fifo's pointers."""
    fifo = _ROB + ("u_uop_info_fifo",)
    return (_read_int(dut, _ROB + ("uop_wptr",), fifo + ("u_wptr_reg", "q")),
            _read_int(dut, _ROB + ("uop_rptr",), fifo + ("u_rptr_reg", "q")))


def _rob_entry_valid(dut):
    """Physical-order entry_valid, read from the valid fifo's storage.

    Not from the `entry_valid` net: on an FT_ON build that name is the majority
    voter's combinational output, and on both builds it is the fifo's
    rptr-windowed `fifo_data` view -- i.e. program order, which would not line
    up with uop_done. `mem` is the storage itself, in physical order.
    On FT_ON the fifo is triplicated; copy 0 is representative (if the copies
    disagreed the voter would have corrected it, and a hang snapshot is not the
    place to re-audit TMR)."""
    for root in (_ROB + ("gen_uop_valid_fifo_tmr", 0, "u_uop_valid_fifo"),
                 _ROB + ("u_uop_valid_fifo",)):
        mem = descend(dut, root + ("mem",))
        if mem is None:
            continue
        out = []
        for e in range(ROB_DEPTH):
            try:
                out.append(int(mem[e].value) & 1)
            except Exception:  # noqa: BLE001
                return None
        return out
    return None


def _rob_uop_done(dut):
    """Physical-order uop_done. On FT_ON read the voted value the way the
    design does: majority of the three stored copies."""
    tmr = descend(dut, _ROB + ("uop_done_tmr",))
    if tmr is not None:
        try:
            c = [int(tmr[i].value) for i in range(FT_TMR_COPIES)]
            voted = (c[0] & c[1]) | (c[1] & c[2]) | (c[0] & c[2])
        except Exception:  # noqa: BLE001
            return None
    else:
        voted = _read_int(dut, _ROB + ("uop_done",))
        if voted is None:
            return None
    return [(voted >> e) & 1 for e in range(ROB_DEPTH)]


def _rob_stuck_entries(dut):
    """Physical entries that are valid but not done -- the omission signature."""
    ev = _rob_entry_valid(dut)
    dn = _rob_uop_done(dut)
    if ev is None or dn is None:
        return None
    return [e for e in range(ROB_DEPTH) if ev[e] and not dn[e]]


def _entry_is_ft(dut, entry):
    """(is_ft, ft_unit) of the uop occupying physical `entry`, or (None, None).

    Only present on an FT_ON build (the fields are inside `ifdef
    FAULT_TOLERANT_ON`). ft_flag/ft_unit in the ROB are already physical-order
    mirrors of the fifo contents, which is what we want."""
    flag = _read_int(dut, _ROB + ("ft_flag",))
    if flag is None:
        return None, None
    unit = _read_int(dut, _ROB + ("ft_unit",))  # noqa: E501
    u = None if unit is None else (unit >> (2 * entry)) & 0b11
    return (flag >> entry) & 1, u


def _rob_snap(dut):
    done = _rob_uop_done(dut)
    ev = _rob_entry_valid(dut)
    return (_rob_ptrs(dut),
            None if done is None else tuple(done),
            None if ev is None else tuple(ev))


async def _is_progressing(dut, clock, cycles=2000):
    """True if ROB state changes at all over `cycles`.

    A hang where the ROB keeps churning is not an omission on any single entry;
    it is the pipeline thrashing (or the scalar core looping) with the backend
    still alive."""
    first = _rob_snap(dut)
    for _ in range(cycles // 100):
        await ClockCycles(clock, 100)
        if _rob_snap(dut) != first:
            return True
    return False


# Reservation stations, in ft_unit order (0=ALU, 1=MUL/MAC, 2=DIV, 3=FALU).
# Their occupancy is what separates the two ways a ROB entry can sit
# valid-but-not-done, which is the distinction the whole watchdog question
# turns on:
#
#   RS empty     the uop already left the queue and is INSIDE an execution
#                unit that never produced a result. This is the execution-unit
#                omission a per-entry watchdog is meant to catch, and the only
#                case where re-issuing the uop is the right response.
#   RS occupied  the uop (or its predecessors) never got dispatched -- the
#                queue itself is wedged, upstream of the unit. A watchdog would
#                re-issue into a blocked queue.
#
# Collapsing these two into one "omission" number would inflate the watchdog's
# apparent coverage with hangs it cannot fix.
_RS_ROOTS = (("u_alu_rs",), ("u_mul_rs",), ("u_div_rs",), ("u_falu_rs",))


def _rs_occupancy(dut):
    """[entry_count per RS] in ft_unit order; None entries for absent units
    (FALU only exists on a ZVE32F_ON build)."""
    out = []
    for root in _RS_ROOTS:
        out.append(_fifo_count(dut, RVV_BACKEND_PREFIX + root))
    return out


async def hang_snapshot(dut, clock):
    """Classify a hung run. Call while the sim is still up, right after the
    timeout fires and before anything is torn down.

    Returns a dict with `hang_class` in {omission, starvation, deadlock,
    external, progressing, unknown} plus the evidence it was decided on, so a
    surprising classification can be re-read from the CSV instead of re-run."""
    out = {"hang_class": "unknown", "stuck_entries": "", "stuck_n": 0,
           "stuck_units": "", "stuck_is_ft": "", "rob_busy": "",
           "rob_wptr": "", "rob_rptr": "", "rs_occupancy": ""}
    stuck = _rob_stuck_entries(dut)
    if stuck is None:
        # Could not read the ROB at all -- report that, never guess.
        return out
    ev = _rob_entry_valid(dut)
    out["rob_busy"] = int(any(ev))
    out["rob_wptr"], out["rob_rptr"] = _rob_ptrs(dut)
    out["stuck_entries"] = " ".join(str(e) for e in stuck)
    out["stuck_n"] = len(stuck)

    if not any(ev):
        # Backend fully drained: whatever stopped, it was not an RVV uop.
        out["hang_class"] = "external"
        return out
    if await _is_progressing(dut, clock):
        out["hang_class"] = "progressing"
        return out
    if not stuck:
        # Entries are valid and every one of them is DONE, yet nothing retires:
        # results arrived, the drain is what is wedged.
        out["hang_class"] = "deadlock"
        return out

    fts, units = [], []
    for e in stuck:
        f, u = _entry_is_ft(dut, e)
        fts.append("?" if f is None else str(f))
        units.append("?" if u is None else str(u))
    out["stuck_is_ft"] = " ".join(fts)
    out["stuck_units"] = " ".join(units)

    occ = _rs_occupancy(dut)
    out["rs_occupancy"] = " ".join("?" if o is None else str(o) for o in occ)
    # Anything still queued means the uops have not all reached a unit yet, so
    # the stall is at or before dispatch rather than inside an execution unit.
    if any(o for o in occ if o is not None):
        out["hang_class"] = "starvation"
    else:
        out["hang_class"] = "omission"
    return out
