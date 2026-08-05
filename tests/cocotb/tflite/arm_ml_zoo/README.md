# TFLite-Micro End-to-End Model Tests (Arm ML-Zoo)

Each subdirectory here corresponds to one INT8-quantized model from the
Arm ML-Zoo and ships an **end-to-end cocotb test**: the model is flashed
into RvvCoreMini's memory, a real reference input is fed through `Invoke()`,
and the output is read back and compared byte-for-byte with the ML-Zoo
reference output.

## 1. Models currently covered

| Subdirectory | Model (`.tflite`) | Input shape | Output shape | Main ops | Task |
|---|---|---|---|---|---|
| `dnn_small_int8/` | `dnn_s_quantized.tflite` | `(1, 490)` | `(1, 12)` | `FullyConnected`, `Reshape`, `Softmax` | KWS (pure-FC baseline) |
| `ds_cnn_small_int8/` | `ds_cnn_s_quantized.tflite` | `(1, 49, 10, 1)` | `(1, 12)` | `Conv2D`, `DepthwiseConv2D`, `AveragePool2D`, `FullyConnected`, `Relu`, `Reshape`, `Softmax` | KWS |
| `ad_small_int8/` | `ad_small_int8.tflite` | `(1, 32, 32, 1)` | `(1, 8)` | `Conv2D`, `DepthwiseConv2D`, `AveragePool2D`, `Relu6`, `Reshape` | Anomaly detection (MicroNet Small) |
| `kws_micronet_small_int8/` | `kws_micronet_s.tflite` | `(1, 49, 10, 1)` | `(1, 12)` | `Conv2D`, `DepthwiseConv2D`, `AveragePool2D`, `Relu6`, `Reshape` | KWS (MicroNet Small) |
| `micronet_vww2_int8/` | `vww2_50_50_INT8.tflite` | `(1, 50, 50, 1)` | `(1, 2)` | `Conv2D`, `DepthwiseConv2D`, `AveragePool2D`, `Add`, `Pad`, `Relu6`, `Reshape` | Visual Wake Words |
| `rnn_noise_int8/` | `rnnoise_INT8.tflite` | 4 int8 tensors (main + 3 GRU states) | 5 int8 tensors | `Add`, `Concatenation`, `Dequantize`, `FullyConnected`, `Logistic`, `Mul`, `Pack`, `Quantize`, `Relu`, `Reshape`, `Split`, `SplitV`, `Sub`, `Tanh`, `Unpack` | Noise suppression |

> Any new model just has to follow the "directory layout + three-file
> contract" described in section 3 below to plug in.

---

## 2. Directory layout (identical for every model)

```
<model_name>/
  BUILD                       # bazel target definitions
  run_<model_name>.cc         # device-side RISC-V program: register ops + run Invoke
  cocotb_<model_name>.py      # host-side cocotb test: load ELF, write input, read output, compare
  models/
    <model>.tflite            # model weights
    definition.yaml           # ML-Zoo metadata (I/O shapes, op list, benchmarks)
    README.md                 # ML-Zoo's own description
    get_class_labels.sh
  test_data/
    input_0.npy               # ML-Zoo reference input  (int8)
    expected_output_0.npy     # ML-Zoo reference output (int8)
```

`rnn_noise_int8` is the one outlier: it has 4 inputs / 5 outputs, so
`test_data/` contains several `.npy` files (`main_input_int8_0.npy`,
`vad_gru_prev_state_int8_0.npy`, `Identity_*_int8_0.npy`, ...), and
`cocotb_rnn_noise_int8.py` uses a "dispatch by tensor byte size to the
matching symbol" multi-IO protocol (see
`rnn_noise_int8/run_rnn_noise_int8.cc:64-74`).

---

## 3. What each of the three files must contain

### 3.1 `BUILD`

Template (using `kws_micronet_small_int8` as the example):

