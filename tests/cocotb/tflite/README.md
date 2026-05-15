# TFLite Micro 端到端模型测试

本目录下每个子目录都对应 Arm ML-zoo 上一个 INT8 量化模型，提供一个**端到端的 cocotb 测试**：把模型烧进 RvvCoreMini 的内存、用真实参考输入跑一次推理、把输出读回来与 ML-zoo 给的参考输出逐字节比对。

## 1. 当前覆盖的模型

| 子目录 | 模型 (`.tflite`) | 输入形状 | 输出形状 | 主要算子 | 任务 |
|---|---|---|---|---|---|
| `dnn_small_int8/` | `dnn_s_quantized.tflite` | `(1, 490)` | `(1, 12)` | `FullyConnected`, `Reshape`, `Softmax` | KWS（纯 FC baseline） |
| `ds_cnn_small_int8/` | `ds_cnn_s_quantized.tflite` | `(1, 49, 10, 1)` | `(1, 12)` | `Conv2D`, `DepthwiseConv2D`, `AveragePool2D`, `FullyConnected`, `Relu`, `Reshape`, `Softmax` | KWS |
| `ad_small_int8/` | `ad_small_int8.tflite` | `(1, 32, 32, 1)` | `(1, 8)` | `Conv2D`, `DepthwiseConv2D`, `AveragePool2D`, `Relu6`, `Reshape` | 异常检测（MicroNet Small） |
| `kws_micronet_small_int8/` | `kws_micronet_s.tflite` | `(1, 49, 10, 1)` | `(1, 12)` | `Conv2D`, `DepthwiseConv2D`, `AveragePool2D`, `Relu6`, `Reshape` | KWS（MicroNet Small） |
| `micronet_vww2_int8/` | `vww2_50_50_INT8.tflite` | `(1, 50, 50, 1)` | `(1, 2)` | `Conv2D`, `DepthwiseConv2D`, `AveragePool2D`, `Add`, `Pad`, `Relu6`, `Reshape` | Visual Wake Words |
| `rnn_noise_int8/` | `rnnoise_INT8.tflite` | 4 个 int8 张量（main+3 GRU 状态） | 5 个 int8 张量 | `Add`, `Concatenation`, `Dequantize`, `FullyConnected`, `Logistic`, `Mul`, `Pack`, `Quantize`, `Relu`, `Reshape`, `Split`, `SplitV`, `Sub`, `Tanh`, `Unpack` | 噪声抑制 |

> 任意时刻新增的模型只要符合本文档第 3 节的「目录结构 + 三件套」约定即可工作。

---

## 2. 目录结构（每个模型一致）

```
<model_name>/
  BUILD                       # bazel 目标定义
  run_<model_name>.cc         # 设备端 RISC-V 程序：注册算子 + 跑 Invoke
  cocotb_<model_name>.py      # 主机端 cocotb 测试：装载 ELF、写输入、读输出、比对
  models/
    <model>.tflite            # 模型权重文件
    definition.yaml           # ML-zoo 元数据（输入/输出形状、算子列表、benchmark）
    README.md                 # ML-zoo 自带说明
    get_class_labels.sh
  test_data/
    input_0.npy               # ML-zoo 提供的参考输入（int8）
    expected_output_0.npy     # ML-zoo 提供的参考输出（int8）
```

`rnn_noise_int8` 是个例外：它有 4 个输入 / 5 个输出，所以 `test_data/` 下有多个 `.npy`（`main_input_int8_0.npy`、`vad_gru_prev_state_int8_0.npy`、`Identity_*_int8_0.npy` 等），并且 `cocotb_rnn_noise_int8.py` 走「按 tensor 字节大小派发到对应符号」的多输入/多输出协议（详见 `rnn_noise_int8/run_rnn_noise_int8.cc:64-74`）。

---

## 3. 三个文件每一份要写什么

### 3.1 `BUILD`

模板（以 `kws_micronet_small_int8` 为例）：

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

# 1) 把 .tflite 二进制嵌成 C 数组
generate_cc_arrays(name = "kws_micronet_s_cc", src = "models/kws_micronet_s.tflite", out = "kws_micronet_s.cc")
generate_cc_arrays(name = "kws_micronet_s_h", src = "models/kws_micronet_s.tflite", out = "kws_micronet_s.h")

