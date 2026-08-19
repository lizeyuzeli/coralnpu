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

"""Static half of the fault-space reconciliation: which sequential elements
exist in the RTL, and which of them the injector can even reach.

The campaign's fault space is a HAND-WRITTEN list of hierarchical paths
(fi_utils.MODULES). A hand-written list drifts, and its drift is invisible in
the results: a cell that is missing from the registry is a cell no campaign
ever strikes, which reads as "this state is not vulnerable" rather than as an
error. `trap_ready` sat outside every module's fault space for two whole
stages exactly this way, and `space_bits` only catches the reverse case (a
path that stops resolving).

Reconciliation therefore has two halves, and neither can replace the other:

  static (this script)   what sequential cells does the RTL contain, and is
                         each one EXPOSED to VPI at all? Reads the design
                         source, so it sees cells that the simulator does not
                         publish -- the class of gap that needs a new vlt
                         directive before any registry line can help.
  runtime (fi_audit.py)  of the cells the simulator does publish under
                         rvv_backend, which ones is the registry actually
                         collecting, and how many bits? Needs a live
                         hierarchy, so it is the only side that can report
                         exact widths and real paths.

Input is the ONE generated file the build actually compiles
(bazel-bin/.../RvvCoreMiniHighmemAxi.sv), preprocessed by `verilator -E` with
the build's defines. Preprocessing is not a convenience: `ifdef`-selected
state is the majority of the FT mechanism (`replay_mem`, the *_tmr copies), so
a scan of the raw source would either miss it or count state that is not in
the build. Pass the same -D set the model is built with, and add
-DFAULT_TOLERANT_ON to audit the FT_ON configuration.

Scope is the `rvv_backend` instantiation subtree, computed from the module
graph rather than from a name pattern -- the backend pulls in third-party
cvfpu modules under u_falu, and those flip-flops are as real as ours.

Usage:
    verilator -E -Wno-fatal -DUSE_GENERIC="" -DTB_SUPPORT -DZVE32F_ON \
        -DVLEN_128 bazel-bin/hdl/chisel/src/coralnpu/RvvCoreMiniHighmemAxi.sv \
        > /tmp/pp.sv
    python3 fi_seq_audit.py /tmp/pp.sv [--vlt rules/default.vlt.tpl]
"""

import argparse
import collections
import re
import sys


# Generic sequential wrappers. One vlt directive per wrapper exposes every
# instance of it in the whole design, so a cell stored inside one of these is
# reachable no matter where it sits in the hierarchy. This is why the registry
# can say `walk="q"` and get the entire execution pipeline.
WRAPPER_STORAGE = {
    "edff": "q",
    "edff_2d": "q",
    "cdffr": "q",
    "dff": "q",
    "multi_fifo": "mem",
}

# Root of the audited subtree.
ROOT_MODULE = "rvv_backend"


