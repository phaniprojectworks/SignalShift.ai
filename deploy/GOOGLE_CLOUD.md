# SignalShift.ai on Google Cloud

How the app runs on Google Cloud today, what to improve, and the exact steps. **Every step is optional and independent.** With none of them applied, a new image behaves exactly like the current deployment.

## 1. Today

```
Browser ──► Cloud Run (one container: UI + API + SSE + scheduler + ad simulator + Telegram)
                │
                └── Cloud Storage bucket, mounted at /mnt/storage (Cloud Storage FUSE)
                       └── signalshift.db  (SQLite, WAL mode), symlinked to /app/signalshift.db
```

**Risks in this setup, most important first:**

1. **SQLite on a Cloud Storage FUSE mount.** Google's docs advise against databases on a FUSE mount:
   - it has no file locking
   - each sync re-uploads the whole object (here ~50 MB), so writes are slow and costly
   - a second instance writing the same object means *last writer wins*, so data is silently lost

   The app writes often (orders, the ad simulator every 30 s, alerts), so this is the main durability risk. **Fix: step 5 (Litestream).**
2. **More than one instance breaks the app.** Several things live in one process:
   - the live stream (SSE event bus)
   - the scheduler
   - the ad simulator
   - the SQLite writer

   A second instance means duplicate scheduled jobs, live updates that only reach half the browsers, and two database writers. **Fix: step 1 (`--max-instances=1`).**
3. **Background work pauses between requests.** With default "CPU only during requests" billing, Cloud Run throttles the scheduler, the ad simulator and Telegram polling whenever no request is in flight. **Fix: step 1 (`--no-cpu-throttling`, `--min-instances=1`).**
4. **Secrets in plain environment variables.** **Fix: step 2 (Secret Manager).**

## 2. What is in the repo for Google Cloud

| File | What it does | Changes behaviour? |
|---|---|---|
| `Dockerfile` | Same image as before, plus the Litestream binary. Starts `deploy/start.sh`. | No: the default start path runs the same commands as the old `CMD`. |
| `deploy/start.sh` | Entrypoint. Default: the database on the bucket mount (as before). With `LITESTREAM_REPLICA_URL` set: local disk + continuous replication to Cloud Storage. | Only if you set `LITESTREAM_REPLICA_URL`. |
| `cloudbuild.yaml` | CI/CD: tests → build → push to Artifact Registry → deploy (image only). | Only when you run it. |
| `.dockerignore` | Keeps `.env`, local databases, tests and `.git` out of the image. | No (they were not needed in the image). |
| `/health` | Health endpoint for probes and uptime checks (process up + database answers). | New route only. |
| Log format | JSON lines now also carry `severity`, `message`, `stack_trace` (Error Reporting) and the request trace. The old `ts`/`level`/`msg` fields are kept; an alert's own severity is logged as `alert_severity`. | Logs only. |
| Vertex AI | `GEMINI_USE_VERTEXAI=true` calls Gemini as the service account (no API key). | Only if you set it. |
| `PORT` | `run.py` serves on `$PORT`, which Cloud Run sets. | No (default 8080). |

## 3. Steps

Set these once in Cloud Shell (or any terminal with `gcloud`):

```bash
PROJECT=your-project-id
REGION=asia-south1              # your Cloud Run region
SERVICE=signalshift             # your Cloud Run service name
BUCKET=your-bucket-name         # the bucket mounted at /mnt/storage
gcloud config set project "$PROJECT"
SA=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(spec.template.spec.serviceAccountName)')
# empty means the default compute service account:
[ -z "$SA" ] && SA="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "$SA  $URL"
```

Each `gcloud run services update` creates a new revision and keeps everything else on the service. If a new revision fails to start, Cloud Run keeps serving the previous one. To undo any step, run the inverse flag, or route traffic back to the earlier revision: Cloud Run → service → **Revisions** → **Manage traffic**.

### Step 1 · Cloud Run service settings (recommended first, no code)

```bash
gcloud run services update "$SERVICE" --region "$REGION" \
  --max-instances=1 --min-instances=1 --no-cpu-throttling --cpu-boost \
  --timeout=3600 --concurrency=200 --memory=2Gi
```

