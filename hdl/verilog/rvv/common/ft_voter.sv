// description
// bitwise 2-out-of-3 majority voter, the read-out half of a register-only TMR.
// features:
//    1. WIDTH-wide, purely combinational, no state of its own
//    2. one output bit per input bit position: y[b] = majority(d[0][b], d[1][b], d[2][b])
// usage:
//    the protected register is triplicated into d[0..2] and every reader takes
//    y instead, so a single upset in any one copy is outvoted. The copies must
//    be fed from y (not from themselves), which both keeps their next-state
//    logic shared and scrubs an upset copy on the following clock.
// constraints:
//    1. only instantiated by fault-tolerance code (`FAULT_TOLERANT_ON`)
//    2. it and the flops it votes on must carry `FT_KEEP: three logically
//       equivalent copies are exactly what an optimizer is entitled to merge

module ft_voter(
  d,
  y
);
  parameter WIDTH = 1;

  input   logic [2:0][WIDTH-1:0]  d;
  output  logic     [WIDTH-1:0]   y;

  assign y = (d[0] & d[1]) | (d[1] & d[2]) | (d[0] & d[2]);

endmodule