# 2) 设备端 ELF
coralnpu_v2_binary(
    name = "run_kws_micronet_small_int8_binary",
    srcs = ["run_kws_micronet_small_int8.cc", ":kws_micronet_s_cc"],
    hdrs = [":kws_micronet_s_h"],
    dtcm_size_kbytes = 1024,         # 一律 highmem
    itcm_size_kbytes = 1024,
    visibility = ["//visibility:public"],
    deps = [
        # 按模型用到的算子挑选；下面这些是常用集合
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

# 3) cocotb 测试目标
cocotb_test_suite(
    name = "cocotb_kws_micronet_small_int8",
    simulators = ["verilator", "vcs"],
    testcases = ["core_mini_rvv_kws_micronet_small_int8"],
    testcases_vname = "KWS_MICRONET_SMALL_INT8_TESTCASES",
    tests_kwargs = {
        "waves": False,
        "tags": ["manual"],          # 真实模型推理太慢，CI 默认不跑，需要手动触发
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

要点：

- **`tags = ["manual"]`**：所有模型测试都打这个标签，`bazel test //...` 默认不会触发；只有显式拉这个 target 才跑。原因是 verilator 跑完整推理基本都要数十分钟到几小时，会拖垮 CI。
- **`size = "enormous"`** + 调用方手动加 `--test_timeout`：bazel `enormous` 默认上限 3600 s，对 KWS-MicroNet（≈140 M cycle）这一档 verilator fastbuild 也不一定够，常配 `--test_timeout=7200`。
- **`dtcm_size_kbytes = 1024 / itcm_size_kbytes = 1024`**：必须用 highmem 镜像，否则模型常量 + tensor arena 装不下。对应 `hdl_toplevel = "RvvCoreMiniHighmemAxi"`。
- **`generate_cc_arrays`** 自动把 `.tflite` 文件嵌成 `g_<basename>_model_data` 这个 C 符号（按文件名生成，不是 target 名）。例如 `kws_micronet_s.tflite` → `g_kws_micronet_s_model_data`，写在 `run_*.cc` 里。

### 3.2 `run_<model>.cc`

设备端程序的骨架（单输入/单输出版，最常见）：

```cpp
extern "C" {
int8_t  inference_status = -1;                                 // 主机端轮询
char    inference_status_message[31] __attribute__((section(".data"), aligned(16)));
int8_t  inference_input [kInputBytes]  __attribute__((section(".data"), aligned(16)));
int8_t  inference_output[kOutputBytes] __attribute__((section(".data"), aligned(16)));

constexpr size_t kTensorArenaSize = 512 * 1024;                // 视模型大小调整
uint8_t tensor_arena[kTensorArenaSize] __attribute__((section(".data"), aligned(16)));
}

int main() {
  std::strncpy(inference_status_message, "Started", 31);
  const tflite::Model* model = tflite::GetModel(g_<basename>_model_data);
  if (model->version() != TFLITE_SCHEMA_VERSION) { /* msg + return -1 */ }

  tflite::MicroMutableOpResolver<N> op_resolver;
  // op_resolver.AddConv2D(coralnpu_v2::opt::litert_micro::Register_CONV_2D());      // 优化版
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

约定：

- **每个全局符号都是 `extern "C"` + 显式 `section(".data")` + `aligned(16)`**。cocotb 端通过名字定位地址再读写，必须保留 ABI 名（无 mangling）。
- **`inference_status` 为 0 表示成功**，主机端先看它再决定要不要继续比对输出。
- **`inference_status_message` 31 字节**，主机端打印失败原因方便定位（"Bad model schema version" / "Error during AllocateTensors" / "Error during Invoke" / 等）。
- **arena 大小**按经验：纯 FC 模型 256 KB；DS-CNN/KWS/AD 这一档 512 KB 足矣；MicroNet Small / VWW-2 用 768 KB ~ 1 MB；RNN-Noise 1 MB（多输入需要常驻）。最初先放 1 MB（`section(".extdata")`）跑通，再按实际占用收紧。
- **`MicroMutableOpResolver<N>` 的 N 必须等于实际 `Add*` 的个数**，以 `definition.yaml` 中 `operators: TensorFlow Lite:` 列表为准。
- **优先用 `coralnpu_v2::opt::litert_micro` 下的优化版算子**：`Register_CONV_2D` / `Register_DEPTHWISE_CONV_2D` / `Register_FULLY_CONNECTED`。其它（`Add`、`Pad`、`Relu6`、`AveragePool2D`、`Reshape` 等）直接用 TFLM 默认实现就够了。

### 3.3 `cocotb_<model>.py`

主机端测试骨架：

```python
import cocotb, numpy as np
from bazel_tools.tools.python.runfiles import runfiles
from coralnpu_test_utils.sim_test_fixture import Fixture

_RUNFILES_PREFIX = "coralnpu_hw/tests/cocotb/tflite/<model_name>/"

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

容差约定：

- **`max|diff| ≤ 1` LSB**：是 TFLM 在不同算子实现之间普遍要求的精度上限（量化舍入差异最多 1 LSB）。
- **`argmax` 一致**：分类任务再加这一条防止边界 LSB 翻盘改变 top-1。
- **回归类输出（如 RNN-Noise 的 `Identity_1` gains）**：可以放宽到 `≤ 2` LSB；现有 `cocotb_rnn_noise_int8.py:46` 就这么处理。
- **`timeout_cycles=2_000_000_000`**：跟 cycles 实际上限挂钩。可以先用 200 M 跑小模型，超时再调大。

---

## 4. 怎么跑

```bash
# 编译（不跑仿真）：验证 ELF + cocotb runner 能拉起来
bazel build //tests/cocotb/tflite/<model>:cocotb_<model>

# 跑端到端推理（推荐 highmem + 大 timeout）
bazel test //tests/cocotb/tflite/<model>:cocotb_<model>_core_mini_rvv_<model> \
           --test_output=streamed \
           --test_timeout=7200
```

`bazel test //tests/cocotb/tflite/...` 不会触发任何模型测试，因为它们都打了 `tags = ["manual"]`。要想批量跑，加 `--test_tag_filters=-manual` 之外的覆盖或显式列举。

---

## 5. 模型实测数据（供选 timeout / 排查瓶颈参考）

| 模型 | `.tflite` 大小 | cycles | 仿真时间 (ns) | cycles / KB |
|---|---:|---:|---:|---:|
| `dnn_small_int8` | 81 KB | 281 575 | 352 052 | 3.5 K |
| `ds_cnn_small_int8` | 46 KB | 29 186 100 | 36 482 727 | 635 K |
| `ad_small_int8` | 246 KB | 35 669 862 | 44 587 471 | 145 K |
| `micronet_vww2_int8` | 273 KB | 15 240 404 | 19 050 765 | 56 K |
| `kws_micronet_small_int8` | 111 KB | 140 045 588 | 175 057 087 | 1262 K |

要点：**`.tflite` 文件大小只反映「参数量」，推理 cycles ≈「参数量 × 平均特征图面积」**。文件最小的 `ds_cnn_small_int8` 跟最大的 `micronet_vww2_int8` cycles 数差不多；最贵的反而是 111 KB 的 `kws_micronet_small_int8`，因为它的 49×10 时频图基本不被下采样，每个权重都被反复乘几百次。

---

## 6. 与 `sw/opt/litert-micro/` RVV 优化算子的关系

本目录每个测试都同时验证 `@/home/lizeyu/eng/coralnpu/sw/opt/litert-micro/conv.cc`、`depthwise_conv.cc`、`fully_connected.cc` 等里的 RVV 优化 kernel：

- `Conv2D_1x1` / `Conv2D_3x3` / `Conv2D_4x4` / `Conv2D_Generic`：MicroNet 系列（AD / KWS / VWW-2）+ DS-CNN 都会大量命中。
- `Conv2D_Generic`（任意 `(kH, kW)` 的 OC 向量化 fallback）：专门解决 KWS-MicroNet 的 10×4 stem 这类「不属于 1×1/3×3/4×4 但 MAC 数巨大」的情形。
- `FullyConnected`：DNN-Small 全程用，RNN-Noise 的 GRU 内部 dense 也走这条。
- `Logistic`（int8 LUT）：RNN-Noise 的 sigmoid。

任何一处 kernel 改动都建议至少跑一次 `dnn_small_int8` + `ds_cnn_small_int8` 做快速回归；KWS / AD / VWW-2 / RNN-Noise 这一档跑一次比较慢，作为大改动的全量回归即可。

---

## 7. 添加新模型的清单

1. 在本目录建 `<new_model>/`，把 `.tflite` 放到 `models/`，把 `input_0.npy` / `expected_output_0.npy` 放到 `test_data/`。
2. 抄一个最接近的现有模型做模板（建议：纯 FC → `dnn_small_int8`；CNN+RELU6 → `ad_small_int8`；CNN+ADD/PAD → `micronet_vww2_int8`；多 IO/RNN → `rnn_noise_int8`）。
3. 改 `BUILD`：模型文件名、target 名、`MicroMutableOpResolver<N>` 的 `N`、deps（按算子）、test 名。
4. 改 `run_<model>.cc`：`g_<basename>_model_data`、`kInputBytes` / `kOutputBytes`、注册算子集合、arena 大小。
5. 改 `cocotb_<model>.py`：`_RUNFILES_PREFIX`、ELF / npy 文件名、shape assert、`timeout_cycles`、容差。
6. `bazel build` 先确认 ELF 和 cocotb runner 都能产出。
7. `bazel test` 跑一次端到端，按 §5 表格的「同形状/同算子集」邻居模型预估 cycles 设 `--test_timeout`。
