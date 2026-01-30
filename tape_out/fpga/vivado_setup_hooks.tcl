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

## Elevate warning for not finding file for readmemh to ERROR.
set_msg_config -id {[Synth 8-4445]} -new_severity ERROR

set workroot [pwd]

# If we see the Xilinx DDR core, register the post-bitstream hook to stitch the calibration FW.
if {[file exists "${workroot}/src/xilinx_ddr4_0_0.1_0"]} {
    set_property STEPS.WRITE_BITSTREAM.TCL.POST "${workroot}/vivado_hook_write_bitstream_post.tcl" [get_runs impl_1]
}