# Copyright 2026 Li Zeyu <lizeyuzeli000lzy@gmail.com>
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

# Framework-only package for the RVV RTL fault-injection harness.
# Per-model FI cocotb_test_suite targets live next to each model under
# //tests/cocotb/tflite/.../<model>:cocotb_<model>_fi and depend on the
# libraries exported here.

py_library(
    name = "fi_utils",
    srcs = ["fi_utils.py"],
    # Put this directory on PYTHONPATH for consumers so they can
    # `import fi_utils` directly, regardless of which Bazel package they
    # live in. Without this, only same-package callers can find it.
    imports = ["."],
    visibility = ["//visibility:public"],
)

py_library(
    name = "fi_campaign",
    srcs = ["fi_campaign.py"],
    imports = ["."],
    visibility = ["//visibility:public"],
    deps = [":fi_utils"],
)
