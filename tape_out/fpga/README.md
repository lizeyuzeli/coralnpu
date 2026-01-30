# FPGA 验证落地建议（阶段 1 → 阶段 2）

## 阶段 1：先跑通 CoralNPU 自身（单板）

### 1) 复用现有“参考板级工程”

工程里已经有一套可综合的参考顶层（Nexus/VU13P）：
- 顶层 RTL：`fpga/rtl/chip_nexus.sv`
- 约束：`fpga/pins_nexus.xdc`
- Vivado hook：`fpga/vivado_setup_hooks.tcl`
- Bazel/FuseSoC 入口：`fpga/BUILD` 里的 `build_chip_nexus_bitstream*`

你要做的是把这套“板级层”移植到你的两块板：
- ZU19EG（Zynq UltraScale+ MPSoC）
- HuaPro P1（VU440 + DDR/PCIe 子卡）

### 2) 先选择最容易成功的板

建议顺序：
1. **HuaPro P1（VU440 + 外接 DDR 子卡）**：更像现有 `chip_nexus`（纯 PL + MIG/DDR），移植成本通常更低。
2. ZU19EG：如果想用 PS DDR/PS 作为系统管理，会涉及 PS-PL AXI、启动链路、时钟复位域规划等，系统工程更大。

### 3) “完整推理任务”可行性拆分

把“跑完整 NN 推理”拆成 3 个递进里程碑，避免一开始就卡死在软件栈：
- M1：基础可观测性：上电、时钟/复位、UART 打印、SPI 可访问寄存器/内存。
- M2：算子/内核级验证：跑现有示例（例如 `examples/` 或 SoC 侧 demo），确认计算正确。
- M3：端到端推理：引入模型、输入/输出搬运、性能与稳定性。

注：现有仿真入口可作为软件与寄存器模型的“金参考”：
- `fpga/rtl/chip_verilator.sv` + `fpga/main.cc`

## 阶段 2：双 FPGA + LVDS（最终形态逼近）

现有 `coralnpu_soc` 对外是 **AXI memory-mapped** 风格（DDR ctrl/mem）。你想对接 LVDS 供应商给的 **AXI-Stream**，中间需要一个“协议/语义转换层”。

推荐的工程路径（先能联调再谈最优）：
1. 定义一套 LVDS 链路上传输的 packet/transaction 语义（例如把 AXI-MM 读写封装成 request/response stream）。
2. FPGA-A：实现 `axi_mm ⇄ axis` 的桥（本质上是一个小型的 DMA/transaction tunnel）。
3. FPGA-B：实现 `axis ⇄ axi_mm` 对端，再对接 DDR/外设（可先用 BRAM model，后用 DDR）。
4. 等两端自洽后，再替换物理层为 Xilinx LVDS IP，并接入供应商数字部分。

## 你接下来我建议我来做的具体事（可选）

如果你同意，我可以继续在工程里：
- 先为 HuaPro P1 / ZU19EG 各自建立一个新的 `chip_<board>.sv` + `<board>.xdc` + 对应 `.core`，并把 `fpga/BUILD` 增加对应的 `fusesoc_build` 目标（先让 Vivado 能跑到 bitstream）。
- 同时补一份阶段 1 的 bring-up checklist（时钟、复位、SPI/UART、自检程序加载）。

你先告诉我：
- ZU19EG 板子上你希望用 **PS DDR** 还是 **PL DDR(MIG)**？
- HuaPro P1 的 DDR/PCIe 子卡你更关心 DDR 还是 PCIe（阶段 1 我建议优先 DDR）。
