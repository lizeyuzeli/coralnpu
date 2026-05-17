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
ST-AI-Zoo "ST MNIST-v1 28x28 tfs INT8" EMNIST/byclass (36-class) cocotb test.

Boundary signature:
  Input  : uint8 [1, 28, 28, 1], q=(scale=1/127.5, zp=127)
           -- float-domain pixels in roughly [-1.0, 1.0].
  Output : float32 [1, 36] (trailing DEQUANTIZE).

Op inventory (BUILTIN_REF):
  QUANTIZE x1, CONV_2D x3, DEPTHWISE_CONV_2D x2, MEAN x1,
  FULLY_CONNECTED x1, SOFTMAX x1, DEQUANTIZE x1.
"""

import os
import sys

import numpy as np
import tensorflow as tf
from PIL import Image


HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "st_mnistv1_28_tfs_int8.tflite")
IMAGE = os.path.join(HERE, "images", "example.jpg")
OUT_DIR = os.path.join(HERE, "test_data")


def _load_and_preprocess(image_path: str, hw: int) -> np.ndarray:
    """Center-crop to a square, resize to (hw, hw) grayscale, return uint8."""
    img = Image.open(image_path).convert("L")  # 1-channel
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((hw, hw), Image.BILINEAR)
    arr = np.asarray(img, dtype=np.uint8)  # HW
    return arr.reshape(1, hw, hw, 1)


def main() -> int:
    if not os.path.isfile(MODEL):
        print(f"ERROR: missing model {MODEL}", file=sys.stderr)
        return 1
    if not os.path.isfile(IMAGE):
        print(f"ERROR: missing image {IMAGE}", file=sys.stderr)
        return 1

    interp = tf.lite.Interpreter(
        model_path=MODEL,
        experimental_op_resolver_type=(
            tf.lite.experimental.OpResolverType.BUILTIN_REF
        ),
    )
    interp.allocate_tensors()
    in_det = interp.get_input_details()[0]
    out_det = interp.get_output_details()[0]

    if tuple(in_det["shape"]) != (1, 28, 28, 1) or in_det["dtype"] != np.uint8:
        print(f"ERROR: unexpected input {in_det['shape']} {in_det['dtype']}",
              file=sys.stderr)
        return 1
    if tuple(out_det["shape"]) != (1, 36) or out_det["dtype"] != np.float32:
        print(f"ERROR: unexpected output {out_det['shape']} {out_det['dtype']}",
              file=sys.stderr)
        return 1

    inp = _load_and_preprocess(IMAGE, 28)
    interp.set_tensor(in_det["index"], inp)
    interp.invoke()
    out = interp.get_tensor(out_det["index"])  # float32 [1, 36]

    os.makedirs(OUT_DIR, exist_ok=True)
    in_path = os.path.join(OUT_DIR, "input_0.npy")
    out_path = os.path.join(OUT_DIR, "expected_output_0.npy")
    np.save(in_path, inp.astype(np.uint8))
    np.save(out_path, out.reshape(-1).astype(np.float32))

    probs = out.reshape(-1)
    print("Wrote:")
    print(f"  {in_path}  (uint8 {inp.shape})")
    print(f"  {out_path}  (float32 {probs.shape})")
    print(f"argmax = {int(np.argmax(probs))}, p_max = {float(probs.max()):.6f}")
    print(f"sum    = {float(probs.sum()):.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
