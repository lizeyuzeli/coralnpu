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
    compute_ctrl  ROB control state             -> TMR
    execute       all execution-unit datapath/  -> DMR
                  control FFs + RS + result fifo
    storage       vector register file          -> ECC

Injection targets are reached through three Verilator `public_flat_rw`
exposures (see rules/default.vlt.tpl):
    edff.q          enable-DFF storage   (execution pipeline regs)
    cdffr.q         clear-DFF storage    (div/falu regs, fifo pointers*)
    multi_fifo.mem  fifo buffer cell     (queues, RS, result fifo)
plus the ROB control regs (entry_valid/uop_done/trap_flag) and the VRF
`vreg`, which carry their own dedicated vlt directives.

  *Important isolation rule:* the multi_fifo read/write pointers (wptr/rptr/
  entry_count) are themselves cdffr instances. They are fifo-internal
  bookkeeping, NOT control- or data-path state, and are EXCLUDED from every
  module. The collector enforces this by only taking `mem` (never `q`) from
  inside a multi_fifo subtree, and by only walking compute-unit subtrees
  (which contain no fifo) for `q`.

Three fault models, matching the physical threats the FT schemes defend
against:
    seu    flip one bit once, leave it (persistent_bit_flip)
    set    flip, hold 1 cycle, flip back (transient_bit_flip)
    stuck  force the bit to 0/1 every cycle until run end (permanent_stuck_at)
"""

import cocotb
from cocotb.triggers import RisingEdge


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
# bookkeeping out of every module's fault space.
# ---------------------------------------------------------------------------
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
                       "(entry_valid / uop_done / trap_flag). Pointers excluded.",
        "sources": [
            {"root": ("u_rob",),
             "names": ["entry_valid", "uop_done", "trap_flag"]},
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
            # NOTE: the result fifo (u_res_ff in the gen_res_ff generate block)
            # is intentionally NOT a target. Verilator inlines that genblock so
            # it has no clean scope, and more importantly its contents are a
            # one-cycle downstream copy of the execution-unit results already
            # covered by the edff `q` cells above -- injecting it would be
            # near-redundant with the execution outputs. See README scope note.
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
}

MODULE_NAMES = tuple(MODULES.keys())


def expand_module_spec(name):
    """Resolve FI_MODULE to an ordered list of module keys ('all' = every)."""
    if name == "all":
        return list(MODULE_NAMES)
    if name in MODULES:
        return [name]
    raise KeyError(
        f"unknown FI_MODULE '{name}'. Known: {sorted(MODULE_NAMES)} or 'all'")


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


def _flatten_targets(handle, base_path, out):
    """Expand a (possibly multi-dimensional) handle into depositable leaves.

    A flat packed vector is taken as one target. An unpacked array (e.g. fifo
    `mem[DEPTH]` of packed entries) is recursed per index so a uniform bit pick
    spans every entry. Hierarchy scopes are not flattened here (the walker
    handles those)."""
    if _is_depositable(handle):
        out.append({"handle": handle, "width": len(handle),
                    "path": _path_str(base_path, ())})
        return
    for idx, child in _index_children(handle):
        _flatten_targets(child, base_path + (f"[{idx}]",), out)


def _walk_collect(node, want, base_path, out, max_depth=28, _depth=0):
    """DFS a subtree collecting every sub-handle named `want` ('q' or 'mem').

    Descends through both module/genblock attribute children and indexable
    arrays (genblock arrays such as gen_res_ff[i]). A matched handle is
    flattened to depositable leaves; we do not descend past a match."""
    if _depth > max_depth:
        return
    for name, child in _attr_children(node):
        if name == want:
            _flatten_targets(child, base_path + (name,), out)
            continue
        _walk_collect(child, want, base_path + (name,), out,
                      max_depth=max_depth, _depth=_depth + 1)
    # Indexable scopes (genblock arrays) carry their instances by index, not
    # as named attributes; recurse into each element too.
    for idx, child in _index_children(node):
        _walk_collect(child, want, base_path + (f"[{idx}]",), out,
                      max_depth=max_depth, _depth=_depth + 1)


def collect_targets(dut, module_name):
    """Build the flat list of depositable targets for one module.

    Returns a list of dicts {handle, width, path}. The campaign treats the
    concatenation of all widths as the module's fault space and picks a global
    bit uniformly across it."""
    spec = MODULES[module_name]
    targets = []
    for src in spec["sources"]:
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
        if "names" in src:
            for nm in src["names"]:
                h = getattr(root, nm, None)
                if h is not None and _is_depositable(h):
                    targets.append({"handle": h, "width": len(h),
                                    "path": _path_str(base, (nm,))})
                else:
                    cocotb.log.warning(
                        "fi: module '%s' signal %s.%s missing/not depositable",
                        module_name, ".".join(str(s) for s in base), nm)
        else:  # "walk": "q" | "mem"
            _walk_collect(root, src["walk"], base, targets)
    return targets


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
# Bit-flip primitives. All deposit on a rising edge so they land after the
# design's NBA writes for that cycle.
# ---------------------------------------------------------------------------
async def persistent_bit_flip(clock, signal, bit_index):
    """SEU: flip one bit once and leave it. The cell keeps the flipped value
    until the design naturally overwrites it (next write / scrub / reset).
    Canonical SEU model for flip-flops and SRAM bit cells."""
    await RisingEdge(clock)
    width = len(signal)
    if not 0 <= bit_index < width:
        raise IndexError(f"bit_index {bit_index} out of range [0,{width})")
    signal.value = int(signal.value) ^ (1 << bit_index)


async def transient_bit_flip(clock, signal, bit_index, hold_cycles=1):
    """SET: flip the bit, hold `hold_cycles`, flip back. Models a combinational
    glitch whose net effect is a single mis-sampled value."""
    await RisingEdge(clock)
    width = len(signal)
    if not 0 <= bit_index < width:
        raise IndexError(f"bit_index {bit_index} out of range [0,{width})")
    mask = 1 << bit_index
    signal.value = int(signal.value) ^ mask
    for _ in range(hold_cycles):
        await RisingEdge(clock)
    signal.value = int(signal.value) ^ mask


async def permanent_stuck_at(clock, signal, bit_index, value):
    """Hard fault: force the bit to `value` every cycle until cancelled. The
    design's writes are observed but immediately overwritten, simulating a
    broken cell. Spawn with cocotb.start_soon and kill at end of run."""
    if value not in (0, 1):
        raise ValueError(f"stuck value must be 0 or 1, got {value!r}")
    width = len(signal)
    if not 0 <= bit_index < width:
        raise IndexError(f"bit_index {bit_index} out of range [0,{width})")
    mask = 1 << bit_index
    while True:
        await RisingEdge(clock)
        try:
            cur = int(signal.value)
        except Exception:  # noqa: BLE001 - X during reset; retry next cycle
            continue
        new = (cur | mask) if value else (cur & ~mask)
        if new != cur:
            signal.value = new


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
