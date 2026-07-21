#!/bin/bash
# deploy.sh — full deployment of the data collection system
# Usage: bash deploy.sh [THING_NAME] [THING_GROUP] [REGION]
set -euo pipefail

THING_NAME="${1:-lerobot-device}"
THING_GROUP="${2:-lerobot-group}"
REGION="${3:-ap-northeast-2}"
STACK_NAME="lerobot-data-collection"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "============================================"
echo " LeRobot Data Collection — Deploy"
echo "============================================"
echo " Account:    $ACCOUNT_ID"
echo " Region:     $REGION"
echo " Thing:      $THING_NAME"
echo " Group:      $THING_GROUP"
echo "============================================"
echo ""

# --- Step 1: CloudFormation ---
echo ">>> [1/6] Deploying CloudFormation..."
aws cloudformation deploy \
  --template-file infra/cloudformation.yaml \
  --stack-name "$STACK_NAME" \
  --parameter-overrides ThingName="$THING_NAME" \
  --capabilities CAPABILITY_IAM \
  --region "$REGION" \
  --no-fail-on-empty-changeset

# Extract output values
CF_URL=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`CloudFrontURL`].OutputValue' --output text)
UI_BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`WebUIBucket`].OutputValue' --output text)
DATA_BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`DataBucket`].OutputValue' --output text)

echo "   CloudFront: $CF_URL"
echo "   UI Bucket:  $UI_BUCKET"
echo "   Data Bucket: $DATA_BUCKET"

# --- Step 2: Upload web UI ---
echo ">>> [2/6] Uploading web UI..."
aws s3 cp web-ui/multiviewer.html "s3://${UI_BUCKET}/multiviewer.html" \
  --content-type text/html --region "$REGION"
# P2P low-latency live screen (default landing) + multi-viewer/storage screen.
aws s3 cp web-ui/live-p2p.html "s3://${UI_BUCKET}/live-p2p.html" \
  --content-type text/html --region "$REGION"

# --- Step 3: TES Role S3 permissions ---
echo ">>> [3/6] Adding TES Role S3 permissions..."
aws iam put-role-policy \
  --role-name GreengrassV2TokenExchangeRole \
  --policy-name LeRobotDataCollectionS3 \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [\"s3:PutObject\", \"s3:GetObject\", \"s3:ListBucket\"],
      \"Resource\": [\"arn:aws:s3:::${DATA_BUCKET}\", \"arn:aws:s3:::${DATA_BUCKET}/*\"]
    }]
  }" 2>/dev/null && echo "   ✅ Permission added" || echo "   ⏭️  Already exists"

# --- Step 4: Register components (default set: v21.gpu + kvs-webrtc-p2p) ---
echo ">>> [4/6] Registering components..."
aws greengrassv2 create-component-version \
  --inline-recipe "fileb://components/com.lerobot.data-collection.v21.gpu/recipe.yaml" \
  --region "$REGION" 2>/dev/null && echo "   ✅ v21.gpu registered" || echo "   ⏭️  v21.gpu already exists"
aws greengrassv2 create-component-version \
  --inline-recipe "fileb://components/com.groot.kvs-webrtc-p2p/recipe.yaml" \
  --region "$REGION" 2>/dev/null && echo "   ✅ kvs-webrtc-p2p registered" || echo "   ⏭️  kvs-webrtc-p2p already exists"
# v21.gpu fetches collect.py from S3 at runtime — upload it to the version path
# (the recipe expects s3://greengrass-datasets-<ACCOUNT>/collect/.../1.0.0/collect.py).
aws s3 cp components/com.lerobot.data-collection.v21.gpu/artifacts/collect.py \
  "s3://${DATA_BUCKET}/collect/com.lerobot.data-collection.v21.gpu/1.0.0/collect.py" \
  --region "$REGION" && echo "   ✅ collect.py uploaded"

# --- Step 5: Greengrass deployment (default set: v21.gpu + kvs-webrtc-p2p) ---
echo ">>> [5/6] Deploying to Greengrass..."
TARGET_ARN="arn:aws:iot:${REGION}:${ACCOUNT_ID}:thinggroup/${THING_GROUP}"

# NOTE: com.groot.kvs-webrtc-p2p is the (optional) low-latency monitor. It
# requires KVS prerequisites to exist first (see components/com.groot.kvs-webrtc-p2p/README.md):
#   - a SINGLE_MASTER P2P signaling channel (default name: thor-001-p2p), NO MediaStorageConfiguration
#   - a KVS video stream for recording/HLS replay (default name: thor-001-webrtc)
#   - an IAM role KvsViewerRole (viewer-only) for browser viewer credentials
# If you don't need live monitoring, remove the kvs-webrtc-p2p block below.

aws greengrassv2 create-deployment \
  --deployment-name "lerobot-data-collection-$(date +%Y%m%d%H%M)" \
  --target-arn "$TARGET_ARN" \
  --components "{
    \"com.lerobot.data-collection.v21.gpu\": {
      \"componentVersion\": \"1.0.0\",
      \"configurationUpdate\": {
        \"merge\": \"{\\\"thingName\\\":\\\"${THING_NAME}\\\",\\\"s3Bucket\\\":\\\"${DATA_BUCKET}\\\"}\"
      }
    },
    \"com.groot.kvs-webrtc-p2p\": {
      \"componentVersion\": \"1.0.0\",
      \"configurationUpdate\": {
        \"merge\": \"{\\\"thingName\\\":\\\"${THING_NAME}\\\",\\\"channelName\\\":\\\"thor-001-p2p\\\",\\\"videoDevice\\\":\\\"/dev/video4\\\",\\\"viewerRoleArn\\\":\\\"arn:aws:iam::${ACCOUNT_ID}:role/KvsViewerRole\\\"}\"
      }
    }
  }" \
  --deployment-policies '{"componentUpdatePolicy":{"action":"SKIP_NOTIFY_COMPONENTS"},"failureHandlingPolicy":"ROLLBACK"}' \
  --region "$REGION"

# --- Step 6: Done ---
echo ""
echo ">>> [6/6] Checking IoT endpoint..."
IOT_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --region "$REGION" --query endpointAddress --output text)

echo ""
echo "============================================"
echo " ✅ Deployment complete!"
echo "============================================"
echo ""
echo " Web UI:       $CF_URL
 P2P screen:   $CF_URL/live-p2p.html  (low-latency live monitor)"
echo " IoT Endpoint: $IOT_ENDPOINT"
echo " Thing Name:   $THING_NAME"
echo " Data Bucket:  $DATA_BUCKET"
echo " Login:        use the <WEB_USERNAME>/<WEB_PASSWORD> you configured (see README; do NOT use demo defaults)"
echo ""
echo " Check deployment status:"
echo "   aws greengrassv2 list-installed-components \\"
echo "     --core-device-thing-name $THING_NAME --region $REGION"
echo ""