def strip_comments(text):
    """Remove // and /* */ comments and `line directives.

    Comments are removed before any parsing because both halves of this script
    key on keywords (`always_ff`, module names), and a commented-out block or a
    module name mentioned in prose would otherwise be counted as real RTL."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"^\s*`line[^\n]*$", "", text, flags=re.M)
    # String literals too: a lone '(' inside a $display format would unbalance
    # the paren-depth test that separates an NBA from a `<=` comparison.
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)
    return text


def split_modules(text):
    """{module_name: body} for every module definition in `text`."""
    out = {}
    for m in re.finditer(r"\bmodule\s+(\w+)\b", text):
        end = text.find("endmodule", m.end())
        if end < 0:
            continue
        out[m.group(1)] = text[m.end():end]
    return out


def _paren_depth_prefix(body):
    """Cumulative '(' minus ')' depth at every character offset of `body`.

    Used to tell an NBA apart from a `<=` comparison. `if (a <= b)` has the
    operator at paren depth >= 1; `q <= a + (b - c)` has it at depth 0. Index
    selects `[...]` do not affect this, so `mem[i] <= d` is still depth 0.
    Purely lexical, and that is enough -- there is no construct in this RTL
    where a statement-level nonblocking assignment sits inside parentheses.

    Computed PER BLOCK, never per module: over a 38k-character module body a
    single unbalanced paren anywhere (a macro, a stray one in a comment that
    survived stripping) offsets every later depth and silently turns every
    assignment below it into a "comparison". That is how `rvv_backend`'s
    replay_mem first came back clean -- the failure is silent and looks exactly
    like "this module has no registers", which is the one answer an audit must
    never invent."""
    depth = [0] * (len(body) + 1)
    d = 0
    for i, ch in enumerate(body):
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
        depth[i + 1] = d
    return depth


def _block_extent(body, start):
    """End offset of the procedural block whose header ends at `start`.

    A `begin`/`end` body is matched by keyword counting; a single-statement
    body ends at its `;`. `endcase` / `endfunction` etc. do not disturb the
    count because the pattern is word-anchored."""
    m = re.compile(r"\bbegin\b").search(body, start)
    semi = body.find(";", start)
    if m is None or (semi >= 0 and semi < m.start()):
        return semi + 1 if semi >= 0 else len(body)
    depth = 0
    for tok in re.finditer(r"\bbegin\b|\bend\b", body[m.start():]):
        depth += 1 if tok.group(0) == "begin" else -1
        if depth == 0:
            return m.start() + tok.end()
    return len(body)


def _lhs_base(lhs):
    """Base identifier of an assignment target, or None.

    Strips trailing field selects and index selects to get from
    `replay_mem[rs_dp2alu[i].rob_entry]` to `replay_mem`. The brackets must be
    matched by scanning, not by a regex character class: an index expression
    can itself contain an index (`mem[ptr[j]]`), and a non-nesting pattern
    silently matches nothing there -- which drops the assignment entirely and
    reports the register as absent."""
    i = len(lhs)
    while True:
        while i and lhs[i - 1].isspace():
            i -= 1
        if i and lhs[i - 1] == "]":
            d, j = 0, i
            while j:
                j -= 1
                if lhs[j] == "]":
                    d += 1
                elif lhs[j] == "[":
                    d -= 1
                    if d == 0:
                        break
            if d != 0:
                return None
            i = j
            continue
        m = re.search(r"\.\w+$", lhs[:i])
        if m:
            i = m.start()
            continue
        break
    m = re.search(r"(\w+)$", lhs[:i])
    return m.group(1) if m else None


def sequential_targets(body):
    """{signal: n_assignments} for every nonblocking-assigned signal in a
    clocked block of this module body.

    `always_ff` and `always @(posedge ...)` both count; `always_comb` and
    combinational `always @(*)` do not, and neither does a blocking `=`. The
    signal recorded is the base identifier, since that is the granularity a
    vlt directive and a VPI handle work at (a bit- or index-select of a packed
    vector is not separately exposable).

    Also returns how many clocked blocks were seen and how many of them yielded
    no target at all. That second number is the parser's own self-check: every
    failure mode of this regex layer (a block extent cut short, an LHS form the
    stripper does not understand) ends in "this module has no registers", which
    is indistinguishable from a module that genuinely has none. The caller
    treats a nonzero count as an error rather than printing a clean report --
    `replay_mem` was missed exactly this way, by a nested index select."""
    out = collections.Counter()
    blocks = mute = 0
    for hdr in re.finditer(r"\balways_ff\b|\balways\b", body):
        # Sensitivity list. always_comb / always @(*) are not clocked; an
        # `always @(posedge clk)` is. always_ff without a posedge does not
        # occur in this RTL but would be reported by the fallthrough.
        tail = body[hdr.end():hdr.end() + 200]
        if hdr.group(0) == "always" and "posedge" not in tail.split(")")[0]:
            continue
        blocks += 1
        blk = body[hdr.end():_block_extent(body, hdr.end())]
        depth = _paren_depth_prefix(blk)
        found = 0
        for a in re.finditer(r"<=", blk):
            if depth[a.start()] != 0:
                continue  # comparison inside a condition, not an assignment
            nm = _lhs_base(blk[:a.start()])
            if nm:
                out[nm] += 1
                found += 1
        if not found:
            mute += 1
    return out, blocks, mute


def instantiated_modules(body, known):
    """Set of modules from `known` instantiated in this body.

    Matched as `<module> [#(...)] <instance_name> (`, which is what separates
    an instantiation from a type name or a function call. Over-approximating
    here would only widen the audited subtree, never narrow it."""
    out = set()
    for name in known:
        pat = re.compile(r"\b" + re.escape(name) +
                         r"\b\s*(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?\w+\s*\(")
        if pat.search(body):
            out.add(name)
    return out


def parse_vlt(path):
    """{(module, var)} exposed read-write by the vlt template."""
    out = set()
    pat = re.compile(r'public_flat_rw\s+-module\s+"([^"]+)"\s+-var\s+"([^"]+)"')
    with open(path) as f:
        for line in f:
            m = pat.search(line)
            if m:
                out.add((m.group(1), m.group(2)))
    return out


def reachable(modules, insts, root):
    """Module names reachable from `root` through the instantiation graph."""
    seen, stack = set(), [root]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in modules:
            continue
        seen.add(cur)
        stack.extend(insts.get(cur, ()))
    return seen


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("preprocessed", help="verilator -E output of the toplevel")
    ap.add_argument("--vlt", default="rules/default.vlt.tpl",
                    help="vlt template that declares the VPI exposures")
    ap.add_argument("--root", default=ROOT_MODULE,
                    help="module whose subtree is audited")
    ap.add_argument("--emit-vlt", action="store_true",
                    help="print public_flat_rw directives for the NOT-exposed "
                         "signals instead of the report, ready to paste into "
                         "the vlt template")
    args = ap.parse_args(argv)

    with open(args.preprocessed, errors="replace") as f:
        text = strip_comments(f.read())
    modules = split_modules(text)
    if args.root not in modules:
        sys.exit(f"root module '{args.root}' not found in {args.preprocessed}")

    insts = {name: instantiated_modules(body, modules)
             for name, body in modules.items()}
    scope = reachable(modules, insts, args.root)
    exposed = parse_vlt(args.vlt)

    # A module's storage is reachable either because it IS one of the generic
    # wrappers (one directive covers every instance) or because the template
    # names that exact (module, var) pair.
    covered, uncovered = [], []
    n_blocks = n_mute = 0
    mute_mods = []
    for name in sorted(scope):
        seq, blocks, mute = sequential_targets(modules[name])
        n_blocks += blocks
        n_mute += mute
        if mute:
            mute_mods.append((name, mute, blocks))
        for var, n in sorted(seq.items()):
            wrapper_var = WRAPPER_STORAGE.get(name)
            is_cov = (var == wrapper_var) or ((name, var) in exposed)
            (covered if is_cov else uncovered).append((name, var, n))

    by_mod_uncovered = collections.defaultdict(list)
    for name, var, n in uncovered:
        by_mod_uncovered[name].append((var, n))

    if args.emit_vlt:
        # Generated, not hand-written, for the same reason this script exists:
        # the ~290 unexposed cells are mostly third-party FP state (cvfpu,
        # ct_vfdsu) under u_falu / u_div, and a hand-kept list of that size
        # drifts the moment the vendor drop changes. Exposing them touches
        # only OUR vlt template -- third-party sources stay untouched.
        # Verify what this prints before pasting: the static scan cannot
        # evaluate generate conditions, so it can name a register that no
        # instance elaborates (multi_fifo `dataout`, gated by DATAOUT_REG).
        # The runtime audit (fi_audit.py) is the check -- an exposure that
        # elaborates shows up there as a new leaf.
        print("`verilator_config")
        for name in sorted({m for m, _, _ in uncovered}):
            for var, _ in sorted(by_mod_uncovered[name]):
                print(f'public_flat_rw -module "{name}" -var "{var}"')
        return 0

    print(f"# fault-space static audit: {args.preprocessed}")
    print(f"# root={args.root}  modules in subtree={len(scope)}"
          f"  vlt exposures={len(exposed)}")
    print(f"# sequential signals: {len(covered)} exposed, "
          f"{len(uncovered)} NOT exposed to VPI")
    print()
    print("## exposed (reachable by the injector)")
    for name, var, n in covered:
        how = "wrapper" if WRAPPER_STORAGE.get(name) == var else "vlt -var"
        print(f"  {name}.{var}  ({n} assignment(s), {how})")
    print()
    print("## NOT exposed -- no campaign can ever strike these")
    for name in sorted(by_mod_uncovered):
        print(f"  {name}:")
        for var, n in by_mod_uncovered[name]:
            print(f"    {var}  ({n} assignment(s))")
    print()
    print(f"# parser self-check: {n_blocks} clocked block(s) in subtree, "
          f"{n_mute} produced no target")
    if n_mute:
        for name, mute, blocks in mute_mods:
            print(f"#   {name}: {mute}/{blocks} block(s) unparsed")
        print("# ERROR: a clocked block with no recognised assignment target "
              "means this scan is under-reporting, and under-reporting here "
              "looks exactly like 'that state does not exist'.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