```python
load("//rules:coco_tb.bzl", "cocotb_test_suite")
load("//rules:coralnpu_v2.bzl", "coralnpu_v2_binary")
load("//rules:utils.bzl", "generate_cc_arrays")
load(
    "//tests/cocotb:build_defs.bzl",
    "VCS_BUILD_ARGS", "VCS_DEFINES", "VCS_TEST_ARGS",
)

VCS_COMMON_DATA = [
    "//tests/cocotb:coverage_exclude.cfg",
    "//hdl/verilog:dpi_files",
]

# 1) Embed the .tflite blob as a C array.
generate_cc_arrays(name = "kws_micronet_s_cc", src = "models/kws_micronet_s.tflite", out = "kws_micronet_s.cc")
generate_cc_arrays(name = "kws_micronet_s_h", src = "models/kws_micronet_s.tflite", out = "kws_micronet_s.h")

# 2) Device-side ELF.
coralnpu_v2_binary(
    name = "run_kws_micronet_small_int8_binary",
    srcs = ["run_kws_micronet_small_int8.cc", ":kws_micronet_s_cc"],
    hdrs = [":kws_micronet_s_h"],
    dtcm_size_kbytes = 1024,         # always highmem
    itcm_size_kbytes = 1024,
    visibility = ["//visibility:public"],
    deps = [
        # Pick deps by ops actually used; the set below is the common one.
        "//sw/opt/litert-micro:conv",
        "//sw/opt/litert-micro:depthwise_conv",
        # "//sw/opt/litert-micro:fully_connected",
        "@tflite_micro//tensorflow/lite/micro:micro_framework",
        "@tflite_micro//tensorflow/lite/micro:micro_log",
        "@tflite_micro//tensorflow/lite/micro:micro_profiler",
        "@tflite_micro//tensorflow/lite/micro:op_resolvers",
        "@tflite_micro//tensorflow/lite/micro:system_setup",
    ],
)

# 3) cocotb test target.
cocotb_test_suite(
    name = "cocotb_kws_micronet_small_int8",
    simulators = ["verilator", "vcs"],
    testcases = ["core_mini_rvv_kws_micronet_small_int8"],
    testcases_vname = "KWS_MICRONET_SMALL_INT8_TESTCASES",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],          # real-model inference is too slow for CI; only run on demand
        "hdl_toplevel": "RvvCoreMiniHighmemAxi",
        "seed": "42",
        "size": "enormous",
        "test_module": ["cocotb_kws_micronet_small_int8.py"],
        "deps": [
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils:sim_test_fixture",
            "@rules_python//python/runfiles",
        ],
        "data": [
            "run_kws_micronet_small_int8_binary.elf",
            "test_data/input_0.npy",
            "test_data/expected_output_0.npy",
        ],
    },
    vcs_build_args = VCS_BUILD_ARGS,
    vcs_data = glob(["**/*.elf"]) + glob(["test_data/*.npy"]) + VCS_COMMON_DATA,
    vcs_defines = VCS_DEFINES,
    vcs_test_args = VCS_TEST_ARGS,
    vcs_verilog_sources = ["//hdl/chisel/src/coralnpu:rvv_core_mini_highmem_axi_cc_library_verilog"],
    verilator_model = "//tests/cocotb:rvv_core_mini_highmem_axi_model",
)
```

Key points:

- **`tags = ["manual"]`** on every model test: `bazel test //...` does not
  trigger them by default, only an explicit target does. Reason: a full
  inference under verilator is anywhere from tens of minutes to hours,
  which would crush CI.
- **`size = "enormous"`** plus a caller-side `--test_timeout`: bazel's
  `enormous` cap is 3600 s. The KWS-MicroNet tier (~15 M cycles) fits in
  ~1150 s under verilator fastbuild, but the heavier models do not
  necessarily; pair with `--test_timeout=7200` when in doubt.
