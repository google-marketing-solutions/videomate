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

"""Unit tests for service/video_voiceover.py command execution safety."""

import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


def _install_import_fakes_if_needed() -> None:
  """Installs lightweight module fakes when cloud/audio deps are absent."""
  service_dir = os.path.abspath(
      os.path.join(os.path.dirname(__file__), "..", "service")
  )
  if service_dir not in sys.path:
    sys.path.insert(0, service_dir)

  try:
    import google.cloud.texttospeech  # pylint: disable=unused-import,g-import-not-at-top
  except ImportError:
    google_pkg = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud_pkg = getattr(google_pkg, "cloud", None)
    if cloud_pkg is None:
      cloud_pkg = types.ModuleType("google.cloud")
      google_pkg.cloud = cloud_pkg
      sys.modules["google.cloud"] = cloud_pkg

    api_core_mod = types.ModuleType("google.api_core")
    exceptions_mod = types.ModuleType("google.api_core.exceptions")

    class ResourceExhausted(Exception):  # pylint: disable=g-bad-exception-name
      pass

    exceptions_mod.ResourceExhausted = ResourceExhausted
    api_core_mod.exceptions = exceptions_mod
    google_pkg.api_core = api_core_mod
    sys.modules["google.api_core"] = api_core_mod
    sys.modules["google.api_core.exceptions"] = exceptions_mod

    tts_mod = types.ModuleType("google.cloud.texttospeech")

    class SsmlVoiceGender:
      MALE = "MALE"
      FEMALE = "FEMALE"

    tts_mod.SsmlVoiceGender = SsmlVoiceGender
    cloud_pkg.texttospeech = tts_mod
    cloud_pkg.bigquery = types.ModuleType("google.cloud.bigquery")
    cloud_pkg.storage = types.ModuleType("google.cloud.storage")
    sys.modules["google.cloud.texttospeech"] = tts_mod
    sys.modules["google.cloud.bigquery"] = cloud_pkg.bigquery
    sys.modules["google.cloud.storage"] = cloud_pkg.storage

  try:
    import librosa  # pylint: disable=unused-import,g-import-not-at-top
  except ImportError:
    librosa_mod = types.ModuleType("librosa")
    librosa_mod.get_duration = lambda path: 1.0
    sys.modules["librosa"] = librosa_mod

  try:
    import vertexai  # pylint: disable=unused-import,g-import-not-at-top
  except ImportError:
    vertexai_mod = types.ModuleType("vertexai")
    gen_models_mod = types.ModuleType("vertexai.generative_models")

    class _HarmCategory:
      HARM_CATEGORY_HATE_SPEECH = 1
      HARM_CATEGORY_DANGEROUS_CONTENT = 2
      HARM_CATEGORY_SEXUALLY_EXPLICIT = 3
      HARM_CATEGORY_HARASSMENT = 4

    class _HarmBlockThreshold:
      BLOCK_NONE = 0

    class SafetySetting:
      HarmCategory = _HarmCategory  # pylint: disable=invalid-name
      HarmBlockThreshold = _HarmBlockThreshold  # pylint: disable=invalid-name

      def __init__(self, category=None, threshold=None):
        self.category = category
        self.threshold = threshold

    class Part:

      @staticmethod
      def from_uri(uri, mime_type=None):
        return (uri, mime_type)

    gen_models_mod.SafetySetting = SafetySetting
    gen_models_mod.Part = Part
    gen_models_mod.TextGenerationModel = object
    gen_models_mod.GenerationConfig = object
    gen_models_mod.SafetySettings = object
    vertexai_mod.generative_models = gen_models_mod
    sys.modules["vertexai"] = vertexai_mod
    sys.modules["vertexai.generative_models"] = gen_models_mod


_install_import_fakes_if_needed()

from service import video_voiceover  # pylint: disable=g-import-not-at-top


class _FakeBlob:
  """In-memory GCS blob fake supporting download, upload, and exists."""

  def __init__(self, uploaded_paths: list[str]):
    self._uploaded_paths = uploaded_paths

  def download_to_filename(self, filename: str) -> None:
    with open(filename, "wb") as fh:
      fh.write(b"fake-media")

  def upload_from_filename(self, filename: str) -> None:
    self._uploaded_paths.append(filename)

  def exists(self) -> bool:
    return False


class _FakeBucket:
  """In-memory GCS bucket fake."""

  def __init__(self, uploaded_paths: list[str]):
    self._uploaded_paths = uploaded_paths

  def blob(self, unused_name: str) -> _FakeBlob:
    return _FakeBlob(self._uploaded_paths)


