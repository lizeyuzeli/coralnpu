`ifndef RVV_CONFIG_SVH
`define RVV_CONFIG_SVH

// config for multi-dispatch
`define DISPATCH3
//`define DISPATCH2

// FP ISA 
//`define ZVE32F_ON
//`define ZVFBFWMA_ON

// LSU interaction
// Disable until scalar side supports NOHANDSHAKE
// `define UNMK_USCS_LOAD_NOHANDSHAKE

// ARBITER
`define ARBITER_ON

// FAULT TOLERANCE (DMR instruction duplication) — default OFF (single switch).
// 取消下一行注释即开启 FT；复用同一套 cocotb 测试，无需给每个 test 加 +define+。
// OFF 时所有 `ifdef FAULT_TOLERANT_ON 块均不存在，编译产物与基线逐位一致 (INV-1)。
//`define FAULT_TOLERANT_ON

// FT 瞬态故障原地重跑上限 K (达上限回退现有 trap_flush_rvv 全清兜底，见 INV-5)。
// `ifndef 兜底定义默认值，可被外部 +define+FT_RETRY_MAX=N 覆盖以微调。
`ifndef FT_RETRY_MAX
  `define FT_RETRY_MAX 3
`endif

// FT 注错自检 — default OFF。仅在 FAULT_TOLERANT_ON 时有意义。
// ON 时 ROB 对每条 is_ft entry 在首次比对前强制把一份结果改错，且仅一次
// (per-entry「已注错」标记随 entry 生命周期清除)，逼其走一遍
// mismatch→回滚→重跑→恢复，逐条验证「复制+比较+回滚」整链。
// 必须「仅一次」：否则每次重跑都注错→永远 mismatch→撞 K→trap→回归反挂；
// 故依赖 FT_RETRY_MAX>1 才有恢复余量 (见 plan Stage1【注意点】)。
//`define FT_INJECT_ON

`endif // RVV_CONFIG_SVH