| Flag | Why |
|---|---|
| `--max-instances=1` | One process owns the scheduler, the live stream and the SQLite writer (see risk 2). |
| `--min-instances=1` | The scheduler, ad simulator and Telegram polling need a running instance. It also removes cold starts (the first start can take a minute while demo data is generated). |
| `--no-cpu-throttling` | CPU stays allocated between requests, so background jobs actually run (see risk 3). |
| `--timeout=3600` | Live dashboards hold a Server-Sent Events connection; browsers reconnect after the timeout and replay what they missed (`Last-Event-ID`). |
| `--concurrency=200` | Each open dashboard or store tab holds one connection. |
| `--memory=2Gi` | Room for pandas and, with step 5, the database on local disk (Cloud Run's disk is RAM). Keep your current value if it is already higher. |

**Cost:** `--min-instances=1` + `--no-cpu-throttling` mean one instance is billed all the time. Check the [Cloud Run pricing calculator](https://cloud.google.com/products/calculator) for your region, and set a **budget alert** (Billing → Budgets & alerts).

**Note:** while a new revision rolls out, the old and new revisions can run side by side for a few seconds, even with `--max-instances=1`. Writes in that window can be lost, today and with step 5 alike. Avoid deploying in the middle of a live demo.

### Step 2 · Secret Manager for the keys

```bash
gcloud services enable secretmanager.googleapis.com
printf '%s' 'YOUR_GEMINI_KEY'    | gcloud secrets create gemini-api-key --data-file=-
printf '%s' 'YOUR_TELEGRAM_TOKEN' | gcloud secrets create telegram-bot-token --data-file=-
for s in gemini-api-key telegram-bot-token; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done
# if these are currently plain env vars, remove them in the same update (a name cannot be both)
gcloud run services update "$SERVICE" --region "$REGION" \
  --remove-env-vars=GEMINI_API_KEY,TELEGRAM_BOT_TOKEN \
  --update-secrets=GEMINI_API_KEY=gemini-api-key:latest,TELEGRAM_BOT_TOKEN=telegram-bot-token:latest
```

The app reads them as ordinary environment variables, so no code changes. Skip a secret you don't use. If gcloud complains that a variable doesn't exist, drop it from `--remove-env-vars`.

### Step 3 · Cloud Logging, Error Reporting and Cloud Trace links

Logs already reach Cloud Logging. With the new image they show the right severity, crashes show up in **Error Reporting** automatically, and lines from the same request are grouped. For the trace grouping, tell the app its project:

```bash
gcloud run services update "$SERVICE" --region "$REGION" --update-env-vars=GOOGLE_CLOUD_PROJECT="$PROJECT"
```

Useful Logs Explorer queries:

```
resource.type="cloud_run_revision" AND jsonPayload.msg="alert decision" AND jsonPayload.alert_severity="critical"
resource.type="cloud_run_revision" AND jsonPayload.logger="signalshift.ads" AND jsonPayload.wasted=true
resource.type="cloud_run_revision" AND severity>=WARNING
```

### Step 4 · Health checks

The new `/health` route returns `200 {"status":"ok"}` when the process is up and the database answers, and `503` otherwise. Add it as a **liveness probe**:
- Where: Cloud Run → service → **Edit & deploy new revision** → container → **Health checks**.
- Settings: HTTP GET `/health`, period 30 s, **timeout 10 s**, failure threshold 5.
- **Do not leave the timeout at its default of 1 s.** The app does heavy work in the background (forecast recalibration, demo reset), and a slow `/health` answer counts as a failure. After enough failures Cloud Run restarts the only instance, and while it restarts every request gets `Rate exceeded.` (HTTP 429). If in doubt, skip this probe: it is optional.

Keep the default TCP **startup** probe. The very first start generates the demo data before the server listens, which can take a minute.

### Step 5 · Litestream: SQLite on local disk, replicated to Cloud Storage (fixes risk 1)

The database moves to the instance's local disk (fast, with real locking). [Litestream](https://litestream.io) streams every committed change to your bucket within about a second, and restores it when an instance starts. App code is unchanged.

1. Build and deploy the new image first (step 7, or your usual deploy). Keep the bucket **volume mount** in place: the first start seeds from it.
2. Make sure the service account can write the bucket. It already can if the mount is read-write; otherwise:
   ```bash
   gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
   ```
3. Switch on:
   ```bash
   gcloud run services update "$SERVICE" --region "$REGION" \
     --update-env-vars=LITESTREAM_REPLICA_URL="gcs://$BUCKET/litestream/signalshift.db"
   ```
4. Check the logs. The first start prints `litestream: no replica yet; seeding from /mnt/storage/signalshift.db (one-time switch-over)`; later starts print `restoring ... from gcs://...`. Objects appear under `gs://$BUCKET/litestream/`.

**After the switch,** `gs://$BUCKET/signalshift.db` is a frozen old copy. The live data is in `litestream/`. Leave the mount in place, or remove it later; Litestream doesn't need it.

**Rolling back to the mount** (new data since the switch lives only in the replica):
```bash
# Cloud Shell: install litestream (https://litestream.io/install/), then
litestream restore -o signalshift.db "gcs://$BUCKET/litestream/signalshift.db"
gcloud storage cp signalshift.db "gs://$BUCKET/signalshift.db"
gcloud run services update "$SERVICE" --region "$REGION" --remove-env-vars=LITESTREAM_REPLICA_URL
```

### Step 6 · Gemini through Vertex AI (no API key)

```bash
gcloud services enable aiplatform.googleapis.com
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role=roles/aiplatform.user
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars=GEMINI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT="$PROJECT",GOOGLE_CLOUD_LOCATION=global
```

- **Model name:** keep `GEMINI_MODEL` (and the fallback) set, and check that the model is available on Vertex AI in that location.
- **Test locally first:** run `gcloud auth application-default login`, then `python scripts/check_gemini.py` with the same variables.
- **Retire the key:** once reports show `Gemini` in the inbox, remove the API key secret from step 2.
- **What stays the same:** guardrails, retries and the template fallback are unchanged. If Vertex fails, reports fall back to the template exactly as before.

### Step 7 · CI/CD with Cloud Build and Artifact Registry

```bash
gcloud services enable cloudbuild.googleapis.com artifactregistry.googleapis.com containerscanning.googleapis.com
gcloud artifacts repositories create signalshift --repository-format=docker --location="$REGION"
# the account Cloud Build runs as: see Cloud Build → Settings (often the default compute service account)
CB_SA="$SA"
for r in roles/run.admin roles/artifactregistry.writer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$CB_SA" --role="$r"
done
gcloud iam service-accounts add-iam-policy-binding "$SA" --member="serviceAccount:$CB_SA" --role=roles/iam.serviceAccountUser
# run once by hand
gcloud builds submit --config cloudbuild.yaml --substitutions=_REGION="$REGION",_SERVICE="$SERVICE"
```

Then automate it: Cloud Build → **Triggers** → **Connect repository** (GitHub `phaniprojectworks/SignalShift.ai`) → create a trigger on branch `^main$` using `cloudbuild.yaml`, with substitutions `_REGION` and `_SERVICE`.

- **Testing:** the pipeline runs the full test suite first (about 5 minutes); failing tests stop the deploy.
- **What a deploy changes:** it passes only `--image`, so your volume mount, env vars and secrets stay as they are.
- **Scanning:** Artifact Registry scans each image for known vulnerabilities (the `containerscanning` API).

### Step 8 · Telegram webhook mode (feature already in the app)

Polling needs an always-running instance. A webhook lets Telegram call the service instead:

```bash
printf '%s' "$(openssl rand -hex 24)" | gcloud secrets create telegram-webhook-secret --data-file=-
gcloud secrets add-iam-policy-binding telegram-webhook-secret --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars=TELEGRAM_MODE=webhook --update-secrets=TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:latest
curl -s "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d url="$URL/telegram/webhook" -d secret_token="$(gcloud secrets versions access latest --secret=telegram-webhook-secret)"
```

### Step 9 · Cloud Monitoring

```bash
# uptime check on /health (Monitoring → Uptime checks shows it; add an alert policy on failures there)
gcloud monitoring uptime create signalshift-health --resource-type=uptime-url \
  --resource-labels=host="${URL#https://}",project_id="$PROJECT" --path=/health --protocol=https
# count critical stock alerts as a metric you can chart and alert on
gcloud logging metrics create critical_stock_alerts --description="Critical stock alerts raised by the engine" \
  --log-filter='resource.type="cloud_run_revision" AND jsonPayload.msg="alert decision" AND jsonPayload.decision="alert" AND jsonPayload.alert_severity="critical"'
```

In Monitoring → **Alerting**, create policies on the uptime check and on `logging/user/critical_stock_alerts` (for example, notify by email when it's above 0 in 5 minutes).

## 4. Roadmap: bigger moves, with trade-offs

| Service | What it would give | What it takes |
|---|---|---|
| **Cloud SQL for PostgreSQL** | A managed database with backups, point-in-time recovery and several app instances. The production answer once the app must scale or survive deploy overlaps. | Port the SQLite-specific parts: date math (`julianday`, `strftime`, `date()`), Ask's full-text search (FTS5 → PostgreSQL `tsvector`), the `BEGIN IMMEDIATE` write lock (→ row locks / `SELECT … FOR UPDATE`), timestamp-as-text comparisons (`sql_ts`), and the demo reset (SQLite backup API). Run the tests against PostgreSQL. Several days of work. |
| **Pub/Sub** (or PostgreSQL `LISTEN/NOTIFY`) | Live events across several instances, so Cloud Run can scale out. | Needs Cloud SQL first (a shared database). Replace the in-process event bus with a publisher and a per-instance subscriber that feeds SSE. |
| **Cloud Scheduler** | Hourly sweeps, the 08:00 briefing and the 02:30 recalibration as HTTP calls, so the instance can scale to zero (lower cost). | Authenticated job endpoints. The ad simulator and Telegram polling would also need a new home (webhook mode covers Telegram). |
| **Identity Platform / Firebase Auth**, or **IAP** | Real sign-in instead of the demo login; IAP can lock the owner dashboard to your Google accounts with no code. | IAP needs a load balancer in front of Cloud Run; Identity Platform needs token checks in the API. |
| **Cloud Armor** (with a load balancer) | Rate limiting and bot protection for public endpoints (`/orders`, `/api/ads/click`). | A global external load balancer in front of Cloud Run. |
| **BigQuery + Looker Studio** | Long-term sales, ad and savings analytics and shareable reports. | A scheduled export (or Datastream once on Cloud SQL). |
| **Firebase Hosting / Cloud CDN** | CDN for the UI. | Low value here: the UI is small and already served by the API container. |
