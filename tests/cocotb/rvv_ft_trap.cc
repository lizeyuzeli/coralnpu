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

// Reporting path for an unrecoverable vector-unit error.
//
// The fault-tolerant vector back end retries a mismatching instruction in place
// and, once the retry budget is exhausted, gives up: it raises trap_flag on the
// retiring uop and flushes itself. This program checks that the scalar core is
// actually told -- that the exception is delivered with cause 19 (hardware
// error) and mepc pointing at the vector instruction that failed, rather than
// the vector instruction quietly producing a value it never computed.
//
// The trap can only occur in a build with FAULT_TOLERANT_ON + FT_INJECT_ON +
// FT_INJECT_PERSIST, so both outcomes are legal and the program distinguishes
// them for the caller instead of guessing:
//   ft_trap_observed == 0 -> no trap; the build has no injection (or no FT).
//   ft_trap_observed == 1 -> trap delivered, and mcause/mepc/mtval were correct.
// A wrong mcause or a wrong mepc is not recorded as either: it takes the ebreak
// below, so it cannot be mistaken for the no-trap case.
//
// It also checks ftstatus (0x7c8), the sticky record of the same event. The
// trap is an interrupt in time -- it is delivered once and then it is over --
// so a status bit that outlives it is what lets anything other than the handler
// itself (a later health check, a supervisor, a post-mortem) learn that the
// vector unit failed. Both halves are checked here: that the bit is set on
// arrival, and that software can clear it, since a bit that cannot be cleared
// reports every future error as already-known.
//
// It also exercises ftctl (0x7cb), the run-time enable. Duplication costs power
// and issue slots, so software has to be able to decline it; the register is only
// worth having if writing it reaches the duplication logic, and a register that
// merely reads back what was written proves nothing about that. So the check is
// behavioural, not a read-back: under persistent injection, the same vector
// instruction that traps with FT enabled must NOT trap with FT disabled, because
// with no second copy there is no comparison to fail. That is the one experiment
// that distinguishes "the enable is wired to dispatch" from "the enable is a
// register nobody reads".
//
// Finally it reads the two event counters, ftdmrcnt (0x7ca) and ftcecnt (0x7c9).
// These are the other half of the picture, and the more interesting half: a
// retry that succeeds is invisible in every other way -- the program computes
// the right answer and nothing is reported -- so without a counter, a run in
// which DMR caught and repaired an error is indistinguishable from a run in
// which nothing ever went wrong. That distinction is the entire claim the time
// redundancy makes, so the FT_INJECT_ON build below (inject, recover, no trap)
// is the case that matters most here, not the trapping one.

#include <riscv_vector.h>
#include <cstdint>

extern "C" {

// 0 = the vector instruction completed without trapping.
// 1 = the trap arrived and every field checked out.
volatile uint32_t ft_trap_observed = 0;
// Reported for diagnosis only; not part of the pass criterion. vstart is what a
// handler would need to resume, and this is the first thing in the design to
// read it back on this path.
volatile uint32_t ft_trap_vstart = 0xffffffff;
volatile uint32_t ft_trap_mcause = 0xffffffff;
volatile uint32_t ft_trap_mepc   = 0xffffffff;
// ftstatus as read in the handler, and again after the handler cleared it.
volatile uint32_t ft_trap_status         = 0xffffffff;
volatile uint32_t ft_trap_status_cleared = 0xffffffff;
// Corrected-error counts, read on whichever path the program actually takes.
// Lifetime totals, cleared only by reset, so a single-ELF test is the only place
// they can be read meaningfully (the harness resets before every program).
volatile uint32_t ft_ce_cnt  = 0xffffffff;
volatile uint32_t ft_dmr_cnt = 0xffffffff;
// ftctl as read at reset, and after software wrote 0 to its enable bit.
// PRESENT (bit 1) is what says whether this build has FT at all, so the whole
// register reading 0 is a legal answer and not a failure.
volatile uint32_t ft_ctl_reset   = 0xffffffff;
volatile uint32_t ft_ctl_cleared = 0xffffffff;
// 1 if the vector instruction executed with FT disabled completed without
// trapping. Under persistent injection with FT enabled it would have trapped, so
// this is the evidence that ftctl.EN reaches dispatch.
volatile uint32_t ft_disabled_ran = 0;

void isr_wrapper(void);
__attribute__((naked)) void isr_wrapper(void) {
  asm volatile(
      // mcause must be 19 (hardware error). Anything else -- in particular 2,
      // illegal instruction -- would send a handler to inspect an encoding that
      // is not the problem.
      "csrr t0, mcause \n"
      "la   t2, ft_trap_mcause \n"
      "sw   t0, 0(t2) \n"
      "li   t1, 19 \n"
      "bne  t0, t1, 2f \n"
      // mepc must be the vector instruction itself, so that a handler can
      // re-execute it. A zero or stale mepc is the failure this test exists to
      // catch: the trap would be unattributable and the instruction unrepeatable.
      "csrr t0, mepc \n"
      "la   t2, ft_trap_mepc \n"
      "sw   t0, 0(t2) \n"
      "la   t1, ft_target \n"
      "bne  t0, t1, 2f \n"
      "csrr t0, vstart \n"
      "la   t2, ft_trap_vstart \n"
      "sw   t0, 0(t2) \n"
      // ftstatus.ERR records the same event for anyone who was not this
      // handler. If the trap arrived, the bit must be set: a trap without the
      // record would leave the error invisible one instruction later.
      "csrr t0, 0x7c8 \n"
      "la   t2, ft_trap_status \n"
      "sw   t0, 0(t2) \n"
      "andi t1, t0, 1 \n"
      "beqz t1, 2f \n"
      // And it must be clearable, or the next error can never be told apart
      // from this one. Read back to check the clear took effect rather than
      // assuming the write did anything.
      "li   t1, 1 \n"
      "csrrc t0, 0x7c8, t1 \n"
      "csrr t0, 0x7c8 \n"
      "la   t2, ft_trap_status_cleared \n"
      "sw   t0, 0(t2) \n"
      "bnez t0, 2f \n"
      // The trap is the end of a retry sequence, so the DMR counter must have
      // counted those retries. Zero here would mean the counter is not wired to
      // the mechanism it claims to measure -- checked as a lower bound only,
      // since the exact number follows FT_RETRY_MAX and is reported, not fixed.
      "csrr t0, 0x7c9 \n"
      "la   t2, ft_ce_cnt \n"
      "sw   t0, 0(t2) \n"
      "csrr t0, 0x7ca \n"
      "la   t2, ft_dmr_cnt \n"
      "sw   t0, 0(t2) \n"
      "beqz t0, 2f \n"
      "li   t0, 1 \n"
      "la   t2, ft_trap_observed \n"
      "sw   t0, 0(t2) \n"
      ".word 0x08000073 \n"  // mpause (halt) -> success
      "2: ebreak \n"         // wrong cause or wrong pc -> fail
  );
}

}  // extern "C"

