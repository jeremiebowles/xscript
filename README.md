# Cute Animal Poetry Service

Returns a random cute image (dog/cat/fox/badger/raccoon/opossum) from Reddit plus a random poetry line.

It also keeps a history of previously used Reddit post IDs and skips repeats.

## Run locally

```bash
pip install -r requirements.txt
python3 cute_poetry_service.py
```

Then call:

```bash
curl "http://localhost:8080/?species=dog"
```

`species` is optional. Allowed values: `dog`, `cat`, `fox`, `badger`, `raccoon`, `opossum`.

## Cloud Run

Use Google Secret Manager so runtime config is never committed to code or plain env files.

### 1) Set project/region

```bash
gcloud config set project YOUR_PROJECT_ID
REGION=us-central1
```

### 2) Enable APIs

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

### 3) Create secrets

```bash
printf 'cute-poetry-bot/1.0 (+you@example.com)' | gcloud secrets create reddit-user-agent --data-file=-
printf 'https://poetrydb.org/random/20' | gcloud secrets create poetry-api-url --data-file=-
```

If a secret already exists, add a new version instead:

```bash
printf 'cute-poetry-bot/1.0 (+you@example.com)' | gcloud secrets versions add reddit-user-agent --data-file=-
printf 'https://poetrydb.org/random/20' | gcloud secrets versions add poetry-api-url --data-file=-
```

### 4) Create runtime service account

```bash
gcloud iam service-accounts create cute-poetry-sa --display-name="Cute Poetry Runtime SA"
SA_EMAIL="cute-poetry-sa@$(gcloud config get-value project).iam.gserviceaccount.com"
```

Grant only secret read:

```bash
gcloud secrets add-iam-policy-binding reddit-user-agent \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding poetry-api-url \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"
```

### 5) Deploy with secrets bound as env vars

```bash
gcloud run deploy cute-poetry \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --service-account "$SA_EMAIL" \
  --set-secrets USER_AGENT=reddit-user-agent:latest,POETRY_API_URL=poetry-api-url:latest
```

### 6) Test

```bash
SERVICE_URL=$(gcloud run services describe cute-poetry --region "$REGION" --format='value(status.url)')
curl "${SERVICE_URL}/?species=dog"
```

### 7) Run every 6 hours (Cloud Scheduler)

```bash
gcloud services enable cloudscheduler.googleapis.com

SERVICE_URL=$(gcloud run services describe cute-poetry --region "$REGION" --format='value(status.url)')
gcloud scheduler jobs create http cute-poetry-every-6h \
  --location="$REGION" \
  --schedule="0 */6 * * *" \
  --uri="${SERVICE_URL}/" \
  --http-method=GET
```

If the job already exists, update it:

```bash
gcloud scheduler jobs update http cute-poetry-every-6h \
  --location="$REGION" \
  --schedule="0 */6 * * *" \
  --uri="${SERVICE_URL}/" \
  --http-method=GET
```

## AWS Lambda

Use handler: `cute_poetry_service.lambda_handler`

For API Gateway, pass query param `species` if needed.

## Local cron (every 6 hours)

```bash
cd "/home/user/Documents/x script"
(crontab -l 2>/dev/null; echo '0 */6 * * * cd "/home/user/Documents/x script" && /usr/bin/python3 cute_poetry_service.py --once >> /tmp/cute_poetry.log 2>&1') | crontab -
```

## Notes

- Poetry lines are fetched from PoetryDB (`https://poetrydb.org/random/20`) with a small local fallback list.
- A basic profanity mask is applied to the Reddit title and poetry line.
- Reddit rate limits anonymous traffic; set `USER_AGENT` via Secret Manager.
- Duplicate prevention uses `HISTORY_FILE` (default: `/tmp/cute_poetry_history.json`) and stores up to `MAX_HISTORY_ITEMS` (default: `2000`).
- `/tmp` is ephemeral on serverless runtimes, so duplicate history may reset when instances restart.
