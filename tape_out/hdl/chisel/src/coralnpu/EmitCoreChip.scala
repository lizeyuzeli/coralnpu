// Copyright 2025 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Emit objects for the tape-out top (Core_Chip) and the verification top
// (Core_Axi_Chip). Mirrors the option set of `EmitCore` in
// hdl/chisel/src/coralnpu/Core.scala but always emits the chip variant.

package coralnpu

import java.io.{File, FileOutputStream}
import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Paths, StandardOpenOption}
import java.util.zip.{ZipEntry, ZipOutputStream}
import scala.collection.mutable.Stack

import circt.stage.ChiselStage

object EmitCoreChipCommon {
  case class Cfg(
      moduleName: String,
      finalModuleName: String,
      memoryRegions: Seq[MemoryRegion],
      chiselArgs: List[String],
      targetDir: Option[String],
      bootAddr: BigInt,
      params: Parameters,
      // Tape-out top: false strips `core_gated_clk` / `core_sync_aresetn` IO.
      // Verification top: forced true inside `Core_Axi_Chip`.
      exposeVerifyPorts: Boolean,
      // false => SV BlackBox `AsyncFIFO_RTL`, true => rocket `AsyncQueue`.
      useChiselAsyncQueue: Boolean,
      // Verification-only: divides aclk by 2^lvdsClkDivLog2 for `lvds_clk`.
      // Ignored for `Core_Chip` (no internal clock generation there).
      lvdsClkDivLog2: Int,
  )

  def parse(args: Array[String]): Cfg = {
    val p = new Parameters
    var moduleName = "Core"
    var chiselArgs = List[String]()
    var targetDir: Option[String] = None
    var bootAddr: BigInt = 0x10000000L
    var exposeVerifyPorts: Boolean = false
    var useChiselAsyncQueue: Boolean = true
    var lvdsClkDivLog2: Int = 1

    for (arg <- args) {
      if (arg.startsWith("--enableFetchL0")) {
        p.enableFetchL0 = arg.split("=")(1).toBoolean
      } else if (arg.startsWith("--moduleName")) {
        moduleName = arg.split("=")(1)
      } else if (arg.startsWith("--fetchDataBits")) {
        p.fetchDataBits = arg.split("=")(1).toInt
      } else if (arg.startsWith("--enableRvv")) {
        p.enableRvv = arg.split("=")(1).toBoolean
      } else if (arg.startsWith("--enableFloat")) {
        p.enableFloat = arg.split("=")(1).toBoolean
      } else if (arg.startsWith("--enableVerification")) {
        p.enableVerification = arg.split("=")(1).toBoolean
      } else if (arg.startsWith("--lsuDataBits")) {
        p.lsuDataBits = arg.split("=")(1).toInt
      } else if (arg.startsWith("--itcmSizeKBytes")) {
        p.itcmSizeKBytes = arg.split("=")(1).toInt
      } else if (arg.startsWith("--dtcmSizeKBytes")) {
        p.dtcmSizeKBytes = arg.split("=")(1).toInt
      } else if (arg.startsWith("--bootAddr")) {
        val v = arg.split("=")(1)
        bootAddr = if (v.startsWith("0x") || v.startsWith("0X"))
            BigInt(v.drop(2), 16) else BigInt(v)
      } else if (arg.startsWith("--exposeVerifyPorts")) {
        exposeVerifyPorts = arg.split("=")(1).toBoolean
      } else if (arg.startsWith("--useChiselAsyncQueue")) {
        useChiselAsyncQueue = arg.split("=")(1).toBoolean
      } else if (arg.startsWith("--lvdsClkDivLog2")) {
        lvdsClkDivLog2 = arg.split("=")(1).toInt
      } else if (arg.startsWith("--target-dir")) {
        targetDir = Some(arg.split("=")(1))
      } else {
        chiselArgs = chiselArgs :+ arg
      }
    }

    val finalModuleName = if (
        p.itcmSizeKBytes == Parameters.itcmSizeKBytesDefault &&
        p.dtcmSizeKBytes == Parameters.dtcmSizeKBytesDefault) {
      moduleName
    } else if (
        p.itcmSizeKBytes == Parameters.itcmSizeKBytesHighmem &&
        p.dtcmSizeKBytes == Parameters.dtcmSizeKBytesHighmem) {
      s"${moduleName}Highmem"
    } else {
      s"${moduleName}_ITCM${p.itcmSizeKBytes}KB_DTCM${p.dtcmSizeKBytes}KB"
    }

    val memoryRegions = if (
        p.itcmSizeKBytes == Parameters.itcmSizeKBytesDefault &&
        p.dtcmSizeKBytes == Parameters.dtcmSizeKBytesDefault) {
      MemoryRegions.default
    } else {
      MemoryRegions.highmem(p.itcmSizeKBytes, p.dtcmSizeKBytes)
    }

    p.m = memoryRegions
    Cfg(moduleName, finalModuleName, memoryRegions, chiselArgs, targetDir,
      bootAddr, p, exposeVerifyPorts, useChiselAsyncQueue, lvdsClkDivLog2)
  }

