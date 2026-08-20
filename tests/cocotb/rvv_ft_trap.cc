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

  // A plain vector arithmetic instruction: nothing about it is illegal, so any
  // exception it takes can only have come from the execution itself.
  asm volatile(
      "vsetivli x0, 4, e32, m1, ta, ma \n"
      ".globl ft_target \n"
      "ft_target: \n"
      "vadd.vi v1, v1, 1 \n");

  // Reached only if the instruction did not trap.
  ft_trap_observed = 0;
  asm volatile(".word 0x08000073");  // mpause (halt)
  return 0;
}
