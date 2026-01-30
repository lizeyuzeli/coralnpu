# tape_out

这个目录用于“流片相关工作”的工程化沉淀（接口裁剪、FPGA上板验证、系统联调、脚本/约束/文档等）。

## 目标系统形态（你描述的最终芯片）

- **对外接口**：
  - SPI：外部 master 对 CoralNPU SoC 配置/控制
  - LVDS：承载数据通路（上层暴露 AXI-Stream，与 LVDS 供应商数字部分对接；物理层用 Xilinx LVDS/IO 资源补全）
- **SoC 内部保留**：
  - `coralnpu_soc` 的 SPI 口（现有工程已存在）
  - `coralnpu_soc` 的 AXI master 口（现有工程已对接 DDR，后续将变为“AXI-MM ⇄ AXI-Stream ⇄ LVDS”隧道）

## 两阶段 FPGA 验证（建议产物）

### 阶段 1：单板验证 CoralNPU 功能正确性

目标：尽量贴近真实系统地验证 NPU + RISC-V 子系统能跑通软件栈（至少可运行一个端到端的推理 demo 或等价负载）。

建议产物：
- 可综合 bitstream（含 DDR/时钟/复位/IO 约束）
- 可烧录/启动的 ROM/镜像加载流程（SPI 或 JTAG/BRAM init 等）
- 基本可观测性：UART 日志、halt/fault 指示、必要时加 ILA

### 阶段 2：双 FPGA 验证最终系统形态（LVDS）

目标：用两块 FPGA 逼近“最终芯片外设形态”。

- FPGA-A：CoralNPU（SPI + LVDS/AXIS）
- FPGA-B：LVDS 对端（AXIS ⇄ AXI-MM/DDR/外设模型），用于模拟系统里的“外部世界”

## 现状速览（当前工程已有）

- 现有 SoC 顶层：`fpga/rtl/coralnpu_soc.sv` 已经暴露 SPI 以及两组 DDR AXI（ctrl/mem）接口。
- 现有板级顶层：
  - `fpga/rtl/chip_verilator.sv`：Verilator 仿真顶层，SPI 由 DPI master 驱动。
  - `fpga/rtl/chip_nexus.sv`：Xilinx FPGA（VU13P）bitstream 顶层，含 DDR4 MIG 连接与约束示例。
- 构建体系：`fpga/BUILD` 里用 `fusesoc_build` 同时支持仿真（verilator）与综合（vivado）。
- **目前工程内没有 LVDS/AXI-stream 相关实现**（需要你在阶段 2 引入/开发）。

下一步建议从 [tape_out/fpga/README.md](fpga/README.md) 开始落地阶段 1 的可跑通链路。
