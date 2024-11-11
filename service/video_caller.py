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

"""Video Caller for Video Mate."""

import json
import logging
import time

from google.api_core import retry
from service import video_voiceover
import voiceover_config
import voiceover_utils


logging.basicConfig(
    format=(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s'
    ),
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Set project ID and subscription
project_id = voiceover_config.GCP_PROJECT_ID
subscription_id = voiceover_config.SUBSCRIPTION_ID

# Initialize service subscriber client
subscriber = voiceover_utils.pubsub_v1.SubscriberClient()
subscription_path = subscriber.subscription_path(project_id, subscription_id)
tts_client = voiceover_utils.texttospeech.TextToSpeechClient()
storage_client = voiceover_utils.storage.Client()
gemini_1_5_pro_model = voiceover_utils.GenerativeModel('gemini-1.5-pro')
gemini_1_5_flash_model = voiceover_utils.GenerativeModel(
    model_name='gemini-1.5-flash'
)


def process_message(
    message: voiceover_utils.pubsub_v1.types.ReceivedMessage,
) -> None:
  """Process a single message from the Pub/Sub subscription.

  This function handles incoming messages from a Pub/Sub subscription.
  It parses the message data, extracts relevant information, initializes a
  `VideoVoiceover` object, and then processes the video using the
  `process_video` function.

  Args:
    message: The message received from the Pub/Sub subscription.

  Raises:
    Exception: If any error occurs during message processing.
  """
  try:
    # Parse message data
    data = json.loads(message.data.decode('utf-8'))
    # Extract necessary information from the message
    trix_id = data.get('trix_id')
    video_id = data.get('video_id')
    client_name = data.get('client_name')
    yyyymmdd = data.get('yyyymmdd')
    version = data.get('version')
    language_name = data.get('language_name')
    language_code = data.get('language_code')
    language_full_name = data.get('language_full_name')
    voice_id = data.get('voice_id')
    voice_gender = data.get('voice_gender')
    original_video_has_vocal_dubbing = data.get(
        'original_video_has_vocal_dubbing'
    )
    voiceover_script_context_prompt = data.get(
        'voiceover_script_context_prompt'
    )
    gcs_bucket_name = data.get('gcs_bucket_name')
    gcs_top_level_folder = data.get('gcs_top_level_folder')
    tmp_table_id = data.get('tmp_table_id')

    # Initialize VideoVoiceover
    vv = video_voiceover.VideoVoiceover(
        gemini_pro_model=gemini_1_5_pro_model,
        tts_client=tts_client,
        gcs_client=storage_client,
        trix_id=trix_id,
        video_id=video_id,
        client_name=client_name,
        yyyymmdd=yyyymmdd,
        version=version,
        language_name=language_name,
        language_code=language_code,
        language_full_name=language_full_name,
        voice_id=voice_id,
        voice_gender=voice_gender,
        original_video_has_vocal_dubbing=original_video_has_vocal_dubbing,
        voiceover_script_context_prompt=voiceover_script_context_prompt,
        tmp_path='/tmp',
        gcs_bucket_name=gcs_bucket_name,
        gcs_top_level_folder=gcs_top_level_folder,
        tmp_table_id=tmp_table_id,
        err_msg='',
    )
    process_video(vv)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.error('Error while processing message: %s', str(e))


def process_video(vv: video_voiceover.VideoVoiceover) -> bool:
  """Process a single video to generate a voiceover.

  This function manages the end-to-end process of generating a voiceover
  for a video. It performs the following steps:

  1. Check for Existing Video: If the final video already exists, it skips
     processing and returns True.
  2. Download Video and Audio: Downloads the original video and its
     associated audio from YouTube (or another source) if they don't
     already exist in Google Cloud Storage.
  3. Generate Script: Uses a language model (e.g., Gemini Pro) to generate a
     voiceover script based on the video content and context.
  4. Synthesize Speech: Converts the generated script into speech audio using
     a text-to-speech service.
  5. Generate Video File: Combines the original video, background audio, and
     synthesized speech to create the final video with voiceover.
  6. Update Status and Results:  Updates the processing status in a BigQuery
     table and writes the results (success or failure) to another BigQuery
     table.

  Args:
    vv: A `VideoVoiceover` object containing information about the video to be
      processed.

  Returns:
    True if the video was processed successfully, False otherwise.
  """
  if vv.check_if_generated_video_exists():
    logger.info('The generated video file exists. video_id: %s', vv.video_id)
    vv.write_result_to_bigquery('ALREADY_EXISTS', '')
    return True

  # Helper function to handle common failure logic
  def handle_failure(err_msg: str):
    logger.error(err_msg)
    vv.write_result_to_bigquery('GENERATE_FAILED', err_msg)
    vv.update_status_in_temp_table('GENERATE_FAILED')
    return False

  # Check and download videos
  if not vv.check_if_video_exists():
    success, _, err_msg = vv.gcs_download_and_upload_youtube_video(
        vv.video_id, f'{vv.video_id}.mp4'
    )
    if not success:
      return handle_failure(f'Failed to download video: {err_msg}')

  # Check and download basic audio
  if not vv.check_if_base_audio_exists():
    success, _, err_msg = vv.gcs_download_and_upload_youtube_video(
        vv.video_id, f'{vv.video_id}_base_audio.mp4'
    )
    if not success:
      return handle_failure(f'Failed to download basic audio: {err_msg}')

  # Generate scripts and synthesize speech
  if not vv.check_if_speech_exists():
    try:
      vv.generate_script()
      success, err_msg = vv.synthesize_speech()
      if not success:
        return handle_failure(f'Synthetic speech failed: {err_msg}')
    except Exception as e:  # pylint: disable=broad-exception-caught
      return handle_failure(
          f'Error generating script or synthesizing speech: {e}'
      )

  # Generate video files
  if vv.generate_video_file():
    vv.write_result_to_bigquery('GENERATE_SUCCESS', '')
    vv.update_status_in_temp_table('GENERATE_SUCCESS')
    logger.info('Complete BQ data update')
    return True
  else:
    return handle_failure('Failed to generate video file')


def main():
  logger.info('Start receiving messages from %s...', subscription_path)
  while True:
    try:
      # Synchronously pull a single message
      response = subscriber.pull(
          request={'subscription': subscription_path, 'max_messages': 1},
          retry=retry.Retry(deadline=300),
      )
      if not response.received_messages:
        logger.info('No new messages received, keep waiting...')
        continue
      for received_message in response.received_messages:
        message = received_message.message
        logger.info('message received: %s', message.data)

        try:
          # Process messages
          process_message(message)
          # Confirm message directly
          subscriber.acknowledge(
              request={
                  'subscription': subscription_path,
                  'ack_ids': [received_message.ack_id],
              }
          )
        except Exception as e:  # pylint: disable=broad-exception-caught
          logger.error('Error while processing message: %s', str(e))
          # If processing fails, not to acknowledge the message and have it
          # resent later. Or send it to the dead letter queue.
    except KeyboardInterrupt:
      logger.info('Program interrupted by user')
      break
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.error('Error while pulling message: %s', str(e))
      # add a short wait here to avoid retrying too frequently in case of
      # errors.
      time.sleep(5)
  subscriber.close()


if __name__ == '__main__':
  main()
