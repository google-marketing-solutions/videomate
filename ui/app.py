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

"""Streamlit control panel for VoiceOver processing."""

import datetime
import json
import logging
import re
import uuid

from google.api_core import exceptions
from google.cloud import bigquery
from google.cloud import pubsub_v1
from google.cloud import storage
from google.oauth2 import service_account
import gspread
from service import voiceover_config
import streamlit as st

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

GCP_PROJECT_ID = voiceover_config.GCP_PROJECT_ID
PUBSUB_TOPIC = voiceover_config.PUBSUB_TOPIC
DATASET_ID = voiceover_config.DATASET_ID
TABLE_ID = voiceover_config.TABLE_ID
SCOPES = frozenset([
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
])
cred = service_account.Credentials.from_service_account_file(
    "YOUR_SERVICE_ACCOUNT_CREDENTIAL.json", scopes=SCOPES
)

# BigQuery table identifiers interpolated into backtick-quoted queries cannot
# be bound as query parameters, so validate table_id strictly before building
# SQL (https://cloud.google.com/bigquery/docs/tables#table_naming).
_BQ_TABLE_ID_RE = re.compile(r"[A-Za-z0-9_]{1,1024}")


def _validate_table_id(table_id: str) -> None:
  """Validates a BigQuery table identifier before SQL interpolation."""
  if not isinstance(table_id, str) or not _BQ_TABLE_ID_RE.fullmatch(table_id):
    raise ValueError("invalid BigQuery table_id")


def create_temp_table(project_id: str, dataset_id: str) -> str:
  """Creates a temporary BigQuery table for storing processing status.

  Args:
    project_id: The GCP project ID.
    dataset_id: The BigQuery dataset ID.

  Returns:
    The ID of the created temporary table.

  Raises:
    Exception: If an error occurs during table creation.
  """
  client = bigquery.Client(project=project_id)
  table_id = f"temp_{uuid.uuid4().hex}"
  table_ref = f"{project_id}.{dataset_id}.{table_id}"
  schema = [
      bigquery.SchemaField("video_id", "STRING"),
      bigquery.SchemaField("version", "STRING"),
      bigquery.SchemaField("status", "STRING"),
  ]
  table = bigquery.Table(table_ref, schema=schema)
  # Set the expiration time of the table to 30 days later
  expiration = datetime.datetime.now() + datetime.timedelta(days=30)
  table.expires = expiration
  try:
    client.create_table(table)
    logging.info("Temporary table %s created successfully.", table_ref)
    return table_id
  except Exception as e:  # pylint: disable=broad-exception-caught
    logging.error(
        "An error occurred while creating temporary table: %s", str(e)
    )
    raise


def update_temp_table(
    project_id: str, dataset_id: str, table_id: str, video_id: str, version: str
) -> None:
  """Updates the temporary BigQuery table with processing status.

  Args:
    project_id: The GCP project ID.
    dataset_id: The BigQuery dataset ID.
    table_id: The BigQuery dataset name.
    video_id: The video ID to be processed.
    version: The version of the video to be processed.

  Raises:
    Exception: If an error occurs during table update.
  """
  _validate_table_id(table_id)
  client = bigquery.Client(project=project_id)
  query = f"""
    INSERT INTO `{project_id}.{dataset_id}.{table_id}` (video_id, version, status)
    VALUES (@video_id, @version, '')
    """
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("video_id", "STRING", str(video_id)),
          bigquery.ScalarQueryParameter("version", "STRING", str(version)),
      ]
  )
  try:
    query_job = client.query(query, job_config=job_config)
    query_job.result()
    logging.info(
        "Updated temporary table for video_id: %s, version: %s",
        video_id,
        version,
    )
  except Exception as e:  # pylint: disable=broad-exception-caught
    logging.error("Error updating temporary table: %s", str(e))


def create_bigquery_table_if_not_exists(
    project_id: str, dataset_id: str, table_id: str
) -> None:
  """Creates a BigQuery table if it does not already exist.

  Args:
    project_id: The GCP project ID.
    dataset_id: The BigQuery dataset ID.
    table_id: The BigQuery dataset name.

  Raises:
    Exception: If an error occurs during table creation.
  """
  client = bigquery.Client(project=project_id)
  table_ref = f"{project_id}.{dataset_id}.{table_id}"
  schema = [
      bigquery.SchemaField("trixid", "STRING"),
      bigquery.SchemaField("client_name", "STRING"),
      bigquery.SchemaField("date", "date"),
      bigquery.SchemaField("version", "STRING"),
      bigquery.SchemaField("language_code", "STRING"),
      bigquery.SchemaField("voice_id", "STRING"),
      bigquery.SchemaField("video_id", "STRING"),
      bigquery.SchemaField("direct_script", "STRING"),
      bigquery.SchemaField("status", "STRING"),
      bigquery.SchemaField("file_location", "STRING"),
      bigquery.SchemaField("err_msg", "STRING"),
  ]
  table = bigquery.Table(table_ref, schema=schema)
  table.time_partitioning = bigquery.TimePartitioning(
      type_=bigquery.TimePartitioningType.DAY, field="date"
  )
  try:
    client.create_table(table)
    logging.info("Table %s created successfully.", table_ref)
  except exceptions.Conflict:
    logging.info("Table %s already exists.", table_ref)
  except Exception as e:
    logging.error(
        "An error occurred while creating table %s: %s", table_ref, str(e)
    )
    raise


