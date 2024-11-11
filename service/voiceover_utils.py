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

"""Utils for Video Mate."""

import logging
import random
import time
from typing import Optional, Any

from google.api_core import exceptions
from google.cloud import bigquery
from google.cloud import storage
import vertexai
from vertexai.generative_models import Part


logging.basicConfig(
    format=(
        "%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s"
    ),
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
SLEEP_TIME = 2


class RegionManager:
  """RegionManager manages the regions used for Gemini API calls.

  Attributes:
    project_id: The project id of the GCP project.
    regions: The list of regions to use.
    current_region_index: The index of the current region.
  """
  REGIONS = frozenset([
      "asia-east2",
      "asia-east1",
      "asia-southeast1",
      "us-west1",
      "us-central1",
      "us-east1",
  ])

  def __init__(self, project_id) -> None:
    self.project_id = project_id
    self.regions = [self.REGIONS[0]] + random.sample(
        self.REGIONS[1:], len(self.REGIONS) - 1
    )
    self.current_region_index = 0

  def get_next_region(self) -> str | None:
    """Gets the next region to use.

    Returns:
      The next region to use.
    """
    if self.current_region_index < len(self.regions):
      region = self.regions[self.current_region_index]
      self.current_region_index += 1
      return region
    return None


def copy_file_from_gcs(
    gcs_bucket: str, source_blob_name: str, destination_local_filename: str
) -> None:
  """Copies a file from Google Cloud Storage to a temporary local filename.

  Args:
    gcs_bucket: string containing the bucket name.
    source_blob_name: Name of the source blob containing the file.
    destination_local_filename: Name of the local file that will be written.

  Returns:
    None
  """
  storage_client = storage.Client()
  bucket = storage_client.bucket(gcs_bucket)
  blob = bucket.blob(source_blob_name)
  blob.download_to_filename(destination_local_filename)


def copy_file_to_gcs(
    gcs_bucket: str, source_local_filename: str, destination_blob_name: str
) -> None:
  """Copies a file to Google Cloud Storage from a temporary local filename.

  Args:
    gcs_bucket: string containing the bucket name.
    source_local_filename: Name of the local file that will be copied.
    destination_blob_name: Name of the blob to be created in GCS.

  Returns:
    None
  """
  storage_client = storage.Client()
  bucket = storage_client.bucket(gcs_bucket)
  blob = bucket.blob(destination_blob_name)
  print(
      f"Uploading from {source_local_filename} to"
      f" gs://{gcs_bucket}/{destination_blob_name}"
  )
  print("Checking if blob exists")
  if blob.exists():
    print("Blob existing")
    # blob.delete()
  else:
    blob.upload_from_filename(source_local_filename)
    print("Uploaded")


def get_gemini_response_with_retry(
    text_prompt: str,
    video_uri: str,
    model: vertexai.generative_models.TextGenerationModel,
    generation_config: vertexai.generative_models.GenerationConfig,
    safety_settings: vertexai.generative_models.SafetySettings,
    project_id: str,
    max_retries: int = 5,
) -> Optional[str]:
  """Gets Gemini response with retry.

  Args:
    text_prompt: Prompt for generating the script.
    video_uri: Uri for the video.
    model: Model to use for generating the script.
    generation_config: Generation config for the model.
    safety_settings: Safety settings for the model.
    project_id: Project id of the GCP project.
    max_retries: Max retries for the model call.

  Returns:
    The response from the model.

  Raises:
    Exception: If the model call fails.
  """
  region_manager = RegionManager(project_id)
  for attempt in range(max_retries):
    region = region_manager.get_next_region()
    if not region:
      print("All regions have been tried. Exiting.")
      return None
    try:
      print(f"Attempting to use region: {region}")
      vertexai.init(project=project_id, location=region)
      if text_prompt and video_uri:
        part = [Part.from_uri(video_uri, mime_type="video/mp4"), text_prompt]
      elif text_prompt:
        part = [text_prompt]
      elif video_uri:
        part = [Part.from_uri(video_uri, mime_type="video/mp4")]
      else:
        print("Not valid input")
        return None
      model_response = model.generate_content(
          contents=part,
          generation_config=generation_config,
          safety_settings=safety_settings,
          stream=False,
      )
      return model_response.text
    except exceptions.ResourceExhausted as e:
      print(
          f"Throttling detected in region {region}. Switching to next region."
          f" Error: {e}"
      )
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(f"An error occurred in region {region}: {e}")
    if attempt < max_retries - 1:
      time.sleep(SLEEP_TIME)
  print("Max retries reached. Unable to get a response.")
  return None


def write_to_bigquery(
    project_id: str, dataset_id: str, table_id: str, row: dict[str, Any]
) -> None:
  """Writes data to BigQuery table.

  Args:
    project_id: The project id of the GCP project.
    dataset_id: The dataset id of the BigQuery dataset.
    table_id: The table id of the BigQuery table.
    row: The row to be written to the BigQuery table.

  Returns:
    None

  Raises:
    Exception: If the BigQuery operation fails.
  """
  client = bigquery.Client(project=project_id)
  table_ref = f"{project_id}.{dataset_id}.{table_id}"
  query = f"""
    MERGE `{table_ref}` T
    USING (SELECT @trixid as trixid, @date as date, @video_id as video_id, @version as version, @language_code as language_code, @voice_id as voice_id) S
    ON T.trixid = S.trixid AND T.date = S.date AND T.video_id = S.video_id AND T.version = S.version AND T.language_code = S.language_code AND T.voice_id = S.voice_id
    WHEN MATCHED THEN
        UPDATE SET client_name = @client_name, language_code = @language_code, voice_id = @voice_id,
                   direct_script = @direct_script, status = @status, file_location = @file_location,
                   err_msg = @err_msg
    WHEN NOT MATCHED THEN
        INSERT (trixid, client_name, date, version, language_code, voice_id, video_id,
                direct_script, status, file_location, err_msg)
        VALUES (@trixid, @client_name, @date, @version, @language_code, @voice_id, @video_id,
                @direct_script, @status, @file_location, @err_msg)
    """
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("trixid", "STRING", row["trixid"]),
          bigquery.ScalarQueryParameter(
              "client_name", "STRING", row["client_name"]
          ),
          bigquery.ScalarQueryParameter("date", "DATE", row["date"]),
          bigquery.ScalarQueryParameter("version", "STRING", row["version"]),
          bigquery.ScalarQueryParameter(
              "language_code", "STRING", row["language_code"]
          ),
          bigquery.ScalarQueryParameter("voice_id", "STRING", row["voice_id"]),
          bigquery.ScalarQueryParameter("video_id", "STRING", row["video_id"]),
          bigquery.ScalarQueryParameter(
              "direct_script", "STRING", row["direct_script"]
          ),
          bigquery.ScalarQueryParameter("status", "STRING", row["status"]),
          bigquery.ScalarQueryParameter(
              "file_location", "STRING", row["file_location"]
          ),
          bigquery.ScalarQueryParameter(
              "err_msg", "STRING", row["err_msg"][:1000]
          ),  # 截断错误消息
      ]
  )
  try:
    client.query(query, job_config=job_config)
    logger.info(
        "Successfully updated/inserted BigQuery table %s,for video_id"
        " :%s,status:%s", table_ref, row["video_id"], row["status"]
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    print(f"BigQuery operation failed: {str(e)}")
  finally:
    client.close()
