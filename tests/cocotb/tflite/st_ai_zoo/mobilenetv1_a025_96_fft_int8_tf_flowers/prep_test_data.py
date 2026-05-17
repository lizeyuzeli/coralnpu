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
ST-AI-Zoo MobileNet-V1 alpha=0.25 96x96 INT8 tf_flowers (5-class) cocotb test.

The model has the same uint8-in / float32-out boundary as
mobilenetv1_a025_224_int8 (QUANTIZE leading, DEQUANTIZE trailing); all heavy
ops are int8 internally. Op inventory:
  CONV_2D x14, DEPTHWISE_CONV_2D x13, FULLY_CONNECTED x1, MEAN x1,
  SOFTMAX x1, QUANTIZE x1, DEQUANTIZE x1.
"""

import os
import sys

import numpy as np
import tensorflow as tf
from PIL import Image


HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "mobilenetv1_a025_96_fft_int8.tflite")
IMAGE = os.path.join(HERE, "images", "example.jpg")
OUT_DIR = os.path.join(HERE, "test_data")


def _load_and_preprocess(image_path: str, hw: int) -> np.ndarray:
    """Center-crop to a square, resize to (hw, hw), return uint8 RGB tensor."""
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((hw, hw), Image.BILINEAR)
    arr = np.asarray(img, dtype=np.uint8)  # HWC
    return arr.reshape(1, hw, hw, 3)


def main() -> int:
    if not os.path.isfile(MODEL):
        print(f"ERROR: missing model {MODEL}", file=sys.stderr)
        return 1
    if not os.path.isfile(IMAGE):
        print(f"ERROR: missing image {IMAGE}", file=sys.stderr)
        return 1

    # `experimental_preserve_all_tensors=True` keeps every intermediate int8
    # activation around so we can dump the first Conv2D's output as a golden
    # for the on-device "first-conv trap" debug harness.
    #
    # `BUILTIN_REF` opts out of the XNNPACK delegate, which would otherwise
    # transparently rewrite some int8 ops to FP/AVX-SIMD equivalents and
    # introduce ~LSB rounding noise vs. the strict TFLM int8 reference path
    # that the device runs. Using BUILTIN_REF makes the host golden bit-for-bit
    # comparable to TFLM reference int8 -- so any residual diff vs. the device
    # is attributable to the RVV kernel, not host-side requant differences.
    interp = tf.lite.Interpreter(
        model_path=MODEL,
        experimental_preserve_all_tensors=True,
        experimental_op_resolver_type=(
            tf.lite.experimental.OpResolverType.BUILTIN_REF
        ),
    )
    interp.allocate_tensors()
    in_det = interp.get_input_details()[0]
    out_det = interp.get_output_details()[0]

    if tuple(in_det["shape"]) != (1, 96, 96, 3) or in_det["dtype"] != np.uint8:
        print(
            f"ERROR: unexpected input signature {in_det['shape']} {in_det['dtype']}",
            file=sys.stderr,
        )
        return 1
    if tuple(out_det["shape"]) != (1, 5) or out_det["dtype"] != np.float32:
        print(
            f"ERROR: unexpected output signature {out_det['shape']} {out_det['dtype']}",
            file=sys.stderr,
        )
        return 1

    inp = _load_and_preprocess(IMAGE, 96)
    interp.set_tensor(in_det["index"], inp)
    interp.invoke()
    out = interp.get_tensor(out_det["index"])  # float32 [1, 5]

    os.makedirs(OUT_DIR, exist_ok=True)
    in_path = os.path.join(OUT_DIR, "input_0.npy")
    out_path = os.path.join(OUT_DIR, "expected_output_0.npy")
    np.save(in_path, inp.astype(np.uint8))
    np.save(out_path, out.reshape(-1).astype(np.float32))

    # Locate the first CONV_2D op and dump its int8 output tensor as the
    # golden for the on-device first-conv trap.
    ops = interp._get_ops_details()
    first_conv = next(o for o in ops if o["op_name"] == "CONV_2D")
    first_conv_out_idx = int(first_conv["outputs"][0])
    first_conv_out = interp.get_tensor(first_conv_out_idx)
    fc_det = next(
        d for d in interp.get_tensor_details() if int(d["index"]) == first_conv_out_idx
    )
    fc_path = os.path.join(OUT_DIR, "first_conv_expected.npy")
    np.save(fc_path, first_conv_out.astype(np.int8))
    print(f"  {fc_path}  (int8 {first_conv_out.shape}, "
          f"tensor_idx={first_conv_out_idx}, "
          f"q=scale {float(fc_det['quantization'][0]):.6f} "
          f"zp {int(fc_det['quantization'][1])})")
    print(f"  first_conv min={int(first_conv_out.min())} "
          f"max={int(first_conv_out.max())} "
          f"mean={float(first_conv_out.astype(np.float32).mean()):.3f}")

    probs = out.reshape(-1)
    print("Wrote:")
    print(f"  {in_path}  (uint8 {inp.shape})")
    print(f"  {out_path}  (float32 {probs.shape})")
    print(f"argmax = {int(np.argmax(probs))}, p_max = {float(probs.max()):.6f}")
    print("probs  =", [round(float(x), 6) for x in probs.tolist()])
    print(f"sum    = {float(probs.sum()):.6f}  (softmax should be ~1.0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
