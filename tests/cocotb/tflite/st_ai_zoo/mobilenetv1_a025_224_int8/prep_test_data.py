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
ST-AI-Zoo MobileNet-V1 alpha=0.25 224x224 INT8 cocotb test.

The model's signature dtypes are uint8 (input) and float32 (output) -- the
internal heavy ops (15 CONV_2D + 13 DEPTHWISE_CONV_2D + ...) all run on int8;
the boundary uint8/float32 is only due to a leading QUANTIZE op and a trailing
DEQUANTIZE op. We therefore:

  - feed `images/example.jpg`, center-cropped + resized to 224x224, as raw
    uint8 pixels (the model's QUANTIZE op handles uint8 -> int8 internally),
  - run the reference TFLite interpreter once, and
  - dump the input + the dequantized 1000-class probability vector.

Run from this directory:

    python3 prep_test_data.py

Writes test_data/input_0.npy (uint8 [1,224,224,3]) and
test_data/expected_output_0.npy (float32 [1000]).
"""

import os
import sys

import numpy as np
import tensorflow as tf
from PIL import Image


HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "mobilenetv1_a025_224_int8.tflite")
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

    interp = tf.lite.Interpreter(model_path=MODEL)
    interp.allocate_tensors()
    in_det = interp.get_input_details()[0]
    out_det = interp.get_output_details()[0]

    if tuple(in_det["shape"]) != (1, 224, 224, 3) or in_det["dtype"] != np.uint8:
        print(
            f"ERROR: unexpected input signature {in_det['shape']} {in_det['dtype']}",
            file=sys.stderr,
        )
        return 1
    if tuple(out_det["shape"]) != (1, 1000) or out_det["dtype"] != np.float32:
        print(
            f"ERROR: unexpected output signature {out_det['shape']} {out_det['dtype']}",
            file=sys.stderr,
        )
        return 1

    inp = _load_and_preprocess(IMAGE, 224)
    interp.set_tensor(in_det["index"], inp)
    interp.invoke()
    out = interp.get_tensor(out_det["index"])  # float32 [1, 1000]

    os.makedirs(OUT_DIR, exist_ok=True)
    in_path = os.path.join(OUT_DIR, "input_0.npy")
    out_path = os.path.join(OUT_DIR, "expected_output_0.npy")
    np.save(in_path, inp.astype(np.uint8))
    np.save(out_path, out.reshape(-1).astype(np.float32))

    probs = out.reshape(-1)
    top5 = np.argsort(-probs)[:5]
    print("Wrote:")
    print(f"  {in_path}  (uint8 {inp.shape})")
    print(f"  {out_path}  (float32 {probs.shape})")
    print(f"argmax = {int(np.argmax(probs))}, p_max = {float(probs.max()):.6f}")
    print("top-5  =", [(int(i), float(probs[i])) for i in top5])
    print(f"sum    = {float(probs.sum()):.6f}  (softmax should be ~1.0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