def get_table_stats(
    project_id: str, dataset_id: str, table_id: str
) -> tuple[int, int]:
  """Gets the statistics of a BigQuery table.

  Args:
    project_id: The GCP project ID.
    dataset_id: The BigQuery dataset ID.
    table_id: The BigQuery dataset name.

  Returns:
    A tuple containing the total and processed counts of the table.

  Raises:
    Exception: If an error occurs during table statistics retrieval.
  """
  _validate_table_id(table_id)
  client = bigquery.Client(project=project_id)
  total_query = f"""
    SELECT COUNT(*) as total
    FROM `{project_id}.{dataset_id}.{table_id}`
    """
  processed_query = f"""
    SELECT COUNT(*) as processed
    FROM `{project_id}.{dataset_id}.{table_id}`
    WHERE status != ''
    """
  try:
    total_job = client.query(total_query)
    total_result = total_job.result()
    total_count = next(iter(total_result))[0]
    processed_job = client.query(processed_query)
    processed_result = processed_job.result()
    processed_count = next(iter(processed_result))[0]
    return total_count, processed_count
  except Exception as e:  # pylint: disable=broad-exception-caught
    logging.error("An error occurred while querying the table: %s", str(e))
    return None, None


def display_table_results(table_id: str) -> None:
  """Display the processing progress of a BigQuery table in a Streamlit app.

  This function retrieves statistics from a BigQuery table (specified by
  `table_id`) and displays the following information in a Streamlit app:

  - Total data volume in the table.
  - Amount of data processed.
  - Processing progress as a percentage.
  - A progress bar to visually represent the progress.
  - A status message indicating whether processing is complete, in progress,
    or hasn't started.

  Args:
    table_id: The ID of the BigQuery table.
  """
  table_id = table_id.strip() if isinstance(table_id, str) else table_id
  if table_id:
    try:
      total, processed = get_table_stats(GCP_PROJECT_ID, DATASET_ID, table_id)
    except ValueError:
      total, processed = None, None
    if total is not None and processed is not None:
      st.write(f"Total data volume: {total}")
      st.write(f"Amount of data processed: {processed}")
      progress = processed / total if total > 0 else 0
      st.progress(progress)
      st.write(
          f"Processing progress: {processed}/{total} ({progress*100:.2f}%)"
      )
      if processed == total and total > 0:
        st.success("All data processed!")
      elif processed == 0:
        st.warning("Processing has not started or no data has been processed.")
      else:
        st.info("Data processing is in progress...")
    else:
      st.error(
          "Unable to get table statistics. Please check that the table ID is"
          " correct."
      )
  else:
    st.warning("No temporary table ID provided.")


def validate_date(date_string: str, date_format="%Y%m%d") -> bool:
  """Validates the date format.

  Args:
    date_string: The date string to be validated.
    date_format: The expected date format (default: %Y%m%d).

  Returns:
    True if the date string is valid according to the given format,
    False otherwise.
  """
  try:
    datetime.datetime.strptime(date_string, date_format)
    return True
  except ValueError:
    return False


def validate_gcs(bucket_name: str, folder_name: str) -> tuple[bool, str]:
  """Verify GCS bucket and folder.

  Args:
    bucket_name: The GCS bucket name.
    folder_name: The GCS folder name.

  Returns:
    True if the GCS bucket and folder exist, False otherwise.

  Raises:
    Exception: If an error occurs during GCS verification.
  """
  try:
    storage_client = storage.Client()
    bucket = storage_client.get_bucket(bucket_name)
    # Make sure folder_name ends with '/'
    if not folder_name.endswith("/"):
      folder_name += "/"
    # Check if the folder exists
    blobs = list(bucket.list_blobs(prefix=folder_name, delimiter="/"))
    if not blobs:
      return (
          False,
          f"Folder '{folder_name}' not found in bucket '{bucket_name}'",
      )
    return True, ""
  except Exception as e:  # pylint: disable=broad-exception-caught
    return False, str(e)


