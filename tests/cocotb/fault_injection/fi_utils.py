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

"""Utilities for RTL-level single-event upset (SEU) fault injection.

Phase 1 scope (kept intentionally small):
- Target: the RVV vector register file storage `vreg` inside
  `rvv_backend_vrf_reg` (a packed [NUM_VRF-1:0][VLEN-1:0] register array).
- Fault model: single-bit transient flip. We XOR one bit at an injection
  cycle and XOR it back the very next cycle.
- The framework also exposes a hierarchy-dump helper, which is used the first
  time we wire up a new simulator build so we can confirm the actual VPI path
  to `vreg` before doing real injection campaigns.
"""

import cocotb
from cocotb.handle import HierarchyObject, LogicArrayObject, LogicObject
from cocotb.triggers import RisingEdge


# Hierarchical prefix from the cocotb `dut` (which is the
# `RvvCoreMiniHighmemAxi` toplevel) down to the SV `rvv_backend` module.
# Chisel-emitted instance names: Core -> CoreAxi.core, RvvCoreShim ->
# Core.rvvCore, RvvCoreWrapper -> RvvCoreShim.rvvCoreWrapper,
# SV RvvCore -> wrapper.core, rvv_backend -> RvvCore.backend.
_RVV_BACKEND_PREFIX = ("core", "rvvCore", "rvvCoreWrapper", "core", "backend")

# Catalog of RVV-internal fault-injection targets. Each entry describes how
# to reach a single packed register / register array via the cocotb handle
# tree (`path` is a tuple of attribute names from `dut`), the leaf signal
# name, a short description, and an optional "row width" used to slice the
# packed vector into logical entries (e.g. NUM_VRF rows of VLEN bits).
TARGETS = {
    "vrf_storage": {
        "path": _RVV_BACKEND_PREFIX + ("u_vrf", "vrf_reg"),
        "signal": "vreg",
        "row_bits": 128,  # VLEN; vreg is [NUM_VRF-1:0][VLEN-1:0]
        "fault_model": "persistent",  # storage cell -> SEU stays
        "description": "RVV vector register file storage (NUM_VRF x VLEN)",
    },
    "rob_uop_done": {
        "path": _RVV_BACKEND_PREFIX + ("u_rob",),
        "signal": "uop_done",
        "row_bits": 1,
        "fault_model": "persistent",
        "description": "ROB per-entry completion bit",
    },
    "rob_trap_flag": {
        "path": _RVV_BACKEND_PREFIX + ("u_rob",),
        "signal": "trap_flag",
        "row_bits": 1,
        "fault_model": "persistent",
        "description": "ROB per-entry trap flag",
    },
    "rob_entry_valid": {
        "path": _RVV_BACKEND_PREFIX + ("u_rob",),
        "signal": "entry_valid",
        "row_bits": 1,
        "fault_model": "persistent",
        "description": "ROB per-entry valid bit",
    },
    # MAC unit pipeline registers (INST_MAC[0]). NUM_MUL=2 so a symmetric
    # lane [1] entry could be added later; for now we only target lane 0.
    "mac0_addsrc_d1": {
        "path": _RVV_BACKEND_PREFIX + (
            "u_mulmac", "INST_MAC", 0, "u_mac", "u_addsrc_delay"),
        "signal": "q",
        "row_bits": 8,  # treat as byte rows for reporting
        "fault_model": "persistent",
        "description": "MAC lane 0: VLEN-wide accumulator-add source D1 register",
    },
    "mac0_rob_entry_d1": {
        "path": _RVV_BACKEND_PREFIX + (
            "u_mulmac", "INST_MAC", 0, "u_mac", "u_rob_entry_delay"),
        "signal": "q",
        "row_bits": 1,  # ROB_DEPTH_WIDTH-bit critical control pointer
        "fault_model": "persistent",
        "description": "MAC lane 0: which ROB entry to write back into (control)",
    },
    "mac1_addsrc_d1": {
        "path": _RVV_BACKEND_PREFIX + (
            "u_mulmac", "INST_MAC", 1, "u_mac", "u_addsrc_delay"),
        "signal": "q",
        "row_bits": 8,
        "fault_model": "persistent",
        "description": "MAC lane 1: VLEN-wide accumulator-add source D1 register",
    },
    "mac1_rob_entry_d1": {
        "path": _RVV_BACKEND_PREFIX + (
            "u_mulmac", "INST_MAC", 1, "u_mac", "u_rob_entry_delay"),
        "signal": "q",
        "row_bits": 1,
        "fault_model": "persistent",
        "description": "MAC lane 1: which ROB entry to write back into (control)",
    },
    # ALU lane 0 (the CMP-capable unit): full pipeline payload register
    # between dispatch and execute stage P1. Holds the whole PIPE_DATA_t
    # struct (opcode + vs1 + vs2 + vd + rob_entry + ...).
    "alu0_uop_p1": {
        "path": _RVV_BACKEND_PREFIX + ("u_alu", "u_alu_cmp_unit", "uop_p1"),
        "signal": "q",
        "row_bits": 8,
        "fault_model": "persistent",
        "description": "ALU unit 0: P1 pipeline payload (struct, opcode+operands+rob)",
    },
    # DIV unit: result information register feeding into the iterative
    # divider output stage. Struct holds w_data + rob_entry + meta.
    "div_res_info": {
        "path": _RVV_BACKEND_PREFIX + (
            "u_div", "DIV_UNIT", 0, "u_div_unit", "res_information"),
        "signal": "q",
        "row_bits": 8,
        "fault_model": "persistent",
        "description": "DIV unit: result-info struct register (data + rob entry)",
    },
}