int main(int argc, char** argv) {
  asm volatile("csrw mtvec, %0" ::"rK"((uint32_t)(&isr_wrapper)));

  // ftctl at reset. EN must come up set: a machine whose fault tolerance boots
  // off protects nothing until software remembers to ask for it, and forgetting
  // is silent. Reads 0 in full without the FT back end, which is why the
  // assertion on this lives in the cocotb driver, where the build is known.
  uint32_t ctl;
  asm volatile("csrr %0, 0x7cb" : "=r"(ctl));
  ft_ctl_reset = ctl;

  // Disable duplication, then run a vector instruction. With FT on and
  // persistent injection this instruction traps; with FT off there is no second
  // copy, so there is nothing to compare and nothing to fail. Reaching the store
  // below is therefore the proof that ftctl.EN reaches dispatch -- if it did not,
  // this instruction would trap and the ISR would halt the program right here,
  // leaving ft_disabled_ran at 0.
  asm volatile("csrci 0x7cb, 1");
  asm volatile("csrr %0, 0x7cb" : "=r"(ctl));
  ft_ctl_cleared = ctl;
  asm volatile(
      "vsetivli x0, 4, e32, m1, ta, ma \n"
      "vadd.vi v2, v2, 1 \n");
  ft_disabled_ran = 1;

  // Re-enable, and from here the program behaves exactly as it did before ftctl
  // existed: the instruction below traps under persistent injection.
  asm volatile("csrsi 0x7cb, 1");

  // A plain vector arithmetic instruction: nothing about it is illegal, so any
  // exception it takes can only have come from the execution itself.
  asm volatile(
      "vsetivli x0, 4, e32, m1, ta, ma \n"
      ".globl ft_target \n"
      "ft_target: \n"
      "vadd.vi v1, v1, 1 \n");

  // Reached only if the instruction did not trap. Report ftstatus anyway: in a
  // build with no error, it must read zero. Without that check, a status bit
  // wired to a constant 1 would still pass the trapping case above, and the
  // register would be reporting the build rather than the machine.
  uint32_t status;
  asm volatile("csrr %0, 0x7c8" : "=r"(status));
  ft_trap_status = status;
  // Reached in three different builds, and the counters are what tells them
  // apart: 0 with no fault tolerance, 0 with it but nothing injected, and
  // nonzero when an injected error was corrected and the program still got the
  // right answer. These reads are not interlocked against the vector unit (see
  // Decode.scala for why), so in principle they could miss a rollback that has
  // not happened yet; here they do not, because the injected mismatch is
  // detected within a few cycles of the uop entering the ROB, which is shorter
  // than the distance from this instruction being fetched to its CSR read.
  uint32_t cecnt, dmrcnt;
  asm volatile("csrr %0, 0x7c9" : "=r"(cecnt));
  asm volatile("csrr %0, 0x7ca" : "=r"(dmrcnt));
  ft_ce_cnt = cecnt;
  ft_dmr_cnt = dmrcnt;
  ft_trap_observed = 0;
  asm volatile(".word 0x08000073");  // mpause (halt)
  return 0;
}
