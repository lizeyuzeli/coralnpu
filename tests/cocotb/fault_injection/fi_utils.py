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
from cocotb.triggers import FallingEdge, RisingEdge


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
                                    "path": _path_str(base, (nm,)),
                                    "owner": root, "kind": "name"})
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
# Bit-flip primitives. `seu` and `stuck` deposit on a rising edge, so they land
# after the design's NBA writes for that cycle. `set` is the exception: it must
# align to the cell's write edge to model a combinational upset being latched,
# so it both observes and deposits on the FALLING edge (a read at the rising
# edge returns the pre-edge value, since the NBA has not settled yet). See
# transient_bit_flip.
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

      "pose"  right after RisingEdge   -- identical to the production primitives
      "nege"  right after FallingEdge  -- candidate fix (mid-cycle strike)

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
