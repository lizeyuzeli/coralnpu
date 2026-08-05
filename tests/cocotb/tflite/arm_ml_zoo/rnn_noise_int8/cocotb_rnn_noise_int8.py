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

"""Cocotb test that runs the Arm ML-zoo RNNoise INT8 noise-suppression model
end-to-end on the RvvCoreMini and checks each of the five produced outputs
against the corresponding ML-zoo reference output."""

import cocotb
import numpy as np

from bazel_tools.tools.python.runfiles import runfiles
from coralnpu_test_utils.sim_test_fixture import Fixture


_RUNFILES_PREFIX = "coralnpu_hw/tests/cocotb/tflite/arm_ml_zoo/rnn_noise_int8/"
_ELF = "run_rnn_noise_int8_binary.elf"

# (npy filename, device symbol, expected element count)
_INPUTS = [
    ("test_data/main_input_int8_0.npy",            "input_main",          42),
    ("test_data/vad_gru_prev_state_int8_0.npy",    "input_vad_state",     24),
    ("test_data/noise_gru_prev_state_int8_0.npy",  "input_noise_state",   48),
    ("test_data/denoise_gru_prev_state_int8_0.npy","input_denoise_state", 96),
]

# (npy filename, device symbol, expected element count, tolerance LSB).
# Outputs: Identity   = denoise gru next state (96)
#          Identity_1 = gains                  (22)
#          Identity_2 = noise gru next state   (48)
#          Identity_3 = vad gru next state     (24)
#          Identity_4 = voice activity prob    (1)
_OUTPUTS = [
    ("test_data/Identity_int8_0.npy",   "output_denoise_state", 96, 1),
    ("test_data/Identity_1_int8_0.npy", "output_gains",         22, 2),
    ("test_data/Identity_2_int8_0.npy", "output_noise_state",   48, 1),
    ("test_data/Identity_3_int8_0.npy", "output_vad_state",     24, 1),
    ("test_data/Identity_4_int8_0.npy", "output_vad_prob",       1, 1),
]


@cocotb.test()
async def core_mini_rvv_rnn_noise_int8(dut):
    fixture = await Fixture.Create(dut, highmem=True)
    r = runfiles.Create()

    elf_path = r.Rlocation(_RUNFILES_PREFIX + _ELF)

    # Load every reference tensor up-front so that any size mismatch fails
    # before the (slow) simulation starts.
    inputs = []
    for npy_name, sym, expected_n in _INPUTS:
        data = np.load(r.Rlocation(_RUNFILES_PREFIX + npy_name))
        data = data.astype(np.int8).flatten()
        assert data.size == expected_n, (
            f"{npy_name}: expected {expected_n} int8 elements, got {data.size}"
        )
        inputs.append((sym, data))

    expected_outputs = []
    for npy_name, sym, expected_n, tol in _OUTPUTS:
        data = np.load(r.Rlocation(_RUNFILES_PREFIX + npy_name))
        data = data.astype(np.int8).flatten()
        assert data.size == expected_n, (
            f"{npy_name}: expected {expected_n} int8 elements, got {data.size}"
        )
        expected_outputs.append((sym, data, tol))

    symbol_names = (
        ["inference_status", "inference_status_message"]
        + [sym for sym, _ in inputs]
        + [sym for sym, _, _ in expected_outputs]
    )
    await fixture.load_elf_and_lookup_symbols(elf_path, symbol_names)

    for sym, data in inputs:
        await fixture.write(sym, data)
    for sym, ref, _ in expected_outputs:
        await fixture.write(sym, np.zeros(ref.size, dtype=np.int8))

    # RNNoise has many GRU/FC layers; give the simulator a generous budget.
    cycle_count = await fixture.run_to_halt(timeout_cycles=2000_000_000)
    print(f"RNNoise INT8 total cycles: {cycle_count}", flush=True)

    status = (await fixture.read_word("inference_status")).view(np.int32)[0]
    message = bytes(
        await fixture.read("inference_status_message", 31)
    ).split(b"\x00", 1)[0].decode(errors="replace")
    assert status == 0, f"Inference failed: status={status} msg='{message}'"

    failures = []
    for sym, ref, tol in expected_outputs:
        actual = (await fixture.read(sym, ref.size)).view(np.int8)
        diff = np.abs(actual.astype(np.int32) - ref.astype(np.int32))
        max_diff = int(diff.max()) if diff.size else 0
        print(
            f"{sym}: max|diff|={max_diff} tol={tol} "
            f"actual={actual.tolist()} expected={ref.tolist()}",
            flush=True,
        )
        if max_diff > tol:
            failures.append(
                f"{sym}: max|diff|={max_diff} > tol={tol}"
            )

    assert not failures, "RNNoise output mismatch:\n  " + "\n  ".join(failures)
    print("RNNoise INT8 inference matched reference outputs.", flush=True)
