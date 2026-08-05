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

"""Cocotb test that runs the ST-AI-Zoo ResNet-8 32x32 INT8 CIFAR-10 model
with a per-op MicroProfiler hooked into the interpreter and dumps a per-op
cycle/time report as a bazel undeclared test output (`op_cycles.csv`).

Under bazel the CSV ends up in
`bazel-testlogs/<package>/<target>/test.outputs/` (path of
`$TEST_UNDECLARED_OUTPUTS_DIR`). Outside bazel it falls back to `/tmp`.

Output CSV columns: idx, op_name, cycles, time_ns
"""

import csv
import os

import cocotb
import numpy as np

from bazel_tools.tools.python.runfiles import runfiles
from coralnpu_test_utils.sim_test_fixture import Fixture


_RUNFILES_PREFIX = (
    "coralnpu_hw/tests/cocotb/tflite/st_ai_zoo/resnet8_32_tfs_int8_cifar10/"
)
_ELF = "run_resnet8_32_tfs_int8_cifar10_per_op_cycles_binary.elf"
_INPUT_NPY = "test_data/input_0.npy"

# Must match kMaxOps / kTagBytes in the C++ runner.
_MAX_OPS = 64
_TAG_BYTES = 24


def _pick_reports_dir() -> str:
    out = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if out:
        os.makedirs(out, exist_ok=True)
        return out
    fallback = "/tmp/coralnpu_per_op_cycles_resnet8"
    os.makedirs(fallback, exist_ok=True)
    return fallback


@cocotb.test()
async def core_mini_rvv_resnet8_32_tfs_int8_cifar10_per_op_cycles(dut):
    fixture = await Fixture.Create(dut, highmem=True)
    r = runfiles.Create()

    clock_ns = float(fixture.core_mini_axi.clock_ns)

    elf_path = r.Rlocation(_RUNFILES_PREFIX + _ELF)
    input_data = np.load(r.Rlocation(_RUNFILES_PREFIX + _INPUT_NPY))

    input_data = input_data.astype(np.uint8).flatten()
    assert input_data.size == 1 * 32 * 32 * 3, (
        f"Unexpected input size {input_data.size}, want {1*32*32*3}")

    await fixture.load_elf_and_lookup_symbols(
        elf_path,
        [
            "inference_status",
            "inference_status_message",
            "inference_input",
            "inference_output",
            "op_profile_count",
            "op_profile_cycles",
            "op_profile_tags",
        ],
    )

    await fixture.write("inference_input", input_data)

    cycle_count = await fixture.run_to_halt(timeout_cycles=2000_000_000)
    print(f"ResNet-8 INT8 CIFAR-10 total cycles: {cycle_count}", flush=True)

    status = (await fixture.read_word("inference_status")).view(np.int32)[0]
    message = bytes(
        await fixture.read("inference_status_message", 31)
    ).split(b"\x00", 1)[0].decode(errors="replace")
    assert status == 0, f"Inference failed: status={status} msg='{message}'"

    op_count = int(
        (await fixture.read_word("op_profile_count")).view(np.uint32)[0]
    )
    assert 0 < op_count <= _MAX_OPS, (
        f"Bad op_profile_count={op_count} (max {_MAX_OPS})"
    )

    cycles = np.frombuffer(
        bytes(await fixture.read("op_profile_cycles", _MAX_OPS * 4)),
        dtype=np.uint32,
    )[:op_count]
    tags_raw = bytes(
        await fixture.read("op_profile_tags", _MAX_OPS * _TAG_BYTES)
    )
    tags = []
    for i in range(op_count):
        slot = tags_raw[i * _TAG_BYTES : (i + 1) * _TAG_BYTES]
        tags.append(slot.split(b"\x00", 1)[0].decode(errors="replace"))

    assert int(cycles.sum()) > 0, (
        f"All per-op cycle samples are zero (count={op_count})"
    )

    reports_dir = _pick_reports_dir()
    csv_path = os.path.join(reports_dir, "op_cycles.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "op_name", "cycles", "time_ns"])
        for i, (tag, c) in enumerate(zip(tags, cycles.tolist())):
            w.writerow([i, tag, int(c), f"{int(c) * clock_ns:.2f}"])
        total = int(cycles.sum())
        w.writerow(["", "TOTAL_PROFILED", total, f"{total * clock_ns:.2f}"])
        w.writerow(
            ["", "TOTAL_INVOKE_WALL", cycle_count,
             f"{int(cycle_count) * clock_ns:.2f}"]
        )

    print(f"Wrote per-op cycle report to {csv_path}", flush=True)
    print(f"{'idx':>3} {'op_name':<22} {'cycles':>12} {'time_ns':>14}",
          flush=True)
    for i, (tag, c) in enumerate(zip(tags, cycles.tolist())):
        print(f"{i:>3} {tag:<22} {int(c):>12} {int(c) * clock_ns:>14.2f}",
              flush=True)
    total = int(cycles.sum())
    print(f"{'':>3} {'TOTAL_PROFILED':<22} {total:>12} "
          f"{total * clock_ns:>14.2f}", flush=True)
    print(f"{'':>3} {'TOTAL_INVOKE_WALL':<22} {int(cycle_count):>12} "
          f"{int(cycle_count) * clock_ns:>14.2f}", flush=True)
