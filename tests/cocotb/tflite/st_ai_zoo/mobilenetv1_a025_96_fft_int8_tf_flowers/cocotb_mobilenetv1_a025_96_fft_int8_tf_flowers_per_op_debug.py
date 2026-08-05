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

"""Cocotb test that runs the ST-AI-Zoo MobileNet-V1 alpha=0.25 96x96 INT8
tf_flowers (5-class) classifier end-to-end on the RvvCoreMini and compares the
produced 5-class probability vector against the reference TFLite output.

The model has a uint8 input / float32 output signature (its first op is a
QUANTIZE that maps uint8 -> int8 and its last op is a DEQUANTIZE that maps
int8 -> float32). All compute-heavy ops in between are int8->int8 and exercise
the same RVV CONV_2D / DEPTHWISE_CONV_2D / FULLY_CONNECTED kernels.
"""

import cocotb
import numpy as np

from bazel_tools.tools.python.runfiles import runfiles
from coralnpu_test_utils.sim_test_fixture import Fixture


_RUNFILES_PREFIX = (
    "coralnpu_hw/tests/cocotb/tflite/st_ai_zoo/"
    "mobilenetv1_a025_96_fft_int8_tf_flowers/"
)
_ELF = "run_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_debug_binary.elf"
_INPUT_NPY = "test_data/input_0.npy"
_EXPECTED_NPY = "test_data/expected_output_0.npy"
_FIRST_CONV_EXPECTED_NPY = "test_data/first_conv_expected.npy"

# Must match kMaxOps / kStatsPerOp in
# run_mobilenetv1_a025_96_fft_int8_tf_flowers.cc.
_MAX_OPS = 64
_STATS_PER_OP = 6
_OP_KIND_NAMES = {1: "CONV_2D", 2: "DW_CONV", 3: "MEAN", 4: "FULLY_C"}
_TFLITE_TYPE_NAMES = {
    0: "NoType",
    1: "Float32",
    2: "Int32",
    3: "UInt8",
    4: "Int64",
    5: "String",
    6: "Bool",
    7: "Int16",
    8: "Complex64",
    9: "Int8",
}

# Same tolerance as the 224x224 variant: int8 softmax has a 1/256 ~= 3.9e-3
# LSB; allow ~2x headroom for any reordering between the host XNNPACK
# reference and the device int8 path.
_MAX_ABS_DIFF = 8e-3


