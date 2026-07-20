# Deployment Guide (commands & configuration reference)

A step-by-step guide to deploying the LeRobot data collection system to an AWS IoT Greengrass v2
device (e.g., Jetson AGX Thor). Environment-specific values in the commands are placeholders — see
the substitution table in `README.md`.

> Notation: `<REGION>` (e.g. `ap-northeast-2`), `<AWS_ACCOUNT_ID>`, `<THING>` (e.g. `thor-001`),
> `<THING_GROUP>` (deployment target), `<DATA_BUCKET>` (dataset upload bucket), `<PROFILE>` (AWS CLI profile).
> The source bucket uses the `greengrass-datasets-<AWS_ACCOUNT_ID>` convention as an example (adjust to any name you like).

---

## 0. Prerequisites
- The device has **Greengrass v2 Nucleus installed + provisioned** (HEALTHY), with Docker + NVIDIA Container Runtime.
- The device belongs to thing group `<THING_GROUP>` (deployments target the thing group).
- AWS CLI + credentials locally (`--profile <PROFILE>`), with permissions in the target region.
- Physical setup (real robot): SO-101 leader/follower (`/dev/ttyACM0/1`), front/wrist cameras (`/dev/cam_*`).

## 0.1 ⚠️ Region/bucket rule (important)
- Keep the **data upload bucket `<DATA_BUCKET>` in the same region as the deployment**.
  A bucket in another region (e.g. us-east-1) causes downloads/playback to fail with
  `AuthorizationQueryParametersError` due to presigned-URL signing-region mismatch.
- Check: `aws s3api get-bucket-location --bucket <DATA_BUCKET>` → `LocationConstraint` must
  match the deployment region (`null` = us-east-1).

---

## 0.2 ⚠️ Required security hardening before deployment (guidance & examples)

> This repository is an **educational/demo sample**. The four items below ship with loose defaults
> for demo convenience, so you **must harden them as shown before deploying to production (or a shared
> IoT account/environment)**. Deploying as-is can expose your environment to risk.
> (For rationale see **Known Limitations** in the README; for detailed findings see the PCSR report.)

### (1) Scope the IoT Custom Authorizer policy to least privilege
The policy returned by `allow()` in `AuthLambda` (`infra/cloudformation.yaml`) allows **account-wide
topics** with `Resource: '*'` in the demo. Before deploying, restrict it to **client/topic ARN
granularity** as shown. Inject account/region/thing from Lambda environment variables or token claims.

```python
# Inside allow() — replace the demo's {'Effect':'Allow', ..., 'Resource':'*'} with the following
import os
region = os.environ['AWS_REGION']; acct = os.environ['ACCOUNT_ID']; thing = 'thor-001'
base = f'arn:aws:iot:{region}:{acct}'
policy = {'Version':'2012-10-17','Statement':[
  {'Effect':'Allow','Action':'iot:Connect',   'Resource':f'{base}:client/web-lerobot-*'},
  {'Effect':'Allow','Action':['iot:Publish','iot:Receive'],
   'Resource':[f'{base}:topic/lerobot/{thing}/collect/*',
               f'{base}:topic/lerobot/{thing}/webrtc/viewer',
               f'{base}:topic/$aws/things/{thing}/shadow/name/episodes/*']},
  {'Effect':'Allow','Action':'iot:Subscribe',
   'Resource':[f'{base}:topicfilter/lerobot/{thing}/collect/*',
               f'{base}:topicfilter/lerobot/{thing}/webrtc/viewer',
               f'{base}:topicfilter/$aws/things/{thing}/shadow/name/episodes/*']},
]}
```
> After deployment, verify: as a web user, subscribing to out-of-scope topics such as `lerobot/#` is **denied**.

### (2) Manage web credentials safely instead of demo defaults
- The demo auth is a **shared account + `base64(user:pass)` token**. Weak values like `admin/admin`
  are **not allowed**. Set `<WEB_USERNAME>`/`<WEB_PASSWORD>` to **strong values** and keep
  `web-ui/index.html` and the Authorizer identical.
- Do not keep credentials as code constants; inject them via **Secrets Manager / a CloudFormation
  `NoEcho` parameter**, and compare in the Lambda using **`hmac.compare_digest`**.