# Signal used by Campaign C to gate injection on "RVV is doing real work".
# When at least one ROB entry is valid, the backend is processing an RVV uop
# pipeline. We use this as a coarse activity proxy. `path` is the parent
# module's handle path; `signal` is the leaf name.
ACTIVITY_GATE = {
    "path": _RVV_BACKEND_PREFIX + ("u_rob",),
    "signal": "entry_valid",
    "description": "RVV ROB has at least one in-flight uop",
}

# Back-compat aliases used by the early prototype.
DEFAULT_VRF_PATH = TARGETS["vrf_storage"]["path"]
DEFAULT_VREG_SIGNAL = TARGETS["vrf_storage"]["signal"]


def get_target(name):
    """Return the TARGETS entry, raising a clear error if unknown."""
    if name not in TARGETS:
        raise KeyError(
            f"unknown FI target '{name}'. Known: {sorted(TARGETS)}")
    return TARGETS[name]


def _try_get_child(node, step):
    """Return a child handle if it exists, else None.

    `step` may be either a string (attribute access) or an int (array
    subscript, used to descend into SV generate-loop instances).

    cocotb exposes sub-handles via attribute access. When the name does not
    resolve, accessing it raises AttributeError (or returns a handle whose
    `_handle` is null in older versions). We treat both as "missing". For
    generate blocks, Verilator typically exposes them under their loop
    label as an indexable handle; if that fails we also try the mangled
    name form `<name>__BRA__<i>__KET__` that Verilator sometimes emits.
    """
    if isinstance(step, int):
        try:
            return node[step]
        except (IndexError, TypeError, KeyError, AttributeError):
            return None
    # String step.
    try:
        child = getattr(node, step)
    except (AttributeError, Exception):  # noqa: BLE001 - cocotb internals
        child = None
    if child is not None:
        return child
    # Try Verilator-mangled `name[i]` -> `name__BRA__i__KET__` rewrite for
    # convenience when users write paths like "INST_MAC[0]" inline.
    if "[" in step and step.endswith("]"):
        base, idx = step[:-1].split("[", 1)
        try:
            return getattr(node, f"{base}__BRA__{idx}__KET__")
        except (AttributeError, Exception):  # noqa: BLE001
            return None
    return None


def resolve_handle(dut, path, signal):
    """Walk `dut` along `path`, then return `signal` child.

    `path` is an iterable of steps; each step is a string (attribute) or
    an int (array index, e.g. into a generate-loop instance array).
    Returns the resolved handle, or None if any step is missing.
    """
    node = dut
    for step in path:
        child = _try_get_child(node, step)
        if child is None:
            cocotb.log.warning(
                "fi_utils: hierarchy step '%r' not found under %s",
                step, getattr(node, "_path", repr(node)))
            return None
        node = child
    sig = _try_get_child(node, signal)
    if sig is None:
        cocotb.log.warning(
            "fi_utils: signal '%s' not found under %s", signal,
            getattr(node, "_path", repr(node)))
    return sig


def dump_hierarchy(node, max_depth=6, _depth=0, _prefix=""):
    """Recursively print the (cocotb-visible) sub-handle tree.

    Prints at most `max_depth` levels. Useful the first time we bring up the
    Verilator model so we can confirm the actual exposed VPI hierarchy.
    """
    try:
        children = list(node._sub_handles.items())
    except Exception:  # noqa: BLE001
        children = []
    # Some cocotb versions populate _sub_handles lazily; force discovery.
    try:
        for _name in dir(node):
            pass
    except Exception:  # noqa: BLE001
        pass
    try:
        children = list(node._sub_handles.items())
    except Exception:  # noqa: BLE001
        pass

    name_self = getattr(node, "_name", repr(node))
    cocotb.log.info("%s%s  [%s]", _prefix, name_self, type(node).__name__)
    if _depth >= max_depth:
        return
    for child_name, child in children:
        try:
            dump_hierarchy(child, max_depth=max_depth, _depth=_depth + 1,
                           _prefix=_prefix + "  ")
        except Exception as e:  # noqa: BLE001
            cocotb.log.info("%s  <error descending %s: %s>", _prefix,
                            child_name, e)


