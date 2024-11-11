#!/bin/bash
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

set -e

# Modify your parameters
export PROJECT_ID='YOUR_PROJECT_ID'
export REGION='YOUR_REGION'
export VPC_NETWORK='voiceover-network'
export SUBNET='voiceover-subnet'

# Default parameters
export GKE_CLUSTER_NAME=YOUR_GKE_CLUSTER_NAME
export LABEL_SERVER_IMAGE="YOUR_REGION-docker.pkg.dev/YOUR_PROJECT_ID/YOUR_IMAGE_REPOSITORY_NAME/vv-dispatch:TAG"
export LABEL_WORKER_IMAGE="YOUR_REGION-docker.pkg.dev/YOUR_PROJECT_ID/YOUR_IMAGE_REPOSITORY_NAME/vv-worker:TAG"
export TOPIC_NAME=YOUR_PUBSUB_TOPIC
export TOPIC=projects/${PROJECT_ID}/topics/${TOPIC_NAME}
export SUBSCRIPTION=projects/${PROJECT_ID}/subscriptions/${TOPIC_NAME}-sub
export ZONE=${REGION}-b
export SERVICE_ACCOUNT=YOUR_SERVICE_ACCOUNT_NAME

echo "Enable services ... "
gcloud services enable compute.googleapis.com \
    cloudresourcemanager.googleapis.com \
    artifactregistry.googleapis.com \
    pubsub.googleapis.com \
    container.googleapis.com \
    aiplatform.googleapis.com \
    bigquery.googleapis.com \
    sheets.googleapis.com \
    storage.googleapis.com \
    network.googleapis.com \
    --project ${PROJECT_ID}

echo "Create service account for voiveover application ... "
gcloud iam service-accounts create ${SERVICE_ACCOUNT} \
    --description="Service account for voiceovcer" \
    --display-name="for voiceover" \
    --project=${PROJECT_ID}

gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member=serviceAccount:${SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com \
    --role='roles/editor' \
    --condition=None \
    --quiet > /dev/null

echo "Create public gke cluster ..."
gcloud beta container --project "${PROJECT_ID}" clusters create-auto "${GKE_CLUSTER_NAME}" \
    --region "${REGION}" \
    --release-channel "stable" \
    --enable-private-nodes \
    --network "projects/${PROJECT_ID}/global/networks/${VPC_NETWORK}" \
    --subnetwork "projects/${PROJECT_ID}/regions/asia-east2/subnetworks/${SUBNET}" \
    --cluster-ipv4-cidr "/20" \
    --binauthz-evaluation-mode=DISABLED \
    --async

echo "Create Pub/Sub topic and subscription ..."
gcloud pubsub topics create ${TOPIC_NAME} --project ${PROJECT_ID}
gcloud pubsub subscriptions create ${TOPIC_NAME}-sub \
    --topic=${TOPIC_NAME} \
    --project ${PROJECT_ID} \
    --ack-deadline=300

waitTime=0
ready="ok"
until [[ $(gcloud container clusters describe ${GKE_CLUSTER_NAME} --region ${REGION} --project ${PROJECT_ID} --format json | jq -r .status) == "RUNNING" ]]; do
    sleep 10;
    waitTime=$(expr ${waitTime} + 10);
    echo "waited ${waitTime} seconds for GKE to be ready ...";
    if [ ${waitTime} -gt 600 ]; then
        ready="failed";
        echo "Wait too long, deploy failed.";
        exit 1;
    fi
done

echo "Environment Preparation Done."