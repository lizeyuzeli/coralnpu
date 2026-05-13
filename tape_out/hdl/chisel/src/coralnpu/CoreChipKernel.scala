// CoreChipKernel: tape-out variant of CoreAxi's internal logic.
//
// Identical to CoreAxi internally except:
//   - The AxiSlave / IBus2Axi / DBus2Axi modules and their AXI ports are
//     removed. Instead the kernel exposes:
//       * fabricSlave (Flipped FabricIO) -- consumed by fabricMux as its
//         command source. Driven externally by the chip-side LVDS adapter.
//       * ibus (IBusIO)  -- routed from core's ibus when target is OUT of ITCM.
//       * ebus (EBusIO)  -- routed from core's ebus.
//     The LVDS adapter (LvdsAdapterChip) bridges these to/from the wire.
//     It MUST use `io.gated_clk` / `io.adapter_aresetn` (same as CSR /
//     fabricMux), not the raw `aclk` pin: RstSync gates `aclk` into `clk_o`
//     and CoreAxi's AxiSlave also lived in that domain.
//   - boot_addr is hardcoded at elaboration time via the `bootAddr` ctor arg.
//   - The DebugIO port from CoreAxi is dropped; core.io.debug is dontTouch'd
//     internally so synthesis can DCE the producing logic.
//   - The dm (DebugModuleIO) port is preserved; the tape-out top
//     (Core_Chip) hooks it up to a dmi_jtag instance. The verification top
//     (Core_Axi_Chip) ties it off harmlessly.

package coralnpu

import chisel3._
import chisel3.util._

import common._