- **`dtcm_size_kbytes = 1024 / itcm_size_kbytes = 1024`**: the highmem
  image is mandatory, otherwise the model constants + tensor arena will
  not fit. This pairs with `hdl_toplevel = "RvvCoreMiniHighmemAxi"`.
- **`generate_cc_arrays`** auto-embeds the `.tflite` file as the
  `g_<basename>_model_data` C symbol (named after the input file, *not*
  the target). For example `kws_micronet_s.tflite` →
  `g_kws_micronet_s_model_data`, which is what `run_*.cc` references.

### 3.2 `run_<model>.cc`

Device-side skeleton (single-input / single-output, the common case):

```cpp
extern "C" {
int8_t  inference_status = -1;                                 // host polls this
char    inference_status_message[31] __attribute__((section(".data"), aligned(16)));
int8_t  inference_input [kInputBytes]  __attribute__((section(".data"), aligned(16)));
int8_t  inference_output[kOutputBytes] __attribute__((section(".data"), aligned(16)));

constexpr size_t kTensorArenaSize = 512 * 1024;                // size to taste
uint8_t tensor_arena[kTensorArenaSize] __attribute__((section(".data"), aligned(16)));
}

int main() {
  std::strncpy(inference_status_message, "Started", 31);
  const tflite::Model* model = tflite::GetModel(g_<basename>_model_data);
  if (model->version() != TFLITE_SCHEMA_VERSION) { /* msg + return -1 */ }

  tflite::MicroMutableOpResolver<N> op_resolver;
  // op_resolver.AddConv2D(coralnpu_v2::opt::litert_micro::Register_CONV_2D());      // RVV-optimized
  // op_resolver.AddDepthwiseConv2D(coralnpu_v2::opt::litert_micro::Register_DEPTHWISE_CONV_2D());
  // op_resolver.AddFullyConnected(coralnpu_v2::opt::litert_micro::Register_FULLY_CONNECTED());
  // op_resolver.AddRelu6(); / AddPad(); / AddAdd(); ...

  tflite::MicroInterpreter interp(model, op_resolver, tensor_arena, kTensorArenaSize);
  interp.AllocateTensors();
  std::memcpy(interp.input(0)->data.data, inference_input, kInputBytes);
  interp.Invoke();
  std::memcpy(inference_output, interp.output(0)->data.data, kOutputBytes);

  std::strncpy(inference_status_message, "Invoke successful", 31);
  inference_status = 0;
  return 0;
}
```

Conventions:

- **Every global symbol is `extern "C"` + explicit `section(".data")` +
  `aligned(16)`.** The cocotb side resolves them by name, so the ABI
  symbol must survive (no C++ name mangling).
- **`inference_status == 0` means success.** The host checks it first
  before bothering to compare outputs.