```yaml
# cloudformation.yaml — pass credentials as parameters that are not logged (example)
Parameters:
  WebUsername: { Type: String }
  WebPassword: { Type: String, NoEcho: true }
```
```python
# Lambda comparison (timing-safe)
import hmac
ok = hmac.compare_digest(user, os.environ['WEB_USER']) and \
     hmac.compare_digest(pwd,  os.environ['WEB_PASS'])
```
> Production recommendation: replace with **Amazon Cognito Hosted UI (Authorization Code + PKCE)**.

### (3) Secure the data S3 bucket (block public access, encryption, TLS, versioning)
Apply the following to the `DataBucket` created by CloudFormation, or to a bucket you create in Section 3.

```bash
# Block public access
aws s3api put-public-access-block --bucket <DATA_BUCKET> --profile <PROFILE> \
  --public-access-block-configuration \
  BlockPublicAcls=true,BlockPublicPolicy=true,IgnorePublicAcls=true,RestrictPublicBuckets=true
# Default encryption (SSE-KMS recommended) + versioning
aws s3api put-bucket-encryption --bucket <DATA_BUCKET> --profile <PROFILE> \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"}}]}'
aws s3api put-bucket-versioning --bucket <DATA_BUCKET> --profile <PROFILE> \
  --versioning-configuration Status=Enabled
# Deny plaintext (HTTP) access (enforce TLS)
aws s3api put-bucket-policy --bucket <DATA_BUCKET> --profile <PROFILE> --policy '{
  "Version":"2012-10-17","Statement":[{"Sid":"DenyInsecureTransport","Effect":"Deny",
  "Principal":"*","Action":"s3:*",
  "Resource":["arn:aws:s3:::<DATA_BUCKET>","arn:aws:s3:::<DATA_BUCKET>/*"],
  "Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}'
```

### (4) Least-privilege KVS permissions instead of `kinesisvideo:*` (hardened version of the Section 4 table)
Restrict the device TES role and `KvsViewerRole` to the **required actions + the specific channel/stream ARNs**.

```json
// Device TES (ingest) — limited to channel/stream ARNs
{"Effect":"Allow",
 "Action":["kinesisvideo:DescribeSignalingChannel","kinesisvideo:GetSignalingChannelEndpoint",
           "kinesisvideo:ConnectAsMaster","kinesisvideo:JoinStorageSession",
           "kinesisvideo:GetDataEndpoint","kinesisvideo:DescribeStream",
           "kinesisvideo:GetIceServerConfig","kinesisvideo:CreateSignalingChannel"],
 "Resource":["arn:aws:kinesisvideo:<REGION>:<AWS_ACCOUNT_ID>:channel/<CHANNEL>/*",
             "arn:aws:kinesisvideo:<REGION>:<AWS_ACCOUNT_ID>:stream/<CHANNEL>/*"]}
```
```json
// KvsViewerRole — viewer-only actions
{"Effect":"Allow",
 "Action":["kinesisvideo:GetSignalingChannelEndpoint","kinesisvideo:ConnectAsViewer",
           "kinesisvideo:DescribeSignalingChannel","kinesisvideo:GetIceServerConfig",
           "kinesisvideo:GetDataEndpoint","kinesisvideo:GetHLSStreamingSessionURL"],
 "Resource":"arn:aws:kinesisvideo:<REGION>:<AWS_ACCOUNT_ID>:channel/<CHANNEL>/*"}
```
> Issue viewer STS sessions with the minimum required duration, and use the policy in (1) to limit
> subscription of the credentials topic to the web user and that topic only.

---

## 1. Replace placeholders
Replace `<AWS_ACCOUNT_ID>`, `<IOT_ENDPOINT>`, `<WEB_USERNAME>`, `<WEB_PASSWORD>`, and
`<DATA_BUCKET>` with real values, per the table in `README.md`.
```bash
# Look up the IoT data endpoint
aws iot describe-endpoint --endpoint-type iot:Data-ATS \
  --query endpointAddress --output text --region <REGION> --profile <PROFILE>
```

---

## 2. Cloud infrastructure (CloudFormation)
Deploys the Custom Authorizer (web login) + S3 (web UI/data) + CloudFront.
```bash
aws cloudformation deploy \
  --template-file infra/cloudformation.yaml \
  --stack-name lerobot-collection-web \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <REGION> --profile <PROFILE>
```
- From the Outputs, note the web UI bucket / data bucket / CloudFront domain / IoT Authorizer name.
- The web credentials (`<WEB_USERNAME>:<WEB_PASSWORD>`) must be **identical in `web-ui/index.html` and the Authorizer Lambda** for login to work.
- ⚠️ Apply the **required security hardening in §0.2 before deploying**: narrow the Authorizer policy (1), manage credentials safely (2), secure the data bucket (3).