class CoreChipKernel(
    p: Parameters,
    coreModuleName: String,
    bootAddr: BigInt = 0x10000000L,
) extends RawModule {
  override val desiredName = coreModuleName + "ChipKernel"
  val memoryRegions = p.m

  val io = IO(new Bundle {
    val aclk = Input(Clock())
    val aresetn = Input(AsyncReset())

    // Replacing axi_slave: external entity drives fabric commands in.
    val fabricSlave = Flipped(new FabricIO(p))
    // Replacing axi_master: kernel emits ibus / ebus traffic to the outside.
    val ibus = new IBusIO(p)
    val ebus = new EBusIO(p)

    // Status / interrupts / debug-module path (see file header for `dm`).
    val halted = Output(Bool())
    val fault  = Output(Bool())
    val wfi    = Output(Bool())
    val irq    = Input(Bool())
    val timer_irq    = Input(Bool())
    val software_irq = Input(Bool())

    val dm = new DebugModuleIO(p)
    val te = Input(Bool())

    // Post-RstSync gated core clock (matches fabricMux, TCM, CSR). Exposed so
    // LvdsAdapterChip / dmi_jtag at the tape-out top can share this domain
    // instead of `aclk` (ungated); otherwise fabricSlave is CDC-skewed.
    val gated_clk = Output(Clock())
    // Active-low reset in lockstep with `gated_clk` (same te mux as below).
    val adapter_aresetn = Output(Bool())
  })
  dontTouch(io)

  val rst_sync = Module(new RstSync())
  rst_sync.io.clk_i := io.aclk
  rst_sync.io.rstn_i := io.aresetn
  rst_sync.io.clk_en := true.B
  rst_sync.io.te := io.te

  io.gated_clk := rst_sync.io.clk_o
  io.adapter_aresetn := Mux(io.te, io.aresetn, rst_sync.io.rstn_o).asBool

  val global_reset = (!Mux(io.te, io.aresetn, rst_sync.io.rstn_o).asBool).asAsyncReset
  withClockAndReset(rst_sync.io.clk_o, global_reset) {

    // -------------------------------------------------------------------------
    // CSR (boot_addr is hardcoded here at elaboration time)
    // -------------------------------------------------------------------------
    val csr = Module(new CoreCSR(p))
    csr.io.internal := false.B
    csr.io.bootAddr := bootAddr.U(p.fetchAddrBits.W)

    val cg = Module(new ClockGate)
    cg.io.clk_i := rst_sync.io.clk_o
    cg.io.te := io.te

    // -------------------------------------------------------------------------
    // Debug Module + arbiter
    // -------------------------------------------------------------------------
    val dm = Module(new DebugModule(p))
    dontTouch(dm.io)
    val dmEnable = RegInit(false.B)
    dmEnable := true.B
    val dmReqArbiter = Module(new CoralNPURRArbiter(new DebugModuleReqIO(p), 2))
    dmReqArbiter.io.in(0) <> GateDecoupled(io.dm.req, dmEnable)
    dmReqArbiter.io.in(1) <> GateDecoupled(csr.io.debug.req, dmEnable)

    val inflight = Module(new Queue(UInt(1.W), 1))

    dm.io.ext.req.bits := dmReqArbiter.io.out.bits
    dm.io.ext.req.valid := dmReqArbiter.io.out.valid && inflight.io.enq.ready
    dmReqArbiter.io.out.ready := dm.io.ext.req.ready && inflight.io.enq.ready

    inflight.io.enq.bits := dmReqArbiter.io.chosen
    inflight.io.enq.valid := dmReqArbiter.io.out.valid && dm.io.ext.req.ready

    val rspId = inflight.io.deq.bits
    inflight.io.deq.ready := dm.io.ext.rsp.fire

    csr.io.debug.rsp.bits := dm.io.ext.rsp.bits
    io.dm.rsp.bits := dm.io.ext.rsp.bits

    csr.io.debug.rsp.valid := dm.io.ext.rsp.valid && inflight.io.deq.valid && (rspId === 1.U)
    io.dm.rsp.valid := dm.io.ext.rsp.valid && inflight.io.deq.valid && (rspId === 0.U)

    dm.io.ext.rsp.ready := inflight.io.deq.valid && Mux(rspId === 1.U,
      csr.io.debug.rsp.ready, io.dm.rsp.ready)

    // -------------------------------------------------------------------------
    // Core
    // -------------------------------------------------------------------------
    val core_reset = Mux(io.te,
      (!io.aresetn.asBool).asAsyncReset,
      (csr.io.reset || dm.io.ndmreset).asAsyncReset)
    val core = withClockAndReset(cg.io.clk_o, core_reset) { Core(p, coreModuleName) }

    val irq_reg = RegNext(io.irq, false.B)
    val timer_irq_reg = RegNext(io.timer_irq, false.B)
    val software_irq_reg = RegNext(io.software_irq, false.B)

    cg.io.enable := irq_reg || timer_irq_reg || software_irq_reg ||
      (!csr.io.cg && !core.io.wfi) || dm.io.haltreq(0)
    io.halted := core.io.halted
    io.fault := core.io.fault
    io.wfi := core.io.wfi
    core.io.irq := irq_reg || dm.io.haltreq(0)
    core.io.timer_irq := timer_irq_reg
    core.io.software_irq := software_irq_reg
    csr.io.halted := core.io.halted
    csr.io.fault := core.io.fault
    csr.io.coralnpu_csr := core.io.csr.out
    core.io.debug_req := true.B
    core.io.csr.in.value(0) := csr.io.pcStart
    for (i <- 1 until p.csrInCount) {
      core.io.csr.in.value(i) := 0.U
    }
    // Drop the DebugIO port from CoreAxi: synthesis will DCE the producers.
    dontTouch(core.io.debug)
    core.io.dflush.ready := true.B
    core.io.iflush.ready := true.B

    core.io.dm.debug_req := dm.io.haltreq(0)
    core.io.dm.resume_req := dm.io.resumereq(0)
    dm.io.resumeack(0) := !core.io.dm.debug_mode && RegNext(core.io.dm.debug_mode, false.B)
    dm.io.halted(0) := core.io.dm.debug_mode
    dm.io.running(0) := !core.io.dm.debug_mode
    dm.io.havereset(0) := false.B
    core.io.dm.csr := dm.io.csr
    core.io.dm.csr_rs1 := dm.io.csr_rs1
    dm.io.csr_rd := core.io.dm.csr_rd
    dm.io.scalar_rd <> core.io.dm.scalar_rd
    dm.io.scalar_rs <> core.io.dm.scalar_rs
    if (p.enableFloat) {
      dm.io.float_rd.get <> core.io.dm.float_rd.get
      dm.io.float_rs.get <> core.io.dm.float_rs.get
    }

    // -------------------------------------------------------------------------
    // TCMs (3 ports: ibus/dbus | fabric | dm)
    // -------------------------------------------------------------------------
    val tcmPortCount = 3

    val itcmSizeBytes: Int = 1024 * p.itcmSizeKBytes
    val itcmSubEntryWidth = 8
    val itcmWidth = p.axi2DataBits
    val itcmEntries = itcmSizeBytes / (itcmWidth / 8)
    val itcm = Module(new TCM128(itcmSizeBytes, itcmSubEntryWidth, memoryRegions(0).memStart))
    dontTouch(itcm.io)
    val itcmWrapper = Module(new SRAM(p, log2Ceil(itcmEntries)))
    itcm.io.addr := itcmWrapper.io.sram.address
    itcm.io.enable := itcmWrapper.io.sram.enable
    itcm.io.write := itcmWrapper.io.sram.isWrite
    itcm.io.wdata := itcmWrapper.io.sram.writeData
    itcm.io.wmask := itcmWrapper.io.sram.mask
    itcmWrapper.io.sram.readData := itcm.io.rdata
    val itcmArbiter = Module(new FabricArbiter(p, tcmPortCount))
    itcmArbiter.io.port <> itcmWrapper.io.fabric

    assert(memoryRegions(0).memType === MemoryRegionType.IMEM)
    val inItcm = memoryRegions(0).contains(core.io.ibus.addr)

    itcmArbiter.io.source(0).readDataAddr := MakeValid(
      core.io.ibus.valid && inItcm, core.io.ibus.addr)
    itcmArbiter.io.source(0).writeDataAddr :=
      MakeInvalid(UInt(p.axi2AddrBits.W))
    itcmArbiter.io.source(0).writeDataBits := 0.U
    itcmArbiter.io.source(0).writeDataStrb := 0.U

    // External (LVDS) ibus port: only carry off-ITCM ibus traffic.
    io.ibus.valid := core.io.ibus.valid && !inItcm
    io.ibus.addr := core.io.ibus.addr

    val inItcmReg = RegNext(inItcm, true.B)
    core.io.ibus.rdata := Mux(inItcmReg,
      itcmArbiter.io.source(0).readData.bits, io.ibus.rdata)
    core.io.ibus.ready := Mux(inItcm, true.B, io.ibus.ready)
    core.io.ibus.fault := io.ibus.fault

    // DTCM
    val dtcmSizeBytes: Int = 1024 * p.dtcmSizeKBytes
    val dtcmWidth = p.axi2DataBits
    val dtcmEntries = dtcmSizeBytes / (dtcmWidth / 8)
    val dtcmSubEntryWidth = 8
    val dtcm = Module(new TCM128(dtcmSizeBytes, dtcmSubEntryWidth, memoryRegions(1).memStart))
    dontTouch(dtcm.io)
    val dtcmWrapper = Module(new SRAM(p, log2Ceil(dtcmEntries)))
    dtcm.io.addr := dtcmWrapper.io.sram.address
    dtcm.io.enable := dtcmWrapper.io.sram.enable
    dtcm.io.write := dtcmWrapper.io.sram.isWrite
    dtcm.io.wdata := dtcmWrapper.io.sram.writeData
    dtcm.io.wmask := dtcmWrapper.io.sram.mask
    dtcmWrapper.io.sram.readData := dtcm.io.rdata
    val dtcmArbiter = Module(new FabricArbiter(p, tcmPortCount))
    dtcmArbiter.io.port <> dtcmWrapper.io.fabric
    dtcmArbiter.io.source(0).readDataAddr := MakeValid(
      core.io.dbus.valid && !core.io.dbus.write, core.io.dbus.addr)
    dtcmArbiter.io.source(0).writeDataAddr := MakeValid(
      core.io.dbus.valid && core.io.dbus.write, core.io.dbus.addr)
    dtcmArbiter.io.source(0).writeDataBits := core.io.dbus.wdata
    dtcmArbiter.io.source(0).writeDataStrb := core.io.dbus.wmask
    core.io.dbus.rdata := dtcmArbiter.io.source(0).readData.bits
    core.io.dbus.ready := true.B

    // -------------------------------------------------------------------------
    // FabricMux: source = external fabricSlave port (fed by LVDS adapter)
    // -------------------------------------------------------------------------
    val fabricMux = Module(new FabricMux(p, memoryRegions))
    fabricMux.io.ports(0) <> itcmArbiter.io.source(1)
    fabricMux.io.periBusy(0) := itcmArbiter.io.fabricBusy(1)
    fabricMux.io.ports(1) <> dtcmArbiter.io.source(1)
    fabricMux.io.periBusy(1) := dtcmArbiter.io.fabricBusy(1)
    fabricMux.io.ports(2) <> csr.io.fabric
    fabricMux.io.periBusy(2) := false.B

    itcmArbiter.io.source(2) <> dm.io.itcm
    dtcmArbiter.io.source(2) <> dm.io.dtcm

    // External fabric source (replaces AxiSlave's fabric output).
    fabricMux.io.source <> io.fabricSlave

    // -------------------------------------------------------------------------
    // ebus -> external port (replaces DBus2Axi).
    // -------------------------------------------------------------------------
    io.ebus.dbus <> core.io.ebus.dbus
    io.ebus.fault <> core.io.ebus.fault
    // ebus.internal is an Output of EBusIO; route from core.
    io.ebus.internal := core.io.ebus.internal
  }
}
