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

"""Cocotb test that runs the ST-AI-Zoo MobileNet-V1 alpha=0.25 224x224 INT8
ImageNet model end-to-end on the RvvCoreMini and compares the produced
1000-class probability vector against the reference TFLite output.

The model has a uint8 input / float32 output signature (its first op is a
QUANTIZE that maps uint8 -> int8 and its last op is a DEQUANTIZE that maps
int8 -> float32). All compute-heavy ops in between are int8->int8 and exercise
the same RVV kernels as the arm_ml_zoo INT8 tests, so this test doubles as a
mixed-dtype boundary smoke-test for `Register_CONV_2D` / `Register_DEPTHWISE_CONV_2D`.
"""

import cocotb
import numpy as np

from bazel_tools.tools.python.runfiles import runfiles
from coralnpu_test_utils.sim_test_fixture import Fixture


_RUNFILES_PREFIX = (
    "coralnpu_hw/tests/cocotb/tflite/st_ai_zoo/mobilenetv1_a025_224_int8/"
)
_ELF = "run_mobilenetv1_a025_224_int8_binary.elf"
_INPUT_NPY = "test_data/input_0.npy"
_EXPECTED_NPY = "test_data/expected_output_0.npy"

# Tolerance on the dequantized float32 output. The output quantization scale
# is 1/256 ~= 0.00390625, so a 1-LSB int8 difference dequantizes to ~3.9e-3.
# We allow a little headroom for any reordering between TFLite XNNPACK
# reference (host) and the bare TFLite-Micro int8 path (device).
_MAX_ABS_DIFF = 8e-3


@cocotb.test()
async def core_mini_rvv_mobilenetv1_a025_224_int8(dut):
    fixture = await Fixture.Create(dut, highmem=True)
    r = runfiles.Create()

    elf_path = r.Rlocation(_RUNFILES_PREFIX + _ELF)
    input_data = np.load(r.Rlocation(_RUNFILES_PREFIX + _INPUT_NPY))
    expected_output = np.load(r.Rlocation(_RUNFILES_PREFIX + _EXPECTED_NPY))

    input_data = input_data.astype(np.uint8).flatten()
    expected_output = expected_output.astype(np.float32).flatten()
    assert input_data.size == 1 * 224 * 224 * 3, (
        f"Unexpected input size {input_data.size}, want {1*224*224*3}")
    assert expected_output.size == 1000, (
        f"Unexpected expected output size {expected_output.size}, want 1000")

    await fixture.load_elf_and_lookup_symbols(
        elf_path,
        [
            "inference_status",
            "inference_status_message",
            "inference_input",
            "inference_output",
        ],
    )

    await fixture.write("inference_input", input_data)
    await fixture.write(
        "inference_output", np.zeros(expected_output.size, dtype=np.float32)
    )

    # Heavy: 28 conv layers @ 224x224 down to 7x7. Budget generously.
    cycle_count = await fixture.run_to_halt(timeout_cycles=2000_000_000)
    print(
        f"MobileNet-V1 0.25@224 INT8 total cycles: {cycle_count}", flush=True
    )

    status = (await fixture.read_word("inference_status")).view(np.int32)[0]
    message = bytes(
        await fixture.read("inference_status_message", 31)
    ).split(b"\x00", 1)[0].decode(errors="replace")
    assert status == 0, f"Inference failed: status={status} msg='{message}'"

    actual_bytes = await fixture.read(
        "inference_output", expected_output.size * 4
    )
    actual_output = np.frombuffer(bytes(actual_bytes), dtype=np.float32)

    exp_argmax = int(np.argmax(expected_output))
    act_argmax = int(np.argmax(actual_output))
    diff = np.abs(actual_output - expected_output)
    max_diff = float(diff.max()) if diff.size else 0.0

    exp_top5 = np.argsort(-expected_output)[:5].tolist()
    act_top5 = np.argsort(-actual_output)[:5].tolist()
    print(
        f"expected argmax={exp_argmax} p={expected_output[exp_argmax]:.4f} "
        f"top5={exp_top5}",
        flush=True,
    )
    print(
        f"actual   argmax={act_argmax} p={actual_output[act_argmax]:.4f} "
        f"top5={act_top5}",
        flush=True,
    )
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
        "MobileNet-V1 0.25@224 INT8 inference matched reference.", flush=True
    )