class _FakeStorageClient:
  """In-memory GCS client fake."""

  def __init__(self):
    self.uploaded_paths: list[str] = []

  def bucket(self, unused_name: str) -> _FakeBucket:
    return _FakeBucket(self.uploaded_paths)


class VideoVoiceoverCommandSafetyTest(unittest.TestCase):
  """Tests for command execution safety in VideoVoiceover.generate_video_file."""

  def _make_voiceover(
      self,
      tmp_dir: str,
      gcs_top_level_folder: str,
      original_video_has_vocal_dubbing: str,
  ) -> video_voiceover.VideoVoiceover:
    """Creates a VideoVoiceover instance wired to in-memory GCS fakes."""
    gcs_client = _FakeStorageClient()
    with mock.patch.object(
        video_voiceover.VideoVoiceover,
        "gcs_download_and_upload_youtube_video",
        return_value=(True, 10.0, ""),
    ):
      return video_voiceover.VideoVoiceover(
          gemini_pro_model=object(),
          tts_client=object(),
          gcs_client=gcs_client,
          trix_id="trix1",
          video_id="vid1",
          client_name="acme.com",
          yyyymmdd="20260101",
          version="1",
          language_name="English",
          language_code="en-US",
          language_full_name="English",
          voice_id="en-US-Standard-A",
          voice_gender="MALE",
          original_video_has_vocal_dubbing=original_video_has_vocal_dubbing,
          voiceover_script_context_prompt="",
          tmp_path=tmp_dir,
          gcs_bucket_name="bucket",
          gcs_top_level_folder=gcs_top_level_folder,
          tmp_table_id="temp_1",
      )

  def test_generate_video_file_blocks_shell_injection_in_vocal_removal_and_mix(
      self,
  ):
    """Verifies vocal removal, normalization, and mixing use safe arg lists."""
    injected_folder = "x.mp4;id>vh_ossys_out;.z"
    recorded_runs: list[tuple[object, dict[str, object]]] = []

    def fake_copy_from_gcs(unused_bkt, unused_src, dst):
      with open(dst, "wb") as fh:
        fh.write(b"fake")

    def fake_run(cmd, **kwargs):
      recorded_runs.append((cmd, kwargs))
      if isinstance(cmd, list) and cmd and cmd[0] == "ffmpeg":
        out_path = cmd[-1]
        with open(out_path, "wb") as fh:
          fh.write(b"out")
      return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with tempfile.TemporaryDirectory() as tmp_dir:
      vv = self._make_voiceover(
          tmp_dir=tmp_dir,
          gcs_top_level_folder=injected_folder,
          original_video_has_vocal_dubbing="YES",
      )
      with (
          mock.patch.object(
              video_voiceover.voiceover_utils,
              "copy_file_from_gcs",
              side_effect=fake_copy_from_gcs,
          ),
          mock.patch.object(
              video_voiceover.voiceover_utils, "copy_file_to_gcs"
          ),
          mock.patch.object(vv, "_get_audio_length", return_value=5.0),
          mock.patch.object(
              video_voiceover.os, "system", side_effect=AssertionError
          ),
          mock.patch.object(
              video_voiceover.subprocess, "run", side_effect=fake_run
          ),
      ):
        result = vv.generate_video_file()

    self.assertTrue(result)
    self.assertEqual(len(recorded_runs), 3)
    for cmd, kwargs in recorded_runs:
      self.assertIsInstance(cmd, list)
      self.assertFalse(kwargs.get("shell", False))
      for idx, arg in enumerate(cmd):
        self.assertNotIn("vh_ossys_out", arg)
        if idx > 0 and cmd[idx - 1] == "-filter_complex":
          continue
        self.assertNotIn(";", arg)

    vocal_cmd, _ = recorded_runs[0]
    self.assertEqual(vocal_cmd[0], "ffmpeg")
    self.assertIn("pan=stereo|c0=c0-c1|c1=c0-c1", vocal_cmd)
    self.assertTrue(vocal_cmd[vocal_cmd.index("-i") + 1].endswith(".mp4"))
    self.assertTrue(vocal_cmd[-1].endswith("_removed_original_vocal.mp4"))

    norm_cmd, _ = recorded_runs[1]
    self.assertEqual(norm_cmd[0], "ffmpeg-normalize")
    self.assertTrue(norm_cmd[1].endswith("_removed_original_vocal.mp4"))

    mix_cmd, _ = recorded_runs[2]
    self.assertEqual(mix_cmd[0], "ffmpeg")
    filter_arg = mix_cmd[mix_cmd.index("-filter_complex") + 1]
    self.assertFalse(filter_arg.startswith('"'))
    self.assertFalse(filter_arg.endswith('"'))
    self.assertIn("[audio_out]", filter_arg)

  def test_generate_video_file_handles_dotted_folder_without_vocal_removal(
      self,
  ):
    """Verifies dotted GCS folders do not corrupt media file extension parsing."""
    recorded_runs: list[tuple[object, dict[str, object]]] = []

    def fake_copy_from_gcs(unused_bkt, unused_src, dst):
      with open(dst, "wb") as fh:
        fh.write(b"fake")

    def fake_run(cmd, **kwargs):
      recorded_runs.append((cmd, kwargs))
      if isinstance(cmd, list) and cmd and cmd[0] == "ffmpeg":
        with open(cmd[-1], "wb") as fh:
          fh.write(b"out")
      return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with tempfile.TemporaryDirectory() as tmp_dir:
      vv = self._make_voiceover(
          tmp_dir=tmp_dir,
          gcs_top_level_folder="campaigns/v1.2/acme.co",
          original_video_has_vocal_dubbing="NO",
      )
      with (
          mock.patch.object(
              video_voiceover.voiceover_utils,
              "copy_file_from_gcs",
              side_effect=fake_copy_from_gcs,
          ),
          mock.patch.object(
              video_voiceover.voiceover_utils, "copy_file_to_gcs"
          ),
          mock.patch.object(vv, "_get_audio_length", return_value=5.0),
          mock.patch.object(
              video_voiceover.os, "system", side_effect=AssertionError
          ),
          mock.patch.object(
              video_voiceover.subprocess, "run", side_effect=fake_run
          ),
      ):
        result = vv.generate_video_file()

    self.assertTrue(result)
    self.assertEqual(len(recorded_runs), 2)
    self.assertEqual(recorded_runs[0][0][0], "ffmpeg-normalize")
    self.assertTrue(recorded_runs[0][0][1].endswith(".mp4"))
    self.assertEqual(recorded_runs[1][0][0], "ffmpeg")

  def test_generate_video_file_falls_back_when_vocal_removal_fails(self):
    """Verifies vocal removal failure falls back cleanly to original video."""
    recorded_runs: list[tuple[object, dict[str, object]]] = []

    def fake_copy_from_gcs(unused_bkt, unused_src, dst):
      with open(dst, "wb") as fh:
        fh.write(b"fake")

    def fake_run(cmd, **kwargs):
      recorded_runs.append((cmd, kwargs))
      if isinstance(cmd, list) and cmd and cmd[0] == "ffmpeg" and "-af" in cmd:
        raise subprocess.CalledProcessError(1, cmd, stderr="ffmpeg failed")
      if isinstance(cmd, list) and cmd and cmd[0] == "ffmpeg":
        with open(cmd[-1], "wb") as fh:
          fh.write(b"out")
      return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with tempfile.TemporaryDirectory() as tmp_dir:
      vv = self._make_voiceover(
          tmp_dir=tmp_dir,
          gcs_top_level_folder="clean_folder",
          original_video_has_vocal_dubbing="YES",
      )
      with (
          mock.patch.object(
              video_voiceover.voiceover_utils,
              "copy_file_from_gcs",
              side_effect=fake_copy_from_gcs,
          ),
          mock.patch.object(
              video_voiceover.voiceover_utils, "copy_file_to_gcs"
          ),
          mock.patch.object(vv, "_get_audio_length", return_value=5.0),
          mock.patch.object(
              video_voiceover.subprocess, "run", side_effect=fake_run
          ),
      ):
        result = vv.generate_video_file()

    self.assertTrue(result)
    self.assertEqual(len(recorded_runs), 3)
    norm_cmd, _ = recorded_runs[1]
    self.assertEqual(norm_cmd[0], "ffmpeg-normalize")
    self.assertNotIn("_removed_original_vocal", norm_cmd[1])

  def test_generate_video_file_rejects_invalid_extension(self):
    """Verifies generate_video_file rejects non-alphanumeric media extensions."""
    with tempfile.TemporaryDirectory() as tmp_dir:
      vv = self._make_voiceover(
          tmp_dir=tmp_dir,
          gcs_top_level_folder="clean_folder",
          original_video_has_vocal_dubbing="NO",
      )
      with (
          mock.patch.object(
              vv, "_get_audio_file_name", return_value="bad.mp4;id"
          ),
          mock.patch.object(
              video_voiceover.voiceover_utils,
              "copy_file_from_gcs",
              side_effect=AssertionError,
          ),
          mock.patch.object(
              video_voiceover.subprocess, "run", side_effect=AssertionError
          ),
      ):
        with self.assertRaisesRegex(ValueError, "Invalid media file extension"):
          vv.generate_video_file()


if __name__ == "__main__":
  unittest.main()
