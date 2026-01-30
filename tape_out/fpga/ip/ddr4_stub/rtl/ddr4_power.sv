/* Copyright (c) 2020-2022 by XEPIC Co., LTD. */
`timescale 1ps/1ps
module ddr4_power (
input sys_rst , //Common port for all controllers
input gclk_100m ,
output ddr4_en_vtt ,
output ddr4_en_vddq ,
output ddr4_en_vcc2v5 ,
input power_good
);
localparam P_pwr_seq = 1000000 ;
localparam P_pwr_seq1 = 1600000 ;
localparam P_pwr_seq2 = 2600000 ;
localparam P_pwr_good = 25000 ;
reg [31:0] r_power_cnt ;
reg [31:0] r_power_cnt1 ;
reg [31:0] r_power_cnt2 ;
wire sys_rst_p ;
wire free_clk ;
wire s_power_cnt_end ;
wire s_power_cnt_end1 ;
wire s_power_cnt_end2 ;
reg r_ddr4_en_vtt ;
reg r_ddr4_en_vddq ;
reg r_ddr4_en_vcc2v5 ;
wire s_power_reset ;
wire s_power_good_n ;
reg [31:0] r_pwr_good_cnt ;
wire s_pwr_good_cnt_end ;
reg r_pwr_good_stable ;
wire s_pwr_good_stable_n ;
assign free_clk = gclk_100m ;
assign sys_rst_p = ~sys_rst ;
assign s_power_reset = sys_rst_p ;
always @(posedge free_clk or posedge s_power_reset)
if (s_power_reset) begin
r_power_cnt <= 32'h0;
end else begin
if ( s_power_cnt_end ) begin
r_power_cnt <= 32'h0;
end else begin
r_power_cnt <= r_power_cnt +1 ;
end
end
assign s_power_cnt_end = (r_power_cnt == (P_pwr_seq -1)) ;
always @(posedge free_clk or posedge s_power_reset)
if (s_power_reset) begin
r_power_cnt1 <= 32'h0;
end else begin
if ( s_power_cnt_end1 ) begin
r_power_cnt1 <= 32'h0;
end else begin
r_power_cnt1 <= r_power_cnt1 +1 ;
end
end
assign s_power_cnt_end1 = (r_power_cnt1 == (P_pwr_seq1 -1)) ;
always @(posedge free_clk or posedge s_power_reset)
if (s_power_reset) begin
r_power_cnt2 <= 32'h0;
end else begin
if ( s_power_cnt_end2 ) begin
r_power_cnt2 <= 32'h0;
end else begin
r_power_cnt2 <= r_power_cnt2 +1 ;
end
end
assign s_power_cnt_end2 = (r_power_cnt2 == (P_pwr_seq2 -1)) ;
always @(posedge free_clk or posedge s_power_reset)
if (s_power_reset) begin
r_ddr4_en_vcc2v5 <= 1'h0;
end else begin
r_ddr4_en_vcc2v5 <= 1'h1;
end
always @(posedge free_clk or posedge s_power_reset)
if (s_power_reset) begin
r_ddr4_en_vddq <= 1'h0;
end else begin
if ( s_power_cnt_end ) begin
r_ddr4_en_vddq <= 1'h1;
end
end
always @(posedge free_clk or posedge s_power_reset)
if (s_power_reset) begin
r_ddr4_en_vtt <= 1'h0;
end else begin
if ( s_power_cnt_end1 ) begin
r_ddr4_en_vtt <= 1'h1;
end
end
assign ddr4_en_vtt = r_ddr4_en_vtt ;
assign ddr4_en_vddq = r_ddr4_en_vddq ;
assign ddr4_en_vcc2v5 = r_ddr4_en_vcc2v5 ;
assign s_power_good_n = ~power_good ;
assign s_pwr_good_cnt_end = (r_pwr_good_cnt == (P_pwr_good -1)) ;
always @(posedge free_clk or posedge s_power_good_n)
if (s_power_good_n) begin
r_pwr_good_cnt <= 32'h0;
end else begin
if ( s_pwr_good_cnt_end | r_pwr_good_stable ) begin
r_pwr_good_cnt <= 32'h0;
end else begin
r_pwr_good_cnt <= r_pwr_good_cnt +1 ;
end
end
always @(posedge free_clk or posedge sys_rst_p)
if (sys_rst_p) begin
r_pwr_good_stable <= 1'h0;
end else begin
// if ( s_pwr_good_cnt_end ) begin
if ( s_power_cnt_end2 ) begin
r_pwr_good_stable <= 1'h1;
end
end
assign s_pwr_good_stable_n = ~r_pwr_good_stable ;
endmodule