# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Clock Signal
create_clock -period 10.00 -name sys_clk_pin -waveform {0 5} [get_ports clk_p_i]
set_property -dict { PACKAGE_PIN AG47 IOSTANDARD LVDS } [get_ports { clk_p_i }];
set_property -dict { PACKAGE_PIN AF47 IOSTANDARD LVDS } [get_ports { clk_n_i }];
# DDR reference clock is constrained by the DDR core XDC.

# Generated Clocks
create_generated_clock -name clk_main [get_pin i_clkgen/i_clkgen/pll/CLKOUT0]
create_generated_clock -name clk_48MHz [get_pin i_clkgen/i_clkgen/pll/CLKOUT1]
create_generated_clock -name clk_aon [get_pin i_clkgen/i_clkgen/pll/CLKOUT4]

# Reset
set_property -dict { PACKAGE_PIN T42 IOSTANDARD LVCMOS18 } [get_ports { rst_ni }];

# JTAG
# 500 kHz clock constraint
create_clock -period 2000.00 -name jtag_tck_i -waveform {0 1000} [get_ports {tck_i}]
set_property -dict { PACKAGE_PIN U54 IOSTANDARD LVCMOS18 PULLTYPE PULLDOWN } [get_ports {tms_i}]
set_property -dict { PACKAGE_PIN T53 IOSTANDARD LVCMOS18 PULLTYPE PULLDOWN } [get_ports {td_o}]
set_property -dict { PACKAGE_PIN T52 IOSTANDARD LVCMOS18 PULLTYPE PULLDOWN } [get_ports {td_i}]
set_property -dict { PACKAGE_PIN V52 IOSTANDARD LVCMOS18 PULLTYPE PULLDOWN } [get_ports {tck_i}]
set_property -dict { PACKAGE_PIN U52 IOSTANDARD LVCMOS18 } [get_ports {trst_ni}]


# SPI
create_clock -period 83.333 -name spi_clk_i -waveform {0 41.667} [get_ports spi_clk_i]
set_property -dict { PACKAGE_PIN W50 IOSTANDARD LVCMOS18 } [get_ports { spi_clk_i }]; # J4 gpio1
set_property -dict { PACKAGE_PIN R51 IOSTANDARD LVCMOS18 } [get_ports { spi_csb_i }]; # J4 gpio2
set_property -dict { PACKAGE_PIN R52 IOSTANDARD LVCMOS18 } [get_ports { spi_mosi_i }]; # J4 gpio3
set_property -dict { PACKAGE_PIN U50 IOSTANDARD LVCMOS18 } [get_ports { spi_miso_o }]; # J4 gpio4

# NOTE: The P1 board's SPI clock pin may not be a GCIO-capable pin.
# Vivado may try to route `spi_clk_i` onto a global clock network (BUFG), which
# fails clock placer rule_gclkio_bufg. For Phase-1 bring-up (low-frequency SPI),
# we allow non-dedicated routing to unblock implementation.
# Prefer a GCIO pin or redesign SPI to avoid using SCK as a fabric clock.
set_property CLOCK_DEDICATED_ROUTE FALSE [get_nets spi_clk_i_IBUF_inst/O]

# UART0
set_property -dict { PACKAGE_PIN R46 IOSTANDARD LVCMOS18 } [get_ports { uart_tx_o[0] }];
set_property -dict { PACKAGE_PIN R45 IOSTANDARD LVCMOS18 } [get_ports { uart_rx_i[0] }];

# UART1
set_property -dict { PACKAGE_PIN V48 IOSTANDARD LVCMOS18 } [get_ports { uart_tx_o[1] }];
set_property -dict { PACKAGE_PIN W48 IOSTANDARD LVCMOS18 } [get_ports { uart_rx_i[1] }];

# LEDs
set_property -dict { PACKAGE_PIN U44 IOSTANDARD LVCMOS18 } [get_ports { io_halted }];
set_property -dict { PACKAGE_PIN U45 IOSTANDARD LVCMOS18 } [get_ports { io_fault }];
set_property -dict { PACKAGE_PIN R40 IOSTANDARD LVCMOS18 } [get_ports { ddr_cal_complete_o }];
set_property -dict { PACKAGE_PIN D24 IOSTANDARD LVCMOS18 } [get_ports { io_ddr_mem_axi_aw_ready }];
set_property -dict { PACKAGE_PIN C24 IOSTANDARD LVCMOS18 } [get_ports { io_ddr_mem_axi_ar_ready }];
set_property -dict { PACKAGE_PIN D21 IOSTANDARD LVCMOS18 } [get_ports { ddr_ui_clk }];
set_property -dict { PACKAGE_PIN B24 IOSTANDARD LVCMOS18 } [get_ports { ddr_ui_clk_sync_rst }];

# Asynchronous Clock Groups
# Define all primary, asynchronous clocks
set_clock_groups -asynchronous \
  -group [get_clocks -include_generated_clocks sys_clk_pin] \
  -group [get_clocks -include_generated_clocks c0_sys_clk_p] \
  -group [get_clocks spi_clk_i] \
  -group [get_clocks jtag_tck_i]

# SPI Probe Outputs (PMOD3) -> Reassigned to SpiMaster
set_property -dict { PACKAGE_PIN U41 IOSTANDARD LVCMOS18 } [get_ports { spim_sclk_o }]; # J4 gpio14
set_property -dict { PACKAGE_PIN T40 IOSTANDARD LVCMOS18 } [get_ports { spim_csb_o }]; # J4 gpio13
set_property -dict { PACKAGE_PIN U40 IOSTANDARD LVCMOS18 } [get_ports { spim_mosi_o }]; # J4 gpio12
set_property -dict { PACKAGE_PIN U46 IOSTANDARD LVCMOS18 } [get_ports { spim_miso_i }]; # J4 gpio11

# I2C (PMOD2)
set_property -dict { PACKAGE_PIN T50 IOSTANDARD LVCMOS18 } [get_ports { i2c_scl }]; # J4 gpio5
set_property -dict { PACKAGE_PIN W51 IOSTANDARD LVCMOS18 } [get_ports { i2c_sda }]; # J4 gpio6

set_property -dict { PACKAGE_PIN W49 IOSTANDARD LVCMOS18 } [get_ports { gpio[0] }]; # J4 gpio0
set_property -dict { PACKAGE_PIN V43 IOSTANDARD LVCMOS18 } [get_ports { gpio[1] }]; # J4 gpio8
set_property -dict { PACKAGE_PIN V44 IOSTANDARD LVCMOS18 } [get_ports { gpio[2] }]; # J4 gpio9
set_property -dict { PACKAGE_PIN V46 IOSTANDARD LVCMOS18 } [get_ports { gpio[3] }]; # J4 gpio10

# set_property -dict { PACKAGE_PIN U40 IOSTANDARD LVCMOS18 } [get_ports { spi_clk_probe_o }]; # J4 gpio12
# set_property -dict { PACKAGE_PIN T40 IOSTANDARD LVCMOS18 } [get_ports { spi_csb_probe_o }]; # J4 gpio13
# set_property -dict { PACKAGE_PIN U41 IOSTANDARD LVCMOS18 } [get_ports { spi_mosi_probe_o }]; # J4 gpio14
# set_property -dict { PACKAGE_PIN V53 IOSTANDARD LVCMOS18 } [get_ports { spi_miso_probe_o }]; # J4 gpio15