  def emit[T <: chisel3.RawModule](
      cfg: Cfg,
      build: () => T,
  ): Unit = {
    val firtoolOpts = Array(
      "--lowering-options=disallowLocalVariables,locationInfoStyle=none",
      "-enable-layers=Verification",
    )

    lazy val core = build()
    val systemVerilogSource = ChiselStage.emitSystemVerilog(
      core, cfg.chiselArgs.toArray, firtoolOpts)
    val resourcesSeparator =
      "// ----- 8< ----- FILE \"firrtl_black_box_resource_files.f\" ----- 8< -----"
    val strippedVerilogSource = systemVerilogSource.split(resourcesSeparator)(0)

    val coreName = core.name
    val header_str = EmitParametersHeader(cfg.params)

    cfg.targetDir match {
      case Some(targetDir) => {
        lazy val core2 = build()
        ChiselStage.emitSystemVerilogFile(
          core2,
          cfg.chiselArgs.toArray ++ Array(
            "--split-verilog", "--target-dir", targetDir),
          firtoolOpts)
        val zip = new ZipOutputStream(new FileOutputStream(
          targetDir + "/" + coreName + ".zip"))
        val dirStack = new Stack[File](1)
        dirStack.push(new File(targetDir))
        while (!dirStack.isEmpty) {
          val dir = dirStack.pop()
          val files = dir.listFiles
          files.foreach { name =>
            if (name.isDirectory()) {
              dirStack.push(name)
            } else {
              val zipName = name.getPath().replace(targetDir + "/", "")
              zip.putNextEntry(new ZipEntry(zipName))
              zip.write(Files.readAllBytes(Paths.get(name.getPath())))
              zip.closeEntry()
            }
          }
        }
        zip.close()
        Files.write(
          Paths.get(targetDir + "/V" + coreName + "_parameters.h"),
          header_str.getBytes(StandardCharsets.UTF_8),
          StandardOpenOption.CREATE)
        Files.write(
          Paths.get(targetDir + "/" + coreName + ".sv"),
          strippedVerilogSource.replace("exclude_file", "exclude_module")
            .getBytes(StandardCharsets.UTF_8),
          StandardOpenOption.CREATE)
      }
      case None => {
        print(strippedVerilogSource)
      }
    }
  }
}

object EmitCore_Chip extends App {
  val cfg = EmitCoreChipCommon.parse(args)
  EmitCoreChipCommon.emit(cfg,
    () => new Core_Chip(
      cfg.params, cfg.finalModuleName, cfg.bootAddr,
      exposeVerifyPorts = cfg.exposeVerifyPorts,
      useChiselAsyncQueue = cfg.useChiselAsyncQueue,
    ))
}

object EmitCore_Axi_Chip extends App {
  val cfg = EmitCoreChipCommon.parse(args)
  EmitCoreChipCommon.emit(cfg,
    () => new Core_Axi_Chip(
      cfg.params, cfg.finalModuleName, cfg.bootAddr,
      lvdsClkDivLog2 = cfg.lvdsClkDivLog2,
      useChiselAsyncQueue = cfg.useChiselAsyncQueue,
    ))
}

object EmitCore_Jtag_Chip extends App {
  val cfg = EmitCoreChipCommon.parse(args)
  EmitCoreChipCommon.emit(cfg,
    () => new Core_Jtag_Chip(
      cfg.params, cfg.finalModuleName, cfg.bootAddr,
      lvdsClkDivLog2 = cfg.lvdsClkDivLog2,
      useChiselAsyncQueue = cfg.useChiselAsyncQueue,
    ))
}