def search_for_signal(node, target_name, max_depth=10, _depth=0):
    """DFS for the first sub-handle whose leaf name equals `target_name`.

    Returns the handle path as a list of names, or None.
    """
    name_self = getattr(node, "_name", "")
    if name_self == target_name:
        return []
    if _depth >= max_depth:
        return None
    try:
        items = list(node._sub_handles.items())
    except Exception:  # noqa: BLE001
        items = []
    if not items:
        # Try forcing discovery
        try:
            for _ in dir(node):
                pass
            items = list(node._sub_handles.items())
        except Exception:  # noqa: BLE001
            items = []
    for child_name, child in items:
        try:
            sub = search_for_signal(child, target_name,
                                    max_depth=max_depth, _depth=_depth + 1)
        except Exception:  # noqa: BLE001
            sub = None
        if sub is not None:
            return [child_name] + sub
    return None


async def persistent_bit_flip(clock, signal, bit_index, *, log=True):
    """Standard register/SRAM SEU model: flip one bit and leave it.

    On the next rising edge of `clock`, read the current full-vector value,
    XOR in `1 << bit_index`, and write it back. The flipped bit then
    persists in the storage cell until the design naturally overwrites it
    (next legitimate write to that row, ECC scrub, reset, etc.). This is
    the canonical SEU model for flip-flops and SRAM bit cells.
    """
    if signal is None:
        raise RuntimeError("persistent_bit_flip: signal handle is None")
    await RisingEdge(clock)
    try:
        current = int(signal.value)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"persistent_bit_flip: failed to read signal value: {e}") from e
    width = len(signal)
    if bit_index < 0 or bit_index >= width:
        raise IndexError(
            f"bit_index {bit_index} out of range [0,{width})")
    mask = 1 << bit_index
    flipped = current ^ mask
    signal.value = flipped
    if log:
        cocotb.log.info(
            "fi: SEU bit %d (mask bit), width=%d  (persistent flip, no restore)",
            bit_index, width)


async def transient_bit_flip(clock, signal, bit_index, hold_cycles=1,
                             *, log=True):
    """Glitch / SET model: flip the bit, hold for `hold_cycles`, then flip
    back. Use this only for *combinational* fault models; for register or
    memory cells prefer `persistent_bit_flip`.
    """
    if signal is None:
        raise RuntimeError("transient_bit_flip: signal handle is None")
    await RisingEdge(clock)
    current = int(signal.value)
    width = len(signal)
    if bit_index < 0 or bit_index >= width:
        raise IndexError(
            f"bit_index {bit_index} out of range [0,{width})")
    mask = 1 << bit_index
    signal.value = current ^ mask
    if log:
        cocotb.log.info(
            "fi: glitch bit %d (hold=%d cycles), width=%d",
            bit_index, hold_cycles, width)
    for _ in range(hold_cycles):
        await RisingEdge(clock)
    post = int(signal.value)
    signal.value = post ^ mask


def classify_outcome(*, fault_flag, status, max_abs_diff, argmax_match,
                     hung, masked_tolerance=1, acc_degraded_threshold=4):
    """Bucket a fault-injection run into a fine-grained outcome label.

    The taxonomy distinguishes hardware-detected (CRASH) from
    software-detected (DETECTED) failures and splits silent corruption
    into a "fully wrong" (SDC) bucket vs a "right class but noisy logits"
    (ACC_DEGRADED) bucket:

    | Bucket         | Condition                                                 |
    |----------------|-----------------------------------------------------------|
    | `HANG`         | run timed out (`hung=True`)                               |
    | `CRASH`        | hardware fault path reached (`fault_flag=True`)           |
    | `DETECTED`     | halted normally but app status != 0                       |
    | `SDC`          | halted, status==0, but top-1 class differs from golden    |
    | `ACC_DEGRADED` | halted, status==0, top-1 correct, max_abs_diff > thresh   |
    | `MASKED`       | halted, status==0, top-1 correct, max_abs_diff <= ε       |

    `masked_tolerance` (ε): max int8 |diff| still considered "noise".
    `acc_degraded_threshold`: above this max int8 |diff|, even a correct
    top-1 is treated as accuracy degradation rather than masked.
    """
    if hung:
        return "HANG"
    if fault_flag:
        return "CRASH"
    if status is None or status != 0:
        return "DETECTED"
    if not argmax_match:
        return "SDC"
    diff = max_abs_diff if max_abs_diff is not None else 0
    if diff > acc_degraded_threshold:
        return "ACC_DEGRADED"
    if diff > masked_tolerance:
        # Right class, mild noise above tolerance but below the "obvious
        # logit shift" threshold. Tag as ACC_DEGRADED for visibility; can
        # be folded back into MASKED by raising `masked_tolerance`.
        return "ACC_DEGRADED"
    return "MASKED"
