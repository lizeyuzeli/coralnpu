# Copyright 2026 Li Zeyu <lizeyuzeli000lzy@gmail.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

load(
    "@coralnpu_hw//rules:chisel.bzl",
    "chisel_library",
    "chisel_test",
)

package(default_visibility = ["//visibility:public"])

chisel_library(
    name = "link",
    srcs = [
        "Crc8.scala",
        "LinkAdapter.scala",
        "LinkFrame.scala",
    ],
    deps = [
        "//hdl/chisel/src/bus",
        "//hdl/chisel/src/common",
        "//hdl/chisel/src/coralnpu:coralnpu_params",
        "@chipsalliance_rocket_chip//:asyncqueue",
    ],
)

chisel_test(
    name = "link_adapter_test",
    size = "medium",
    srcs = [
        "LinkAdapterTest.scala",
    ],
    deps = [
        ":link",
        "//hdl/chisel/src/bus",
        "//hdl/chisel/src/coralnpu:coralnpu_params",
    ],
)

