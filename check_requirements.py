# Copyright 2024 Google LLC.
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

"""Requirements for Video Mate."""

import sys
import pkg_resources


def get_package_version(package_name: str) -> str:
  """Returns the version of the package installed.

  Args:
    package_name: The name of the package.

  Returns:
    The version of the package installed.
  """
  try:
    return pkg_resources.get_distribution(package_name).version
  except pkg_resources.DistributionNotFound:
    return "Version not found"


packages = frozenset([
    "vertexai",
    "google-cloud-texttospeech",
    "google-cloud-pubsub",
    "google-cloud-bigquery",
    "google-cloud-storage",
    "google-api-core",
    "streamlit",
    "gspread",
    "google-auth",
    "librosa",
    "ffmpeg-normalize",
])

for package in packages:
  if package == "dataclasses":
    print(
        "dataclasses: Built-in (Python"
        f" {sys.version_info.major}.{sys.version_info.minor})"
    )
  else:
    print(f"{package}: {get_package_version(package)}")