---

## 3. Data bucket (same region as the deployment)
If CloudFormation did not create it, or you use a separate bucket, create it with an explicit region:
```bash
aws s3api create-bucket --bucket <DATA_BUCKET> --region <REGION> \
  --create-bucket-configuration LocationConstraint=<REGION> --profile <PROFILE>
```

---

## 4. TES role permissions (what the device uses to write to the cloud)
The device accesses AWS via the Greengrass **Token Exchange Service (TES) role**
(`GreengrassV2TokenExchangeRole`, etc.). The following permissions are required.

| Purpose | Required permissions | Resource |
|---|---|---|
| Dataset upload / presign | `s3:PutObject` `s3:GetObject` `s3:DeleteObject` `s3:ListBucket` `s3:GetBucketLocation` | `<DATA_BUCKET>`(+`/*`) |
| collect.py fetch | `s3:GetObject` | `greengrass-datasets-<AWS_ACCOUNT_ID>/*` |
| Episode window shadow | `iot:UpdateThingShadow` `iot:GetThingShadow` | `arn:aws:iot:<REGION>:<AWS_ACCOUNT_ID>:thing/<THING>` |
| (WebRTC ingest) KVS | `kinesisvideo:*` (demo) → **least-privilege channel/stream ARNs recommended per §0.2(4)** | channel/stream |
| (WebRTC viewer credentials) | `sts:AssumeRole` | `arn:aws:iam::<AWS_ACCOUNT_ID>:role/KvsViewerRole` |

Example — add the shadow permission inline:
```bash
aws iam put-role-policy --role-name GreengrassV2TokenExchangeRole \
  --policy-name EpisodeShadowUpdate --profile <PROFILE> \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":["iot:UpdateThingShadow","iot:GetThingShadow"],
    "Resource":"arn:aws:iot:<REGION>:<AWS_ACCOUNT_ID>:thing/<THING>"}]}'
```

---

## 5. Upload collect.py (version match required)
`collect.py` is not packaged; the recipe `run` step fetches it at runtime from a **version folder**.
Always keep the **recipe fetch-path version == uploaded version** identical.
```bash
VER=1.0.0   # must match the fetch path in the recipe run script
aws s3 cp components/com.lerobot.data-collection/artifacts/collect.py \
  s3://greengrass-datasets-<AWS_ACCOUNT_ID>/collect/com.lerobot.data-collection/$VER/collect.py \
  --region <REGION> --profile <PROFILE>
```

---

## 6. (Optional) WebRTC live/episode-playback infrastructure — `com.groot.kvs-webrtc-ingest`
Color camera → KVS WebRTC ingestion (live + per-episode HLS playback source).
```bash
CH=<THING>-webrtc
# Signaling channel
aws kinesisvideo create-signaling-channel --channel-name "$CH" \
  --region <REGION> --profile <PROFILE>
# Video stream (same name)
aws kinesisvideo create-stream --stream-name "$CH" --data-retention-in-hours 24 \
  --media-type video/h264 --region <REGION> --profile <PROFILE>
# Channel → stream MediaStorageConfiguration (ENABLED = ingestion)
CH_ARN=$(aws kinesisvideo describe-signaling-channel --channel-name "$CH" \
  --query ChannelInfo.ChannelARN --output text --region <REGION> --profile <PROFILE>)
STREAM_ARN=$(aws kinesisvideo describe-stream --stream-name "$CH" \
  --query StreamInfo.StreamARN --output text --region <REGION> --profile <PROFILE>)
aws kinesisvideo update-media-storage-configuration --channel-arn "$CH_ARN" \
  --media-storage-configuration Status=ENABLED,StreamARN="$STREAM_ARN" \
  --region <REGION> --profile <PROFILE>
```
- Create **KvsViewerRole** (viewer-only, read permissions only) and set it in the recipe config `viewerRoleArn`.
  Required actions: `kinesisvideo:GetSignalingChannelEndpoint/GetIceServerConfig/DescribeSignalingChannel/
  DescribeMediaStorageConfiguration/JoinStorageSessionAsViewer/ConnectAsViewer/GetDataEndpoint/
  GetHLSStreamingSessionURL`. Allow the TES role to `sts:AssumeRole` it in the trust policy.

---

