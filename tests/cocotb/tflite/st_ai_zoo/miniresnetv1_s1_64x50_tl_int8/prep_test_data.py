#!/usr/bin/env python3
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

"""Generate test_data/input_0.npy and test_data/expected_output_0.npy for the
ST-AI-Zoo MiniResNet-V1 s1 64x50 tl INT8 (10-class) cocotb test.

The model has int8 input ([1, 64, 50, 1], scale=0.3137, zp=127) and float32
output ([1, 10]) -- the trailing DEQUANTIZE op converts int8 logits to
float32 probabilities. Input range in float-domain is [-80, 0], suggesting
the model expects something like a log-mel spectrogram. We don't have a
real example tensor checked in, so we synthesise a deterministic plausible
input with numpy and dump the reference TFLite output as the golden.

Op inventory (15 ops):
  CONV_2D x6, PAD x2, ADD x2, MAX_POOL_2D x1, RESHAPE x1,
  FULLY_CONNECTED x1, SOFTMAX x1, DEQUANTIZE x1.

Run from this directory:

    python3 prep_test_data.py
"""

import os
import sys

import numpy as np
import tensorflow as tf


HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "miniresnetv1_s1_64x50_tl_int8.tflite")
OUT_DIR = os.path.join(HERE, "test_data")


def _synth_input(rng: np.random.Generator) -> np.ndarray:
    """Deterministic synthetic int8 input.

    Real models of this shape consume log-mel-like features; the float-domain
    input range here is roughly [-80, 0] (zp=127, scale=0.3137). We sample a
    smooth low-frequency pattern in that float range, then quantise.
    """
    h, w = 64, 50
    # Smooth 2D pattern: vertical Gaussian bump * horizontal sinusoid, in dB.
    yy = np.linspace(-1.0, 1.0, h)[:, None]
    xx = np.linspace(0.0, 4.0 * np.pi, w)[None, :]
    pattern = -40.0 - 30.0 * np.exp(-(yy ** 2) * 4.0) * (
        0.5 + 0.5 * np.cos(xx + 0.3)
    )
    pattern += rng.normal(0.0, 1.0, size=pattern.shape)  # mild noise
    pattern = np.clip(pattern, -80.0, 0.0).astype(np.float32)
    # Quantise: q = round(f / scale) + zp.
    scale, zp = 0.3137255012989044, 127
    q = np.round(pattern / scale) + zp
    q = np.clip(q, -128, 127).astype(np.int8)
    return q.reshape(1, h, w, 1)


def main() -> int:
    if not os.path.isfile(MODEL):
        print(f"ERROR: missing model {MODEL}", file=sys.stderr)
        return 1

    # Use BUILTIN_REF to avoid the XNNPACK delegate substituting some int8 ops
    # with FP equivalents -- we want a strict TFLite-Micro-equivalent golden.
    interp = tf.lite.Interpreter(
        model_path=MODEL,
        experimental_op_resolver_type=(
            tf.lite.experimental.OpResolverType.BUILTIN_REF
        ),
    )
    interp.allocate_tensors()
    in_det = interp.get_input_details()[0]
    out_det = interp.get_output_details()[0]

    if tuple(in_det["shape"]) != (1, 64, 50, 1) or in_det["dtype"] != np.int8:
        print(
            f"ERROR: unexpected input signature {in_det['shape']} {in_det['dtype']}",
            file=sys.stderr,
        )
        return 1
    if tuple(out_det["shape"]) != (1, 10) or out_det["dtype"] != np.float32:
        print(
            f"ERROR: unexpected output signature {out_det['shape']} {out_det['dtype']}",
            file=sys.stderr,
        )
        return 1

    rng = np.random.default_rng(seed=0xC0DE)
    inp = _synth_input(rng)
    interp.set_tensor(in_det["index"], inp)
    interp.invoke()
    out = interp.get_tensor(out_det["index"])  # float32 [1, 10]

    os.makedirs(OUT_DIR, exist_ok=True)
    in_path = os.path.join(OUT_DIR, "input_0.npy")
    out_path = os.path.join(OUT_DIR, "expected_output_0.npy")
    np.save(in_path, inp.astype(np.int8))
    np.save(out_path, out.reshape(-1).astype(np.float32))

    probs = out.reshape(-1)
    print("Wrote:")
    print(f"  {in_path}  (int8 {inp.shape})")
    print(f"  {out_path}  (float32 {probs.shape})")
    print(f"argmax = {int(np.argmax(probs))}, p_max = {float(probs.max()):.6f}")
    print("probs  =", [round(float(x), 6) for x in probs.tolist()])
    print(f"sum    = {float(probs.sum()):.6f}  (softmax should be ~1.0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
