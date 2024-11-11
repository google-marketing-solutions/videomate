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

gcloud services enable compute.googleapis.com \
    artifactregistry.googleapis.com \
    pubsub.googleapis.com \
    container.googleapis.com \
    aiplatform.googleapis.com \
    bigquery.googleapis.com \
    sheets.googleapis.com \
    storage.googleapis.com \
    --project ${PROJECT_ID}


# Create a network and NAT to ensure that the Private network can pull the image
gcloud compute networks create ${VPC_NETWORK} --project=${PROJECT_ID} --subnet-mode=custom

gcloud compute networks subnets create ${SUBNET} \
    --project=${PROJECT_ID} \
    --region=${REGION} \
    --network=${VPC_NETWORK} \
    --range=10.0.0.0/20 \
    --enable-private-ip-google-access

gcloud compute routers create vvtest-router \
    --project=${PROJECT_ID} \
    --region=${REGION} \
    --network=${VPC_NETWORK}

gcloud compute routers nats create vvtest-nat \
    --project=${PROJECT_ID} \
    --router=vvtest-router \
    --router-region=${REGION} \
    --nat-all-subnet-ip-ranges \
    --auto-allocate-nat-external-ips