def is_video_processed(
    client: bigquery.Client,
    project_id: str,
    dataset_id: str,
    table_id: str,
    date: str,
    version: str,
    video_id: str,
    languagecode: str,
    voiceid: str,
) -> bool:
  """Check if processed video exists in BigQuery.

  Args:
    client: The BigQuery client.
    project_id: The GCP project ID.
    dataset_id: The BigQuery dataset ID.
    table_id: The BigQuery dataset name.
    date: The date of the video to be processed.
    version: The version of the video to be processed.
    video_id: The video ID to be processed.
    languagecode: The language code of the video to be processed.
    voiceid: The voice ID of the video to be processed.

  Returns:
    True if the video is processed, False otherwise.

  Raises:
    ValueError: If the date string is invalid.
  """
  try:
    date_str = str(date)
    formatted_date = datetime.datetime.strptime(date_str, "%Y%m%d").strftime(
        "%Y-%m-%d"
    )
  except ValueError:
    logging.error("Invalid date format: %s. Expected format: YYYYMMDD", date)
    return False
  _validate_table_id(table_id)
  query = f"""
    SELECT COUNT(*) as count
    FROM `{project_id}.{dataset_id}.{table_id}`
    WHERE date = DATE(@date)
    AND video_id = @video_id
    AND language_code = @languagecode
    AND voice_id = @voiceid
    AND status IN ('GENERATE_SUCCESS', 'ALREADY_EXISTS')
    """
  query_parameters = [
      bigquery.ScalarQueryParameter("date", "STRING", formatted_date),
      bigquery.ScalarQueryParameter("video_id", "STRING", str(video_id)),
      bigquery.ScalarQueryParameter(
          "languagecode", "STRING", str(languagecode)
      ),
      bigquery.ScalarQueryParameter("voiceid", "STRING", str(voiceid)),
  ]
  if version:
    query += " AND version = @version"
    query_parameters.append(
        bigquery.ScalarQueryParameter("version", "STRING", str(version))
    )
  job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
  logging.info("query is %s", query)
  query_job = client.query(query, job_config=job_config)
  results = query_job.result()
  row = next(iter(results))
  return row["count"] > 0