@cocotb.test()
async def core_mini_rvv_mobilenetv1_a025_96_fft_int8_tf_flowers_per_op_debug(dut):
    fixture = await Fixture.Create(dut, highmem=True)
    r = runfiles.Create()

    elf_path = r.Rlocation(_RUNFILES_PREFIX + _ELF)
    input_data = np.load(r.Rlocation(_RUNFILES_PREFIX + _INPUT_NPY))
    expected_output = np.load(r.Rlocation(_RUNFILES_PREFIX + _EXPECTED_NPY))
    first_conv_expected = np.load(
        r.Rlocation(_RUNFILES_PREFIX + _FIRST_CONV_EXPECTED_NPY)
    )

    input_data = input_data.astype(np.uint8).flatten()
    expected_output = expected_output.astype(np.float32).flatten()
    first_conv_expected = first_conv_expected.astype(np.int8).flatten()
    assert input_data.size == 1 * 96 * 96 * 3, (
        f"Unexpected input size {input_data.size}, want {1*96*96*3}")
    assert expected_output.size == 5, (
        f"Unexpected expected output size {expected_output.size}, want 5")
    assert first_conv_expected.size == 1 * 48 * 48 * 8, (
        f"Unexpected first_conv size {first_conv_expected.size}, "
        f"want {1*48*48*8}")

    await fixture.load_elf_and_lookup_symbols(
        elf_path,
        [
            "inference_status",
            "inference_status_message",
            "inference_input",
            "inference_output",
            "inference_diag",
            "first_conv_output",
            "op_stats",
            "op_stats_count",
        ],
    )

    await fixture.write("inference_input", input_data)
    await fixture.write(
        "inference_output", np.zeros(expected_output.size, dtype=np.float32)
    )
    await fixture.write(
        "first_conv_output",
        np.zeros(first_conv_expected.size, dtype=np.int8),
    )
    await fixture.write(
        "op_stats",
        np.zeros(_MAX_OPS * _STATS_PER_OP, dtype=np.uint32),
    )
    await fixture.write("op_stats_count", np.zeros(1, dtype=np.uint32))

    cycle_count = await fixture.run_to_halt(timeout_cycles=2000_000_000)
    print(
        f"MobileNet-V1 0.25@96 FFT INT8 total cycles: {cycle_count}",
        flush=True,
    )

    status = (await fixture.read_word("inference_status")).view(np.int32)[0]
    message = bytes(
        await fixture.read("inference_status_message", 31)
    ).split(b"\x00", 1)[0].decode(errors="replace")

    # Diag block (16 x uint32). See run_*.cc header for layout.
    diag = np.frombuffer(
        bytes(await fixture.read("inference_diag", 16 * 4)), dtype=np.uint32
    )
    in_scale = float(np.frombuffer(diag[5:6].tobytes(), dtype=np.float32)[0])
    out_scale = float(np.frombuffer(diag[9:10].tobytes(), dtype=np.float32)[0])
    print(
        "diag: inputs_size={} outputs_size={} "
        "in0(type={}, bytes={}, zp={}, scale={:.6f}, first4={:#010x}) "
        "out0(type={}, bytes={}, zp={}, scale={:.6f}, first4={:#010x}, "
        "last4={:#010x}) arena_used={}".format(
            int(diag[0]), int(diag[1]),
            int(diag[2]), int(diag[3]), np.int32(diag[4]).item(), in_scale,
            int(diag[12]),
            int(diag[6]), int(diag[7]), np.int32(diag[8]).item(), out_scale,
            int(diag[10]), int(diag[11]),
            int(diag[13]),
        ),
        flush=True,
    )

    assert status == 0, f"Inference failed: status={status} msg='{message}'"

    # -------------------------------------------------------------------
    # First conv sanity cross-check (already proven good in a prior run,
    # but cheap to keep verifying).
    # -------------------------------------------------------------------
    first_conv_actual = np.frombuffer(
        bytes(await fixture.read("first_conv_output", first_conv_expected.size)),
        dtype=np.int8,
    )
    fc_diff = np.abs(
        first_conv_actual.astype(np.int32) - first_conv_expected.astype(np.int32)
    )
    fc_max = int(fc_diff.max()) if fc_diff.size else 0
    fc_match = int((fc_diff == 0).sum())
    fc_total = int(fc_diff.size)
    print(
        f"first_conv_output: {fc_match}/{fc_total} exact "
        f"({100.0*fc_match/fc_total:.2f}%), max|diff|={fc_max}",
        flush=True,
    )

    # -------------------------------------------------------------------
    # Per-op output stats table (the actual bug-locator).
    # -------------------------------------------------------------------
    op_stats_count = int(
        (await fixture.read_word("op_stats_count")).view(np.uint32)[0]
    )
    op_stats_raw = np.frombuffer(
        bytes(await fixture.read("op_stats", _MAX_OPS * _STATS_PER_OP * 4)),
        dtype=np.uint32,
    ).reshape(_MAX_OPS, _STATS_PER_OP)
    print(
        f"\nper-op output stats (op_stats_count = {op_stats_count}):",
        flush=True,
    )
    print(
        "  idx kind     dtype   nelem      min      max  abs_mean*1000  zeros",
        flush=True,
    )
    suspicious = []
    for i in range(min(op_stats_count, _MAX_OPS)):
        s = op_stats_raw[i]
        kind = int(s[0] & 0xFF)
        out_type = int((s[0] >> 8) & 0xFF)
        nelem = int(s[1])
        lo = np.int32(s[2]).item()
        hi = np.int32(s[3]).item()
        absmean = np.int32(s[4]).item()
        zeros = np.int32(s[5]).item()
        kind_name = _OP_KIND_NAMES.get(kind, f"?{kind}")
        type_name = _TFLITE_TYPE_NAMES.get(out_type, f"?{out_type}")
        print(
            f"  [{i:2d}] {kind_name:8s} {type_name:7s} {nelem:6d}  "
            f"{lo:+5d}  {hi:+5d}  {absmean:10d}  {zeros:6d}",
            flush=True,
        )
        # A "collapsed" layer is one whose output is constant -- min == max.
        # Note: a real ReLU6-style layer can legitimately saturate one side
        # (e.g. min == -128 with hi near zero is OK), but min == max means
        # every output element is the same value, which the rest of the
        # network can never recover from.
        if nelem > 0 and lo == hi and out_type == 9:  # 9 == Int8
            suspicious.append((i, kind_name, lo, nelem))

    if suspicious:
        print("\n*** SUSPICIOUS LAYERS (constant output) ***", flush=True)
        for i, kind, val, nelem in suspicious:
            print(
                f"  op[{i:2d}] {kind} produced CONSTANT value {val:+d} for "
                f"all {nelem} elements -- the chain dies here.",
                flush=True,
            )

    actual_bytes = await fixture.read(
        "inference_output", expected_output.size * 4
    )
    actual_output = np.frombuffer(bytes(actual_bytes), dtype=np.float32)

    exp_argmax = int(np.argmax(expected_output))
    act_argmax = int(np.argmax(actual_output))
    diff = np.abs(actual_output - expected_output)
    max_diff = float(diff.max()) if diff.size else 0.0

    print(f"expected probs={expected_output.tolist()} argmax={exp_argmax}",
          flush=True)
    print(f"actual   probs={actual_output.tolist()} argmax={act_argmax}",
          flush=True)
    print(f"max|diff| = {max_diff:.6f}", flush=True)

    assert act_argmax == exp_argmax, (
        f"Argmax mismatch: expected={exp_argmax} actual={act_argmax} "
        f"(max|diff|={max_diff:.6f})"
    )
    assert max_diff <= _MAX_ABS_DIFF, (
        f"Output diverged from reference: max|diff|={max_diff:.6f} > "
        f"{_MAX_ABS_DIFF}"
    )
    print(
        "MobileNet-V1 0.25@96 FFT INT8 inference matched reference.",
        flush=True,
    )
