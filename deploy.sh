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
aws s3 cp web-ui/index.html "s3://${UI_BUCKET}/index.html" \
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

# --- Step 4: Register component ---
echo ">>> [4/6] Registering component..."
aws greengrassv2 create-component-version \
  --inline-recipe "fileb://components/com.lerobot.data-collection/recipe.yaml" \
  --region "$REGION" 2>/dev/null && echo "   ✅ Registered" || echo "   ⏭️  Already exists"

# --- Step 5: Greengrass deployment ---
echo ">>> [5/6] Deploying to Greengrass..."
TARGET_ARN="arn:aws:iot:${REGION}:${ACCOUNT_ID}:thinggroup/${THING_GROUP}"

aws greengrassv2 create-deployment \
  --deployment-name "lerobot-data-collection-$(date +%Y%m%d%H%M)" \
  --target-arn "$TARGET_ARN" \
  --components "{
    \"com.lerobot.data-collection\": {
      \"componentVersion\": \"1.2.19\",
      \"configurationUpdate\": {
        \"merge\": \"{\\\"thingName\\\":\\\"${THING_NAME}\\\",\\\"s3Bucket\\\":\\\"${DATA_BUCKET}\\\"}\"
      }
    }
  }" \
  --deployment-policies '{"componentUpdatePolicy":{"action":"SKIP_NOTIFY_COMPONENTS"},"failureHandlingPolicy":"DO_NOTHING"}' \
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
echo " Web UI:       $CF_URL"
echo " IoT Endpoint: $IOT_ENDPOINT"
echo " Thing Name:   $THING_NAME"
echo " Data Bucket:  $DATA_BUCKET"
echo " Login:        use the <WEB_USERNAME>/<WEB_PASSWORD> you configured (see README; do NOT use demo defaults)"
echo ""
echo " Check deployment status:"
echo "   aws greengrassv2 list-installed-components \\"
echo "     --core-device-thing-name $THING_NAME --region $REGION"
echo ""