- **`inference_status_message` is 31 bytes**, used to surface failure
  reasons on the host side ("Bad model schema version" / "Error during
  AllocateTensors" / "Error during Invoke" / etc.).
- **Arena size, rules of thumb**: pure-FC models 256 KB; the
  DS-CNN/KWS/AD tier 512 KB is plenty; MicroNet Small / VWW-2 want
  768 KB ~ 1 MB; RNN-Noise needs 1 MB (multiple inputs stay resident).
  Start big in `.extdata` (1 MB) to get it running, then shrink.
- **The `N` of `MicroMutableOpResolver<N>` must equal the actual `Add*`
  count**, taken from the `operators: TensorFlow Lite:` list in
  `definition.yaml`.
- **Prefer the `coralnpu_v2::opt::litert_micro` optimized kernels** for
  `Register_CONV_2D` / `Register_DEPTHWISE_CONV_2D` /
  `Register_FULLY_CONNECTED`. The structural ops (`Add`, `Pad`,
  `Relu6`, `AveragePool2D`, `Reshape`, ...) can stay on the TFLM
  reference implementations.

### 3.3 `cocotb_<model>.py`

Host-side test skeleton:

```python
import cocotb, numpy as np
from bazel_tools.tools.python.runfiles import runfiles
from coralnpu_test_utils.sim_test_fixture import Fixture

_RUNFILES_PREFIX = "coralnpu_hw/tests/cocotb/tflite/arm_ml_zoo/<model_name>/"

@cocotb.test()
async def core_mini_rvv_<model_name>(dut):
    fixture = await Fixture.Create(dut, highmem=True)
    r = runfiles.Create()
    elf = r.Rlocation(_RUNFILES_PREFIX + "run_<model_name>_binary.elf")
    inp = np.load(r.Rlocation(_RUNFILES_PREFIX + "test_data/input_0.npy")).astype(np.int8).flatten()
    ref = np.load(r.Rlocation(_RUNFILES_PREFIX + "test_data/expected_output_0.npy")).astype(np.int8).flatten()

    await fixture.load_elf_and_lookup_symbols(elf, [
        "inference_status", "inference_status_message",
        "inference_input", "inference_output",
    ])
    await fixture.write("inference_input",  inp)
    await fixture.write("inference_output", np.zeros(ref.size, dtype=np.int8))

    cycles = await fixture.run_to_halt(timeout_cycles=2_000_000_000)
    print(f"total cycles: {cycles}", flush=True)

    status = (await fixture.read_word("inference_status")).view(np.int32)[0]
    msg = bytes(await fixture.read("inference_status_message", 31)).split(b"\x00", 1)[0].decode()
    assert status == 0, f"Inference failed: status={status} msg='{msg}'"

    out = (await fixture.read("inference_output", ref.size)).view(np.int8)
    assert int(np.abs(out.astype(int) - ref.astype(int)).max()) <= 1
    assert int(np.argmax(out)) == int(np.argmax(ref))
```

Tolerance conventions:

- **`max|diff| <= 1` LSB**: the standard precision ceiling between
  different TFLM kernel implementations (quantization rounding can
  drift by at most 1 LSB).
- **`argmax` match**: an extra guard for classification tasks against a
  borderline LSB flipping the top-1.
- **Regression-style outputs (e.g. RNN-Noise's `Identity_1` gains)**:
  loosen to `<= 2` LSB; this is what `cocotb_rnn_noise_int8.py:46`
  already does.
- **`timeout_cycles=2_000_000_000`**: pegged to the realistic cycle
  ceiling. Start with 200 M for small models and bump on timeout.

---

## 4. How to run

```bash
# Build only (skip simulation): sanity-check that the ELF + cocotb
# runner come up cleanly.
bazel build //tests/cocotb/tflite/arm_ml_zoo/<model>:cocotb_<model>

# Full end-to-end inference (highmem + generous timeout recommended).
bazel test //tests/cocotb/tflite/arm_ml_zoo/<model>:cocotb_<model>_core_mini_rvv_<model> \
           --test_output=streamed \
           --test_timeout=7200
```

`bazel test //tests/cocotb/tflite/arm_ml_zoo/...` does not trigger any
model test, since they are all tagged `tags = ["manual"]`. To batch-run
them, either explicitly enumerate the targets or override the tag
filter (`--test_tag_filters=-manual` won't help on its own because
`manual` is excluded by default; you have to opt the targets in
explicitly).

---

## 5. Measured numbers (for picking timeouts / spotting bottlenecks)

| Model | `.tflite` size | cycles | sim time (ns) | cycles / KB | measured on |
|---|---:|---:|---:|---:|---|
| `dnn_small_int8` | 81 KB | 276 779 | 346 045 | 3.4 K | current |
| `ds_cnn_small_int8` | 46 KB | 4 570 136 | 5 712 684 | 99 K | current |
| `ad_small_int8` | 246 KB | 35 669 862 | 44 587 471 | 145 K | old base |
| `micronet_vww2_int8` | 273 KB | 15 240 404 | 19 050 765 | 56 K | old base |
| `kws_micronet_small_int8` | 111 KB | 14 702 087 | 18 377 711 | 132 K | current |

The `old base` rows were measured before this branch was rebased onto
current main and are kept only as an upper bound for picking timeouts.
Re-measuring showed the gain scaling with how conv-heavy the model is:
`kws_micronet_small_int8` dropped from 140 045 588 to 14 702 087 cycles
(9.5x), `ds_cnn_small_int8` from 29 186 100 to 4 570 136 (6.4x), while
the pure-FC `dnn_small_int8` barely moved (-1.7%). The gain is therefore
concentrated in the convolution / memory path rather than being a uniform
core speedup, and the two remaining stale rows should be read as
optimistic-to-re-measure in the same direction.

`kws_micronet_small_int8` is also the only test that exercises
`Conv2D_Generic` (its `10x4` stem is neither 1x1 / 3x3 / 4x4); its
reference-output match is the end-to-end evidence for that kernel.

Takeaway: **the `.tflite` size only reflects parameter count; inference
cycles are roughly `params x average feature-map area`.** The 46 KB
`ds_cnn_small_int8` costs 16x the 81 KB `dnn_small_int8`, and the 111 KB
`kws_micronet_small_int8` costs another 3x on top of that, because its
49x10 time-frequency map is barely downsampled and every weight is
multiplied hundreds of times. File size ranks these models in nearly the
opposite order to cost. (The two `old base` rows cannot be compared
against the current ones — `ad_small_int8` only looks like the most
expensive model because its number predates the conv-path speedup.)

---

## 6. Relationship with `sw/opt/litert-micro/` RVV kernels

Each test in this directory simultaneously validates the RVV-optimized
kernels in `sw/opt/litert-micro/conv.cc`, `depthwise_conv.cc`,
`fully_connected.cc`, etc.:

- `Conv2D_1x1` / `Conv2D_3x3` / `Conv2D_4x4` / `Conv2D_Generic`: heavily
  hit by the MicroNet family (AD / KWS / VWW-2) plus DS-CNN.
- `Conv2D_Generic` (OC-vectorized fallback for arbitrary `(kH, kW)`):
  designed precisely for cases like the KWS-MicroNet `10x4` stem, which
  is neither 1x1 / 3x3 / 4x4 yet has enormous MAC counts.
- `FullyConnected`: used end-to-end by DNN-Small, and inside RNN-Noise
  for the GRU's internal dense layers.
- `Logistic` (int8 LUT): RNN-Noise's sigmoid path.

After any kernel change, run `dnn_small_int8` + `ds_cnn_small_int8` as
a quick regression. The KWS / AD / VWW-2 / RNN-Noise tier is slower,
so reserve it for full regressions on larger changes.

---

## 7. Checklist for adding a new model

1. Create `<new_model>/` here, drop `.tflite` into `models/` and
   `input_0.npy` / `expected_output_0.npy` into `test_data/`.
2. Copy the closest-matching existing model as a template (rule of
   thumb: pure FC -> `dnn_small_int8`; CNN+RELU6 -> `ad_small_int8`;
   CNN+ADD/PAD -> `micronet_vww2_int8`; multi-IO/RNN ->
   `rnn_noise_int8`).
3. Update `BUILD`: model filename, target names, the `N` of
   `MicroMutableOpResolver<N>`, deps (per ops), test names.
4. Update `run_<model>.cc`: `g_<basename>_model_data`, `kInputBytes` /
   `kOutputBytes`, op-registration list, arena size.
5. Update `cocotb_<model>.py`: `_RUNFILES_PREFIX`, ELF / npy filenames,
   shape asserts, `timeout_cycles`, tolerances.
6. `bazel build` first to confirm both ELF and cocotb runner come up.
7. `bazel test` end-to-end; pick `--test_timeout` from the closest
   shape/op-set neighbour in the table in section 5.
