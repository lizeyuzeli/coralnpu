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

"""Cocotb test that runs the Arm ML-zoo DNN Small INT8 keyword-spotting model
end-to-end on the RvvCoreMini and checks the produced output against the
ML-zoo reference output."""

import cocotb
import numpy as np

from bazel_tools.tools.python.runfiles import runfiles
from coralnpu_test_utils.sim_test_fixture import Fixture


_RUNFILES_PREFIX = "coralnpu_hw/tests/cocotb/tflite/arm_ml_zoo/dnn_small_int8/"
_ELF = "run_dnn_small_int8_binary.elf"
_INPUT_NPY = "test_data/input_0.npy"
_EXPECTED_NPY = "test_data/expected_output_0.npy"


@cocotb.test()
async def core_mini_rvv_dnn_small_int8(dut):
    fixture = await Fixture.Create(dut, highmem=True)
    r = runfiles.Create()

    elf_path = r.Rlocation(_RUNFILES_PREFIX + _ELF)
    input_path = r.Rlocation(_RUNFILES_PREFIX + _INPUT_NPY)
    expected_path = r.Rlocation(_RUNFILES_PREFIX + _EXPECTED_NPY)

    input_data = np.load(input_path).astype(np.int8).flatten()
    expected_output = np.load(expected_path).astype(np.int8).flatten()
    assert input_data.size == 250, (
        f"Unexpected input size {input_data.size}, want 250")
    assert expected_output.size == 12, (
        f"Unexpected expected output size {expected_output.size}, want 12")

    await fixture.load_elf_and_lookup_symbols(
        elf_path,
        [
            "inference_status",
            "inference_status_message",
            "inference_input",
            "inference_output",
        ],
    )

    # Stage the input into the device's data section.
    await fixture.write("inference_input", input_data)
    # Reset the output buffer to a known value so we can detect a no-op.
    await fixture.write(
        "inference_output", np.zeros(expected_output.size, dtype=np.int8)
    )

    # NOTE: Running this real model in DEBUG mode is slow; allow plenty of
    # cycles. The DNN Small model is small (~84KB, FC+RELU+SOFTMAX) so this
    # is well under the partial mobilenet budget.
    cycle_count = await fixture.run_to_halt(timeout_cycles=50_000_000)
    print(f"DNN Small INT8 total cycles: {cycle_count}", flush=True)

    status = (await fixture.read_word("inference_status")).view(np.int32)[0]
    message = bytes(
        await fixture.read("inference_status_message", 31)
    ).split(b"\x00", 1)[0].decode(errors="replace")
    assert status == 0, f"Inference failed: status={status} msg='{message}'"

    actual_output = (
        await fixture.read("inference_output", expected_output.size)
    ).view(np.int8)
    print(f"expected={expected_output.tolist()}", flush=True)
    print(f"actual  ={actual_output.tolist()}", flush=True)

    # Allow off-by-one quantization differences between the optimized FC
    # kernel and the ML-zoo reference (the kernel uses a different rounding
    # path on some lanes). Be tolerant by 1 LSB but require an exact argmax
    # match -- this is the metric that matters for keyword spotting.
    diff = np.abs(actual_output.astype(np.int32) -
                  expected_output.astype(np.int32))
    max_diff = int(diff.max()) if diff.size else 0
    assert max_diff <= 1, (
        f"Output diverged from reference (max |diff|={max_diff}): "
        f"expected={expected_output.tolist()} actual={actual_output.tolist()}"
    )
    assert int(np.argmax(actual_output)) == int(np.argmax(expected_output)), (
        f"Argmax mismatch: expected={int(np.argmax(expected_output))} "
        f"actual={int(np.argmax(actual_output))}"
    )
    print("DNN Small INT8 inference matched reference output.", flush=True)