## 7. Register components (create-component-version)
Register each recipe. **Register a new version every time you bump the fetch path / ComponentVersion.**
```bash
# Data collection (NVENC variant)
aws greengrassv2 create-component-version \
  --inline-recipe fileb://components/com.lerobot.data-collection.gpu/recipe.yaml \
  --region <REGION> --profile <PROFILE>
# (Optional) color WebRTC ingestion
aws greengrassv2 create-component-version \
  --inline-recipe fileb://components/com.groot.kvs-webrtc-ingest/recipe.yaml \
  --region <REGION> --profile <PROFILE>
# Wait for DEPLOYABLE
aws greengrassv2 describe-component \
  --arn arn:aws:greengrass:<REGION>:<AWS_ACCOUNT_ID>:components:com.lerobot.data-collection.gpu:versions:1.0.0 \
  --query status.componentState --output text --region <REGION> --profile <PROFILE>
```
> `com.lerobot.data-collection` (original) and `.gpu` use the same MQTT topics, so **pick one** (do not run both at once).

---

## 8. Deploy (create-deployment) — preserve existing components
**Always fetch the current deployment and preserve all components + each config**, replacing only the
target component. If you omit any component, it is removed from the device.

```bash
TG_ARN=arn:aws:iot:<REGION>:<AWS_ACCOUNT_ID>:thinggroup/<THING_GROUP>
DID=$(aws greengrassv2 list-deployments --target-arn "$TG_ARN" \
  --history-filter LATEST_ONLY --query 'deployments[0].deploymentId' \
  --output text --region <REGION> --profile <PROFILE>)
aws greengrassv2 get-deployment --deployment-id "$DID" \
  --region <REGION> --profile <PROFILE> > cur.json

python3 - <<'PY'
import json
cur=json.load(open('cur.json'))
comps=cur['components']
# Change the version/config of the target component only (preserve the rest)
comps['com.lerobot.data-collection.gpu']['componentVersion']='1.0.0'
comps['com.lerobot.data-collection.gpu'].setdefault('configurationUpdate',{})['merge']=json.dumps({
    "thingName":"<THING>",
    "s3Bucket":"<DATA_BUCKET>",
    "region":"<REGION>",
    "episodeLength":"60",
    "numEpisodes":"50",
    "dataImage":"lerobot-data-collection-gpu:1.0.0"
})
json.dump({
  "targetArn":cur["targetArn"],
  "deploymentName":"lerobot-data-collection-deploy",
  "components":comps,
  "deploymentPolicies":cur.get("deploymentPolicies",{
     "failureHandlingPolicy":"ROLLBACK",
     "componentUpdatePolicy":{"timeoutInSeconds":60,"action":"NOTIFY_COMPONENTS"},
     "configurationValidationPolicy":{"timeoutInSeconds":60}})
}, open('deploy.json','w'))
print("components:", sorted(comps))
PY

aws greengrassv2 create-deployment --cli-input-json file://deploy.json \
  --region <REGION> --profile <PROFILE>
rm -f cur.json deploy.json
```
> ⚠️ Another operator may change the deployment concurrently, so **always re-fetch right before deploying**. `failureHandlingPolicy=ROLLBACK` is recommended.

---

## 9. Configuration reference

### 9.1 `com.lerobot.data-collection[.gpu]` (data collection)
| Key | Default | Description |
|---|---|---|
| `thingName` | `lerobot-device` | IoT thing name = MQTT topic prefix (`lerobot/<thing>/collect/*`). **Must be set to the device thing** |
| `s3Bucket` | (empty) | Dataset upload bucket. **Must be in the same region as the deployment** |
| `s3Prefix` | `datasets/` | S3 key prefix |
| `region` | `ap-northeast-2` | AWS region (upload/presign/shadow/KVS) |
| `langInstruction` | `pick orange` | Default task instruction (overridden by the web UI) |
| `datasetName` | `pick_orange_demo` | Dataset folder name |
| `numEpisodes` | `50` | Target episode count (overridden by the web UI start) |
| `episodeLength` | `300` | Max **seconds** before auto-advancing an episode (e.g. 60). lerobot `episode_time_s` |
| `leaderPort`/`followerPort` | `/dev/ttyACM0`·`1` | SO-101 leader/follower serial |
| `frontCameraIndex`/`wristCameraIndex` | `/dev/cam_front`·`cam_wrist` | Cameras (symlinks recommended) |
| `cameraWidth`/`cameraHeight`/`cameraFps` | `640`/`480`/`30` | Camera resolution/FPS |
| `datasetDir` | `/home/arobot/.../outputs` | Local output path mounted into the container |
| `dataImage` | `lerobot-data-collection[-gpu]:<tag>` | Image tag the device builds. **Change the tag to force an install rebuild (~2h)**; keep it when changing only collect.py |
| `gpuEncode` / `videoCodec` | `1` / `h264_nvenc` | (.gpu only) NVENC encoding switch. In practice lerobot may encode via PyAV so this can be a no-op (harmless, CPU AV1) |
| `kvsStreamName` | `thor-001-camera` | (legacy) IR monitoring stream name |
| `lerobotCommit`/`torch*`/`jetsonTorchIndex` | (fixed) | **Image build parameters** — usually keep defaults (advanced) |