def main():
  try:
    st.title("Video Processing Control Panel")
    # Create two pages: homepage and results page
    page = st.sidebar.radio(
        "Navigation", ["Homepage", "Processing Results Preview"]
    )
    if page == "Homepage":
      input_trix_id = st.text_input(
          "Input Spreadsheet ID", value="your-default-spreadsheet-id"
      )
      input_sheet_name = st.text_input("Input Sheet Name", value="Sheet1")
      gcs_bucket_name = st.text_input(
          "Google Cloud Storage Bucket Name", value="your-bucket-name"
      )
      gcs_top_level_folder = st.text_input(
          "Google Cloud Storage Top Level Folder Name", value="your-folder-name"
      )
      date_col1, date_col2 = st.columns([3, 1])
      with date_col1:
        date = st.text_input("Date (yyyymmdd)", value="20240101")
      with date_col2:
        if not validate_date(date):
          st.error("Invalid format")
        else:
          st.success("Correct format")

      version = st.text_input(
          "Version (optional)",
          value="",
          help=(
              "If left blank, all versions on the specified date will be"
              " processed"
          ),
      ).strip()
      process_count = st.number_input(
          "Processing video quantity", min_value=1, value=1000
      )

      if st.button("Run processing"):
        # Check the date format again, if invalid no processing is performed
        if not date:
          st.error("Date is required, please fill in and try again.")
        elif not validate_date(date):
          st.error(
              "Invalid date format. Please try again using yyyymmdd format."
          )
        else:
          # Verify GCS bucket and folder
          is_valid, error_message = validate_gcs(
              gcs_bucket_name, gcs_top_level_folder
          )
          if not is_valid:
            st.error("GCS Authentication failed: %s", error_message)
          else:
            try:
              create_bigquery_table_if_not_exists(
                  GCP_PROJECT_ID, DATASET_ID, TABLE_ID
              )
              try:
                gc = gspread.authorize(cred)
                input_sh = gc.open_by_key(input_trix_id)
                input_worksheet = input_sh.worksheet(input_sheet_name)
                input_rows = input_worksheet.get_all_records()
              except gspread.exceptions.SpreadsheetNotFound:
                st.error(
                    "The specified spreadsheet cannot be found, please check if"
                    " the Spreadsheet ID is correct."
                )
                return
              except gspread.exceptions.WorksheetNotFound:
                st.error(
                    "Sheet '%s' not found, please check if the sheet name is"
                    " correct.",
                    input_sheet_name,
                )
                return
              except (
                  gspread.exceptions.APIError,
                  gspread.exceptions.GSpreadException,
              ) as e:
                st.error(
                    "An error occurred while reading the spreadsheet: %s",
                    str(e),
                )
                return
              # Create an empty placeholder for updating the status message
              status_message = st.empty()
              # Filter data matching VERSION
              matching_rows = [
                  row
                  for row in input_rows
                  if str(row["yyyymmdd"]).strip() == str(date).strip()
                  and (not version or str(row["version"]).strip() == version)
              ]
              if not matching_rows:
                status_message.warning(
                    "No matching data found for date %s."
                    + ("Version %s " if version else ""),
                    date,
                    version,
                )
                return
              bq_client = bigquery.Client(project=GCP_PROJECT_ID)
              temp_table_id = create_temp_table(GCP_PROJECT_ID, DATASET_ID)
              input_video_id_list = []
              # input_channel = 'GCS'
              st.info("Temporary table ID: %s", temp_table_id)
              for row in matching_rows:
                if row["gcs_object_name"] and not is_video_processed(
                    bq_client,
                    GCP_PROJECT_ID,
                    DATASET_ID,
                    TABLE_ID,
                    row["yyyymmdd"],
                    version,
                    row["gcs_object_name"],
                    row["language_code"],
                    row["voice_id"],
                ):
                  input_video_id_list.append((
                      row["gcs_object_name"],
                      row["client_name"],
                      row["yyyymmdd"],
                      row["version"],
                      row["language_name"],
                      row["language_code"],
                      row["language_full_name"],
                      row["voice_id"],
                      row["voice_gender"],
                      row["original_video_has_vocal_dubbing"],
                      row["voiceover_script_context_prompt"],
                  ))
              logging.info(
                  "Number of videos to be processed: %s",
                  len(input_video_id_list),
              )
              if not input_video_id_list:
                st.info(
                    "All videos for version %s have been processed.", version
                )
                return
              # Initialize Pub/Sub client
              publisher = pubsub_v1.PublisherClient()
              topic_path = publisher.topic_path(GCP_PROJECT_ID, PUBSUB_TOPIC)
              successful_messages = 0
              total_to_process = min(len(input_video_id_list), process_count)
              # Send message to Pub/Sub
              progress_bar = st.progress(0)
              for i, video_id_tuple in enumerate(
                  input_video_id_list[:process_count]
              ):
                (
                    video_id,
                    client_name,
                    yyyymmdd,
                    version,
                    language_name,
                    language_code,
                    language_full_name,
                    voice_id,
                    voice_gender,
                    original_video_has_vocal_dubbing,
                    voiceover_script_context_prompt,
                ) = video_id_tuple
                message = {
                    "trix_id": input_trix_id,
                    "video_id": video_id,
                    "client_name": client_name,
                    "yyyymmdd": yyyymmdd,
                    "version": version,
                    "language_name": language_name,
                    "language_code": language_code,
                    "language_full_name": language_full_name,
                    "voice_id": voice_id,
                    "voice_gender": voice_gender,
                    "original_video_has_vocal_dubbing": (
                        original_video_has_vocal_dubbing
                    ),
                    "voiceover_script_context_prompt": (
                        voiceover_script_context_prompt
                    ),
                    "gcs_bucket_name": gcs_bucket_name,
                    "gcs_top_level_folder": gcs_top_level_folder,
                    "tmp_table_id": temp_table_id,
                }
                try:
                  future = publisher.publish(
                      topic_path,
                      json.dumps(message, ensure_ascii=False).encode("utf-8"),
                  )
                  # Wait for message sending to complete
                  future.result()
                  update_temp_table(
                      GCP_PROJECT_ID,
                      DATASET_ID,
                      temp_table_id,
                      video_id,
                      version,
                  )
                  successful_messages += 1
                except (
                    exceptions.GoogleAPICallError,
                    ValueError,
                    TypeError,
                ) as e:
                  logging.error(
                      "Failed to send message (video_id: %s): %s",
                      video_id,
                      str(e),
                  )
                progress_bar.progress((i + 1) / total_to_process)
                status_message.success(
                    "Successfully sent %s messages to Pub/Sub (processing"
                    " progress: %s/%s)",
                    successful_messages,
                    i + 1,
                    total_to_process,
                )
              # final status update
              status_message.success(
                  "Processing completed! Successfully sent"
                  " %s messages to Pub/Sub (total"
                  " processed: %s)",
                  successful_messages,
                  total_to_process,
              )
              st.info("Temporary table ID: %s", temp_table_id)
            except (
                exceptions.GoogleAPICallError,
                ValueError,
                TypeError,
            ) as e:
              logging.error("An error occurred during processing: %s", str(e))
              st.error(f"An error occurred during processing: {e}")
    elif page == "Processing Results Preview":
      st.subheader("Processing Results Preview")
      temp_table_id = st.text_input("Please enter temporary table ID")
      if st.button("View Results"):
        display_table_results(temp_table_id)
  except (exceptions.GoogleAPICallError, ValueError) as e:
    logging.error("Uncaught error occurred in main function: %s", str(e))
    st.error(f"An error occurred: {e}")


if __name__ == "__main__":
  main()
