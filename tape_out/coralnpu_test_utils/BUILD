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

# Tape-out specific cocotb test utilities. Anything here is for the
# tape_out flow only and should not be imported by upstream tests.

load("@coralnpu_hw//third_party/python:requirements.bzl", "requirement")

package(default_visibility = ["//visibility:public"])

# JTAG-DMI driver for the Core_Jtag_Chip cocotb top.
#
# Importable as:
#   from coralnpu_test_utils.jtag_dmi_interface import JtagDmiInterface
#
# Even though this file lives under //tape_out/coralnpu_test_utils, the
# import path stays `coralnpu_test_utils.jtag_dmi_interface` because the
# module's PYTHONPATH layout treats coralnpu_test_utils/ as a single flat
# package (as the upstream //coralnpu_test_utils targets do). We rely on
# cocotb runners adding both directories to sys.path.
py_library(
    name = "jtag_dmi_interface",
    srcs = ["jtag_dmi_interface.py"],
    imports = [".."],
    deps = [
        requirement("cocotb"),
        "//coralnpu_test_utils:core_mini_axi_sim_interface",
    ],
)
