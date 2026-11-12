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

"""VideoVoiceover Class and Helper Methods for Video Mate."""

import dataclasses
import datetime
import logging
import os
import random
import re
import shutil
import string
import subprocess
from typing import Optional

from google.cloud import texttospeech
import librosa
from vertexai.generative_models import SafetySetting
import voiceover_config
import voiceover_utils

logging.basicConfig(
    format=(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s'
    ),
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_CHARS_PER_SECOND = 11
_MAX_SPEED_ADJUST = 1.2
_RANDOM_STRING_LENGTH = 12
_MAX_DELAY_IN_MILLISECONDS = 800
_MILLISECONDS_IN_SECOND = 1000
_SAFE_EXTENSION_RE = re.compile(r'^[A-Za-z0-9]{1,10}$')


def _extract_safe_extension(file_path: str) -> str:
  """Extracts a validated alphanumeric file extension from a media path."""
  _, ext = os.path.splitext(os.path.basename(file_path))
  ext = ext.lstrip('.')
  if not _SAFE_EXTENSION_RE.fullmatch(ext):
    raise ValueError(f'Invalid media file extension in path: {file_path!r}')
  return ext


@dataclasses.dataclass
class VideoVoiceover:
  """A class for video voiceover.

  Attributes:
    logger: A logging object for recording events and errors.
    version: A version string for the video (e.g., "v1", "v2").
    yyyymmdd: A date in YYYYMMDD format.
    video_length: The duration of the video in seconds.
    ssml_gender: Specifying gender in SSML
    download_path: The local directory where the video is downloaded.
    audio_path: The local path to the audio file extracted or generated from the
      video.
    output_path: The local directory where the final output video with voiceover
      is saved.
    input_path: The local path to the original video file.
    direct_script: A pre-written script for the voiceover.
    gemini_pro_model: An instance of the Gemini Pro language model.
    tts_client: A client object for a text-to-speech service.
    gcs_client: A client object for interacting with Google Cloud Storage.
    trix_id: The spreadsheet ID of the videos.
    video_id: YouTube video ID.
    client_name: The name of the client.
    language_name: Target short language name of the voiceover.
    language_code: Target language code of the voiceover.
    language_full_name: Target full language name of the voiceover.
    voice_id: Target voice id of the voiceover.
    voice_gender: The gender of the synthetic voice.
    original_video_has_vocal_dubbing: Indicates if the original video already
      has a dubbed voiceover.
    voiceover_script_context_prompt: A prompt provided to Gemini Pro to generate
      the voiceover script.
    tmp_path: A temporary directory for intermediate files.
    gcs_bucket_name: The name of a Google Cloud Storage bucket.
    gcs_top_level_folder: The top-level folder within the GCS bucket.
    tmp_table_id: The ID of a temporary table in BigQuery.
    err_msg: An optional string to store error messages.
    max_retry_count: The maximum number of times to retry an operation (defaults
      to 3).
  """

  gemini_pro_model: any
  tts_client: any
  gcs_client: any
  trix_id: str
  video_id: str
  client_name: str
  yyyymmdd: str
  version: str
  language_name: str
  language_code: str
  language_full_name: str
  voice_id: str
  voice_gender: str
  original_video_has_vocal_dubbing: bool
  voiceover_script_context_prompt: str
  tmp_path: str
  gcs_bucket_name: str
  gcs_top_level_folder: str
  tmp_table_id: str
  err_msg: Optional[str] = None
  direct_script: str = ''
  max_retry_count: int = 3
  logger: logging.Logger = dataclasses.field(init=False)
  video_length: Optional[float] = None
  ssml_gender: Optional[any] = None

  def __post_init__(self) -> None:
    self.logger = logging.getLogger(__name__)
    self.version = str(self.version)
    self.yyyymmdd = str(self.yyyymmdd)
    self._initialize_paths()
    self._validate_inputs()
    success, duration, err_msg = self.gcs_download_and_upload_youtube_video(
        self.video_id, f'{self.video_id}.mp4'
    )
    if success:
      self.video_length = duration
      self.ssml_gender = self._get_ssml_gender()
    else:
      self.logger.error('Video download/upload failed: %s', err_msg)
      self.update_status_in_temp_table('GENERATE_FAILED')
      self.write_result_to_bigquery(
          'GENERATE_FAILED', f'Video download/upload failed: {err_msg}'
      )
      raise RuntimeError(f'Failed to initialize VideoVoiceover: {err_msg}')

  def _initialize_paths(self) -> None:
    """Initialize file paths for video processing.

    This function sets up the directory paths required for storing
    and accessing files during video processing. It defines paths for:

    - `download_path`:  Where the original video and audio are downloaded.
    - `audio_path`: Where processed audio files (e.g., synthesized speech)
                    are stored.
    - `output_path`: Where the final generated video is saved.
    - `input_path`: Where the input video is located.

    These paths are constructed using attributes like `gcs_top_level_folder`,
    `client_name`, `yyyymmdd`, `language_code`, and `version` to create a
    structured directory hierarchy.
    """
    self.download_path = os.path.join(
        self.gcs_top_level_folder,
        self.client_name,
        self.yyyymmdd,
        'downloads',
        self.language_code,
        self.version,
    )
    self.audio_path = os.path.join(
        self.gcs_top_level_folder,
        self.client_name,
        self.yyyymmdd,
        'audio',
        self.language_code,
        self.version,
    )
    self.output_path = os.path.join(
        self.gcs_top_level_folder,
        self.client_name,
        self.yyyymmdd,
        'output',
        self.language_code,
        self.version,
    )
    self.input_path = os.path.join(
        self.gcs_top_level_folder,
        self.client_name,
        self.yyyymmdd,
        'input',
        self.version,
    )

  def _validate_inputs(self) -> None:
    """Validate the input parameters.

    This function checks if the `video_id` is provided.

    Raises:
      ValueError: If `video_id` is empty.
    """
    if not self.video_id:
      raise ValueError('video_id cannot be empty')

  def _get_ssml_gender(self) -> texttospeech.SsmlVoiceGender:
    """Get the SSML voice gender.

    This function maps the `voice_gender` attribute (string) to the
    corresponding `texttospeech.SsmlVoiceGender` enum value.

    Returns:
      The `texttospeech.SsmlVoiceGender` enum value.

    Raises:
      ValueError: If the `voice_gender` is not "MALE" or "FEMALE".
    """
    if self.voice_gender == 'MALE':
      return texttospeech.SsmlVoiceGender.MALE
    elif self.voice_gender == 'FEMALE':
      return texttospeech.SsmlVoiceGender.FEMALE
    else:
      self.logger.error('Unsupported Voice Gender: %s', self.voice_gender)
      raise ValueError(f'Unsupported Voice Gender: {self.voice_gender}')

  def _get_speech_file_name(self) -> str:
    """Get the file name for the synthesized speech.

    Returns:
      The file name for the synthesized speech audio file, which includes the
      video ID, voice ID, voice gender, and language code, along with the
      .mp3 extension.
    """
    return f'{self.video_id}__{self.voice_id}_{self.voice_gender}_{self.language_code}.mp3'

  def _get_video_file_name(self) -> str:
    """Get the file name of the original video.

    Returns:
      The file name of the original video, including the .mp4 extension.
    """
    return f'{self.video_id}.mp4'

  def _get_audio_file_name(self) -> str:
    """Get the file name of the base audio.

    Returns:
      The file name of the base audio extracted from the original video,
      including the .mp4 extension.
    """
    return f'{self.video_id}_base_audio.mp4'

  def _get_generated_video_file_name(self) -> str:
    """Get the file name for the generated video with voiceover.

    Returns:
      The file name for the final generated video, which includes the
      video ID, voice ID, voice gender, and language code, along with
      the .mp4 extension.
    """
    return f'{self.video_id}__{self.voice_id}_{self.voice_gender}_{self.language_code}.mp4'

  def _get_audio_length(self, path: str) -> float:
    """Get the length of an audio file.

    Args:
      path: The path to the audio file.

    Returns:
      The duration of the audio in seconds as a float.
    """
    return librosa.get_duration(path=path)

  def _get_video_duration(self, video_path: str) -> float:
    """Get the duration of a video using ffprobe.

    This function uses the `ffprobe` command-line tool to extract the
    duration of the video at the specified path.

    Args:
      video_path: The path to the video file.

    Returns:
      The duration of the video in seconds as a float.

    Raises:
      subprocess.CalledProcessError: If the `ffprobe` command fails.
    """
    result = subprocess.run(
        [
            'ffprobe',
            '-v',
            'error',
            '-show_entries',
            'format=duration',
            '-of',
            'default=noprint_wrappers=1:nokey=1',
            video_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    return float(result.stdout)

  def check_audio_file(self, file_path: str) -> str:
    """Check the audio file using ffprobe.

    This function uses the `ffprobe` command-line tool to extract
    information about the audio stream in the specified file.

    Args:
      file_path: The path to the audio file to check.

    Returns:
      A string containing comma-separated values with the following
      information about the audio stream:

        - codec_type: The type of audio codec (e.g., "audio").
        - codec_name: The name of the audio codec (e.g., "aac", "mp3").
        - duration: The duration of the audio stream in seconds.

      Example: "audio,aac,120.54"

    Raises:
      subprocess.CalledProcessError: If the `ffprobe` command fails.
    """
    result = subprocess.run(
        [
            'ffprobe',
            '-v',
            'error',
            '-select_streams',
            'a:0',
            '-count_packets',
            '-show_entries',
            'stream=codec_type,codec_name,duration',
            '-of',
            'csv=p=0',
            file_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()

  def _clear_tmp_folder(self) -> None:
    """Clear the temporary folder.

    This function removes all files and directories within the
    `tmp_path` directory. It's typically used to clean up temporary
    files created during video processing.
    """
    for filename in os.listdir(self.tmp_path):
      file_path = os.path.join(self.tmp_path, filename)
      try:
        if os.path.isfile(file_path) or os.path.islink(file_path):
          os.unlink(file_path)
        elif os.path.isdir(file_path):
          shutil.rmtree(file_path)
      except OSError as e:
        print(f'Failed to delete {file_path}. Reason: {e}')

  def write_result_to_bigquery(self, status: str, err_msg=None) -> None:
    """Write the result of the video processing to BigQuery.

    Args:
      status: The status of the video processing (e.g., "GENERATE_SUCCESS",
        "GENERATE_FAILED").
      err_msg: An optional error message if the processing failed.
    """
    file_location = f'gs://{self.gcs_bucket_name}/{self.output_path}/{self._get_generated_video_file_name()}'
    if status == 'GENERATE_FAILED':
      file_location = ''
    date_obj = datetime.datetime.strptime(self.yyyymmdd, '%Y%m%d')
    formatted_date = date_obj.strftime('%Y-%m-%d')
    row = {
        'trixid': self.trix_id,
        'client_name': self.client_name,
        'date': formatted_date,
        'version': self.version,
        'language_code': self.language_code,
        'voice_id': self.voice_id,
        'video_id': self.video_id,
        'direct_script': self.direct_script,
        'status': status,
        'file_location': file_location,
        'err_msg': err_msg,
    }
    project_id = voiceover_config.GCP_PROJECT_ID
    dataset_id = voiceover_config.DATASET_ID
    table_id = voiceover_config.TABLE_ID
    if row is not None:
      voiceover_utils.write_to_bigquery(project_id, dataset_id, table_id, row)
    else:
      logger.error('Row object is None, cannot write to BigQuery')

  def update_status_in_temp_table(self, new_status: str) -> None:
    """Update the status of the video in the temporary BigQuery table.

    This function updates the `status` column for the specified video
    and version in the temporary BigQuery table.

    Args:
      new_status: The new status to set for the video in the table.

    Raises:
      ValueError: If `tmp_table_id` is not a valid BigQuery table identifier.
      Exception: If any error occurs during the BigQuery update operation.
    """
    project_id = voiceover_config.GCP_PROJECT_ID
    dataset_id = voiceover_config.DATASET_ID
    table_id = self.tmp_table_id
    allowed_chars = frozenset(string.ascii_letters + string.digits + '_')
    if (
        not isinstance(table_id, str)
        or not 1 <= len(table_id) <= 1024
        or not set(table_id).issubset(allowed_chars)
    ):
      raise ValueError('invalid BigQuery table_id')
    client = voiceover_utils.bigquery.Client(project=project_id)
    query = f"""
    UPDATE `{project_id}.{dataset_id}.{table_id}`
    SET status = @new_status
    WHERE video_id = @video_id AND version = @version
    """
    job_config = voiceover_utils.bigquery.QueryJobConfig(
        query_parameters=[
            voiceover_utils.bigquery.ScalarQueryParameter(
                'new_status', 'STRING', str(new_status)
            ),
            voiceover_utils.bigquery.ScalarQueryParameter(
                'video_id', 'STRING', str(self.video_id)
            ),
            voiceover_utils.bigquery.ScalarQueryParameter(
                'version', 'STRING', str(self.version)
            ),
        ]
    )
    try:
      query_job = client.query(query, job_config=job_config)
      query_job.result()
      logger.info(
          "Updated status to '%s' for video_id: %s, version: %s",
          new_status,
          self.video_id,
          self.version,
      )
    except Exception as e:
      logger.error('Error updating status in temporary table: %s', str(e))
      raise

  def gcs_download_and_upload_youtube_video(
      self, video_id: str, file_name: str
  ) -> tuple[bool, float, str]:
    """Downloads a YouTube video and uploads it to a Google Cloud Storage bucket.

    Args:
      video_id: The video file name of the video in GCS bucket - eg. abcd
      file_name: The video file name with ext of the video in GCS bucket - eg.
        abcd.mp4

    Returns:
      A tuple containing:
        - success: A boolean indicating whether the operation was successful.
        - duration: The duration of the video in seconds.
        - err_msg: An error message if any occurred, or None if successful.
    """
    err_msg = ''
    # Download to local
    local_video_path = f'{video_id}.mp4'
    try:
      # Download to local
      bucket = self.gcs_client.bucket(self.gcs_bucket_name)
      download_blob = bucket.blob(f'{self.input_path}/{local_video_path}')
      download_blob.download_to_filename(f'{local_video_path}')
      duration = self._get_video_duration(local_video_path)
      # Upload to GCS
      blob = bucket.blob(f'{self.download_path}/{file_name}')
      blob.upload_from_filename(local_video_path)
      self.logger.info(
          'Video downloaded and uploaded to gs://%s/%s',
          self.download_path,
          file_name,
      )
    except Exception as e:  # pylint: disable=broad-exception-caught
      self.logger.error(
          'An error occurred when downloading the video(%s): %s', video_id, e
      )
      err_msg = f'An error occurred when downloading the video({video_id}): {e}'
      return False, None, err_msg
    finally:
      # Clean up the downloaded file
      if os.path.exists(local_video_path):
        os.remove(local_video_path)
    return True, duration, err_msg

  def check_if_generated_video_exists(self) -> bool:
    """Check if the generated video file already exists in GCS.

    Returns:
      True if the generated video file exists, False otherwise.
    """
    bucket = self.gcs_client.bucket(self.gcs_bucket_name)
    path = f'{self.output_path}/{self._get_generated_video_file_name()}'
    blob = bucket.blob(path)
    return blob.exists()

  def check_if_speech_exists(self) -> bool:
    """Check if the synthesized speech audio file exists in GCS.

    Returns:
      True if the speech file exists, False otherwise.
    """
    bucket = self.gcs_client.bucket(self.gcs_bucket_name)
    path = f'{self.audio_path}/{self._get_speech_file_name()}'
    blob = bucket.blob(path)
    return blob.exists()

  def check_if_base_audio_exists(self) -> bool:
    """Check if the base audio file exists in GCS.

    Returns:
      True if the base audio file exists, False otherwise.
    """
    bucket = self.gcs_client.bucket(self.gcs_bucket_name)
    path = f'{self.download_path}/{self._get_audio_file_name()}'
    blob = bucket.blob(path)
    return blob.exists()

  def check_if_video_exists(self) -> bool:
    """Check if the original video file exists in GCS.

    Returns:
      True if the video file exists, False otherwise.
    """
    bucket = self.gcs_client.bucket(self.gcs_bucket_name)
    path = f'{self.download_path}/{self._get_video_file_name()}'
    blob = bucket.blob(path)
    return blob.exists()

  def generate_script(self) -> None:
    """Generate a voiceover script using Gemini Pro.

    This function generates a voiceover script for the video using the
    Gemini Pro language model. It takes into account the video's length,
    language, and the provided context prompt. If the initially generated
    script is too long, it attempts to shorten it while preserving key
    information and call-to-action phrases.

    The function sets parameters for the Gemini Pro model, including
    temperature, token limits, and safety settings. It constructs a prompt
    with instructions and context, and then sends it to the model.
    The generated script is stored in the `direct_script` attribute.
    """
    parameters = {
        'temperature': (
            0.8
        ),  # Temperature controls the degree of randomness in token selection.
        'max_output_tokens': (
            500
        ),  # Token limit determines the maximum amount of text output.
        'top_p': (
            0.80
        ),  # Tokens are selected from most probable to least until the sum of
        # their probabilities equals the top_p value.
        'top_k': (
            40
        ),  # A top_k of 1 means the selected token is the most probable among
        # all tokens.
    }
    safety_settings = [
        SafetySetting(
            category=SafetySetting.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            threshold=SafetySetting.HarmBlockThreshold.BLOCK_NONE,
        ),
        SafetySetting(
            category=SafetySetting.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            threshold=SafetySetting.HarmBlockThreshold.BLOCK_NONE,
        ),
        SafetySetting(
            category=SafetySetting.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            threshold=SafetySetting.HarmBlockThreshold.BLOCK_NONE,
        ),
        SafetySetting(
            category=SafetySetting.HarmCategory.HARM_CATEGORY_HARASSMENT,
            threshold=SafetySetting.HarmBlockThreshold.BLOCK_NONE,
        ),
    ]
    video_uri = f'gs://{self.gcs_bucket_name}/{self.download_path}/{self._get_video_file_name()}'
    text_prompt = f"""
    You are an expertise on video voiceover script writing. Please generate a voiceover script based on the following context:
    \n
    {self.voiceover_script_context_prompt}
    \n
    Please follow the below requirements when generating the voiceover script: \n
    1. Please generate a voiceover script that is less than {self.video_length} seconds. \n
    2. Please provide the script in language of {self.language_full_name}.
    """
    self.logger.info('text_prompt: %s', text_prompt)
    self.direct_script = voiceover_utils.get_gemini_response_with_retry(
        text_prompt,
        video_uri,
        self.gemini_pro_model,
        parameters,
        safety_settings,
        voiceover_config.PROJECT_ID,
    )
    self.logger.info(
        'voice over script: %s, script length: %s',
        self.direct_script,
        len(self.direct_script),
    )
    max_rewrite_count = 5
    count = 0
    while (
        count <= max_rewrite_count
        and len(self.direct_script) >= self.video_length * _CHARS_PER_SECOND
    ):
      text_prompt = (
          'You are an expertise on video voiceover script writing. Please'
          ' shorten the original voiceover script by dropping some of the'
          ' detailed information. Please try to keep some attractive keywords'
          ' related to the business context, and also maintain the'
          ' call-to-action phrases if possible.\n Original voiceover script:'
          f' {self.direct_script}. \n Requirements: 1. Please do not provide'
          ' multiple options for me to choose, but directly provide the'
          ' shortened version without any other words or phrases.\n 2. Please'
          f' return the script in language of {self.language_full_name}. '
      )
      self.direct_script = voiceover_utils.get_gemini_response_with_retry(
          text_prompt,
          None,
          self.gemini_pro_model,
          parameters,
          safety_settings,
          voiceover_config.PROJECT_ID,
      )
      self.logger.info('%s, %s', self.direct_script, len(self.direct_script))
      count += 1

  def synthesize_speech(self) -> tuple[bool, str]:
    """Synthesize speech from the generated script using Text-to-Speech.

    This function uses the Google Cloud Text-to-Speech API to convert the
    `direct_script` into an audio file. It configures the voice, language,
    and audio encoding. The synthesized speech is temporarily saved to a local
    file, then its length is checked against the video length. If the speech
    is significantly longer than the video, it's discarded and an error is
    returned. Otherwise, the audio file is uploaded to Google Cloud Storage.

    Returns:
      A tuple containing:
        - A boolean indicating whether the speech synthesis and upload were
          successful.
        - An error message if there was a failure, otherwise an empty string.
    """
    synthesis_input = texttospeech.SynthesisInput(text=self.direct_script)
    voice = texttospeech.VoiceSelectionParams(
        language_code=self.language_code,
        name=self.voice_id,
        ssml_gender=self.ssml_gender,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3, speaking_rate=1
    )
    speech_response = self.tts_client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )
    speech_file_name = self._get_speech_file_name()
    tmp_speech_file_name = f'{self.tmp_path}/{speech_file_name}'

    with open(tmp_speech_file_name, 'wb') as out:
      # Write the response to the output file.
      out.write(speech_response.audio_content)
    voice_dub_length = self._get_audio_length(tmp_speech_file_name)
    speed_adjust = max(1, voice_dub_length / self.video_length)
    err_msg = ''
    if speed_adjust > _MAX_SPEED_ADJUST:
      err_msg = (
          f'Speech length is longer than video length. {voice_dub_length} >'
          f' {self.video_length}, speed_adjust:{speed_adjust}'
      )
      print(err_msg)
      os.remove(tmp_speech_file_name)
      return False, err_msg
    else:
      voiceover_utils.copy_file_to_gcs(
          self.gcs_bucket_name,
          tmp_speech_file_name,
          f'{self.audio_path}/{speech_file_name}',
      )
      os.remove(tmp_speech_file_name)
      return True, err_msg

  def generate_video_file(self) -> bool:
    """Generate the final video file with voiceover.

    This function orchestrates the process of creating the final video
    file that includes the synthesized voiceover. It involves the following
    steps:

    1. Prepare File Paths: Define paths for video, audio, and speech files,
       both in GCS and locally.
    2. Copy from GCS: Copy necessary files (video, original audio, synthesized
       speech) from Google Cloud Storage to the local temporary directory.
    3. Optional Vocal Removal: If the original video has a vocal dub, attempt
       to remove it using `ffmpeg`.
    4. Audio Normalization: Normalize the audio track (with or without vocal
       removal) using `ffmpeg-normalize`.
    5. Mix Audio and Video: Use `ffmpeg` to combine the video, background audio,
       and synthesized speech, adjusting the speech speed and timing if
       necessary.
    6. Upload to GCS: Upload the final generated video to Google Cloud Storage.
    7. Cleanup: Remove temporary files from the local directory.

    Returns:
      True if the video file was generated and uploaded successfully,
      False otherwise.
    """
    video_file = f'{self.download_path}/{self._get_video_file_name()}'
    audio_file = f'{self.download_path}/{self._get_audio_file_name()}'
    speech_file = f'{self.audio_path}/{self._get_speech_file_name()}'
    generated_video_file = (
        f'{self.output_path}/{self._get_generated_video_file_name()}'
    )
    random_string = ''.join(
        random.choices(
            string.ascii_lowercase + string.digits, k=_RANDOM_STRING_LENGTH
        )
    )
    video_extension = _extract_safe_extension(video_file)
    tmp_video_file = f'{self.tmp_path}/video_{random_string}.{video_extension}'
    sound_extension = _extract_safe_extension(audio_file)
    tmp_audio_file = (
        f'{self.tmp_path}/base_audio_{random_string}.{sound_extension}'
    )
    tmp_audio_file_after_vocal_removal = tmp_audio_file
    speech_extension = _extract_safe_extension(speech_file)
    tmp_speech_file = (
        f'{self.tmp_path}/speech_{random_string}.{speech_extension}'
    )
    tmp_generated_video_file = f'{self.tmp_path}/output_{random_string}.mp4'
    voiceover_utils.copy_file_from_gcs(
        self.gcs_bucket_name, speech_file, tmp_speech_file
    )
    voiceover_utils.copy_file_from_gcs(
        self.gcs_bucket_name, video_file, tmp_video_file
    )
    voiceover_utils.copy_file_from_gcs(
        self.gcs_bucket_name, audio_file, tmp_audio_file
    )
    if (
        self.original_video_has_vocal_dubbing
        and self.original_video_has_vocal_dubbing == 'YES'
    ):
      logger.info('original_video_has_vocal_dubbing: YES')
      tmp_audio_file_removed_original_vocal = (
          f'{self.tmp_path}/base_audio_{random_string}'
          f'_removed_original_vocal.{sound_extension}'
      )
      # Remove the vocals from the original video by trying to remove or
      # attenuate the center channel sound
      # ffmpeg -i input.mp4
      # -af "pan=stereo|c0=c0-c1|c1=c0-c1" -c:v copy output.mp4
      ffmpeg_remove_original_vocal_dub_command = [
          'ffmpeg',
          '-i',
          tmp_audio_file,
          '-af',
          'pan=stereo|c0=c0-c1|c1=c0-c1',
          '-c:v',
          'copy',
          '-y',
          tmp_audio_file_removed_original_vocal,
      ]
      logger.info(
          'Running command to remove original vocal dub: %s',
          ffmpeg_remove_original_vocal_dub_command,
      )
      try:
        subprocess.run(
            ffmpeg_remove_original_vocal_dub_command,
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info('execute: %s', ffmpeg_remove_original_vocal_dub_command)
        video_path = f'{tmp_audio_file_removed_original_vocal}'
        # Upload to Google Cloud Storage
        bucket = self.gcs_client.bucket(self.gcs_bucket_name)
        blob = bucket.blob(
            f'{self.download_path}/{self.video_id}'
            '_base_audio_removed_original_vocal.mp4'
        )
        blob.upload_from_filename(video_path)
        logger.info(
            'Video voal removed and uploaded to gs://%s/%s',
            self.download_path,
            self.video_id + '_base_audio_removed_original_vocal.mp4',
        )
        tmp_audio_file_after_vocal_removal = (
            tmp_audio_file_removed_original_vocal
        )
        logger.info('file location %s', tmp_audio_file_after_vocal_removal)
      except Exception as e:  # pylint: disable=broad-exception-caught
        self.logger.error('Failed to remove original vocal dub: %s', e)
    ffmpeg_normalize_command = [
        'ffmpeg-normalize',
        tmp_audio_file_after_vocal_removal,
        '-o',
        tmp_audio_file_after_vocal_removal,
        '-f',
        '-c:a',
        'aac',
    ]
    self.logger.info(
        'Start executing ffmpeg-normalize command: %s', ffmpeg_normalize_command
    )
    try:
      result = subprocess.run(
          ffmpeg_normalize_command,
          check=True,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          text=True,
      )
      self.logger.info(
          'ffmpeg-normalize command is executed successfully. output: %s',
          result.stdout,
      )
    except subprocess.CalledProcessError as e:
      self.logger.error(
          'ffmpeg-normalize command failed to execute. error: %s', e.stderr
      )
      raise
    self.logger.info('ffmpeg-normalize command execution completed')
    voice_dub_length = self._get_audio_length(tmp_speech_file)
    speed_adjust = max(1, voice_dub_length / self.video_length)
    millisecond_start_audio = max(
        min(
            _MAX_DELAY_IN_MILLISECONDS,
            (self.video_length - voice_dub_length) * _MILLISECONDS_IN_SECOND,
        ),
        0,
    )
    filter_complex = (
        f'[2:a]atempo={speed_adjust},adelay={millisecond_start_audio}:all=1'
        '[voice_dub];[1:a][voice_dub]amix=inputs=2:duration=longest[audio_out]'
    )
    ffmpeg_mix_command = [
        'ffmpeg',
        '-loglevel',
        'info',
        '-progress',
        'pipe:1',
        '-i',
        tmp_video_file,
        '-i',
        tmp_audio_file_after_vocal_removal,
        '-i',
        tmp_speech_file,
        '-filter_complex',
        filter_complex,
        '-c:v',
        'copy',
        '-c:a',
        'aac',
        '-b:a',
        '192k',
        '-map',
        '0:v',
        '-map',
        '[audio_out]',
        '-y',
        tmp_generated_video_file,
    ]
    try:
      self.logger.info(
          'Start executing the ffmpeg command for mix: %s', ffmpeg_mix_command
      )
      result = subprocess.run(
          ffmpeg_mix_command,
          check=True,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          text=True,
      )
      self.logger.info(
          'ffmpeg command for mix is executed successfully. output: %s',
          result.stdout,
      )
    except subprocess.CalledProcessError as e:
      self.logger.error(
          'ffmpeg command for mix failed to execute. error: %s', e.stderr
      )
    try:
      voiceover_utils.copy_file_to_gcs(
          self.gcs_bucket_name, tmp_generated_video_file, generated_video_file
      )
      os.remove(tmp_video_file)
      os.remove(tmp_audio_file)
      os.remove(tmp_speech_file)
      os.remove(tmp_generated_video_file)
      if (
          tmp_audio_file_after_vocal_removal != tmp_audio_file
          and os.path.exists(tmp_audio_file_after_vocal_removal)
      ):
        os.remove(tmp_audio_file_after_vocal_removal)
      return True
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(e)
      return False