> What `collect.py` actually uses are the environment variables exported by the recipe `run` step.
> The deployment config `merge` overrides the recipe DefaultConfiguration (stored config takes
> precedence, so change values **via config merge**).

### 9.2 `com.groot.kvs-webrtc-ingest` (color WebRTC ingestion)
| Key | Default | Description |
|---|---|---|
| `channelName` | `thor-001-webrtc` | KVS signaling channel = stream name |
| `region` | `ap-northeast-2` | Region |
| `videoDevice` | (empty) | Color camera v4l2 node. **If empty, auto-detects a YUYV (color) node**; set `/dev/videoN` to pin a specific node |
| `videoWidth`/`videoHeight`/`videoFps` | `640`/`480`/`30` | Encoding resolution/FPS |
| `credentialRefreshSec` | `2700` | Viewer STS credential re-issue interval (seconds) |
| `thingName` | `thor-001` | MQTT prefix (`lerobot/<thing>/webrtc/viewer`) |
| `viewerRoleArn` | `arn:aws:iam::<AWS_ACCOUNT_ID>:role/KvsViewerRole` | Viewer-only STS role ARN |

---

## 10. Web UI deployment / update
```bash
aws s3 cp web-ui/index.html s3://<WEB_UI_BUCKET>/index.html \
  --content-type text/html --region <REGION> --profile <PROFILE>
# CloudFront invalidation
aws cloudfront create-invalidation --distribution-id <DIST_ID> \
  --paths "/index.html" "/" --profile <PROFILE>
```
- Pre-deploy checks: `node --check` on the inline JS, all-English (no Korean), balanced `<div>`/`</div>`.
- Replace the IoT endpoint / thing / data-bucket (#bk) defaults in `web-ui/index.html` for your environment.

---

## 11. Verification
```bash
# Installed components/versions
aws greengrassv2 list-installed-components --core-device-thing-name <THING> \
  --region <REGION> --profile <PROFILE>
# Deployment job status
aws iot describe-job-execution --job-id <JOB_ID> --thing-name <THING> \
  --region <REGION> --profile <PROFILE> --query execution.status
# Component logs (CloudWatch) — [OK] Controller running / [CMD] [REC] [CTRL] [STATUS] [SHADOW]
aws logs tail "/aws/greengrass/UserComponent/<REGION>/com.lerobot.data-collection.gpu" \
  --since 10m --region <REGION> --profile <PROFILE>
# Episode window shadow (populated after a session ends)
aws iot-data get-thing-shadow --thing-name <THING> --shadow-name episodes \
  --region <REGION> --profile <PROFILE> /dev/stdout
# Start recording (example)
aws iot-data publish --topic "lerobot/<THING>/collect/command" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"action":"start","lang":"pick orange","numEpisodes":"3"}' \
  --region <REGION> --profile <PROFILE>
```

---

## 12. Operational workflows
- **Change collect.py only** → upload to a new version folder (§5) + bump the recipe fetch-path version + re-register/redeploy the component.
  No image rebuild (fast).
- **Change the image (stack)** → **change the `dataImage` tag** (config merge) → install rebuilds (~2h).
- **Rollback** → revert config/version to a previous collect.py version folder + previous `dataImage` tag and redeploy.

## 13. Troubleshooting summary
- Download/playback `AuthorizationQueryParametersError` → data bucket region ≠ deployment region. Use a **same-region bucket**.
- Download `SignatureDoesNotMatch` → presign uses frozen credentials + SigV4 (already in the code). Re-list files to get fresh URLs.
- exit 133 `add_episode ... before add_frame` → an empty (0-frame) episode. The latest collect.py recovers and uploads completed episodes.
- Empty shadow → the session must end normally (endSession / all episodes complete) to be recorded. Check the TES `iot:UpdateThingShadow` permission.
- WebRTC live not showing (firewall) → choose **HLS Live** (TCP/443) in the web UI Monitor toggle.
