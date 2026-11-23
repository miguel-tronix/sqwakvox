#!/usr/bin/env bash
# Helper script to build, test locally with Podman, and deploy/toggle Sqwakvox Web on GCP Cloud Run.
#
# Usage:
#   ./deploy/cloudrun.sh build        # Build container image locally with Podman (uses /home2/app2/podman)
#   ./deploy/cloudrun.sh local        # Run container locally on http://127.0.0.1:8760
#   ./deploy/cloudrun.sh push         # Push local Podman image to GCP Artifact Registry
#   ./deploy/cloudrun.sh cloud-build  # Build & push image remotely via Google Cloud Build (saves local disk/bandwidth)
#   ./deploy/cloudrun.sh up           # Deploy / enable service on Cloud Run (demo ON); ensures telemetry bucket
#                                      # and sets SQWAKVOX_TELEMETRY_BUCKET so JSONL spans upload to GCS
#   ./deploy/cloudrun.sh proxy        # Open authenticated tunnel at http://127.0.0.1:8760
#   ./deploy/cloudrun.sh off          # Throttle CPU & scale to 0 between demos ($0 idle)
#   ./deploy/cloudrun.sh down         # Delete Cloud Run service completely (keeps image)
#   ./deploy/cloudrun.sh status       # Show current Cloud Run service status

set -euo pipefail

export PATH="/home2/app2/dev-tools/google-cloud-sdk/bin:/home2/app2/.local/bin:${PATH}"

# Keep Podman storage and temp directories on /home2 (since / only has ~1.5 GB free)
PODMAN_BASE="/home2/app2/podman"
mkdir -p "${PODMAN_BASE}/tmp" "${PODMAN_BASE}/storage" "${PODMAN_BASE}/run"
export TMPDIR="${PODMAN_BASE}/tmp"
PODMAN_ARGS=("--root" "${PODMAN_BASE}/storage" "--runroot" "${PODMAN_BASE}/run")

SERVICE_NAME="${SQWAKVOX_SERVICE_NAME:-sqwakvox-web}"
REGION="${GCP_REGION:-australia-southeast1}"
AR_REPO="${GCP_AR_REPO:-sqwakvox}"
IMAGE_NAME="sqwakvox-web"
LOCAL_TAG="${IMAGE_NAME}:latest"
LOCAL_PORT="${LOCAL_PORT:-8760}"
TELEMETRY_BUCKET="${SQWAKVOX_TELEMETRY_BUCKET:-nutriteasy-sqwakvox-telemetry}"
TELEMETRY_PREFIX="${SQWAKVOX_TELEMETRY_PREFIX:-telemetry/spans}"
TELEMETRY_RETENTION_DAYS="${SQWAKVOX_TELEMETRY_RETENTION_DAYS:-90}"
DATA_BUCKET="${SQWAKVOX_DATA_BUCKET:-nutriteasy-sqwakvox-data}"
DATA_MOUNT_PATH="${SQWAKVOX_DATA_MOUNT_PATH:-/data}"
UPLOADS_RETENTION_DAYS="${SQWAKVOX_UPLOADS_RETENTION_DAYS:-30}"
SERVICE_ACCOUNT_NAME="${SQWAKVOX_SERVICE_ACCOUNT_NAME:-sqwakvox-web}"

get_project_id() {
    local proj="${GCP_PROJECT_ID:-}"
    if [[ -z "${proj}" ]]; then
        proj="$(gcloud config get-value project 2>/dev/null || true)"
    fi
    if [[ -z "${proj}" || "${proj}" == "(unset)" ]]; then
        echo "Error: GCP project is not set. Run 'gcloud config set project <PROJECT_ID>' or export GCP_PROJECT_ID." >&2
        exit 1
    fi
    echo "${proj}"
}

remote_image_uri() {
    local proj
    proj="$(get_project_id)"
    echo "${REGION}-docker.pkg.dev/${proj}/${AR_REPO}/${IMAGE_NAME}:latest"
}

ensure_ar_repo() {
    local proj="$1"
    echo "==> Ensuring required GCP APIs are enabled in project '${proj}'..."
    gcloud services enable \
        artifactregistry.googleapis.com \
        cloudbuild.googleapis.com \
        run.googleapis.com \
        --project="${proj}"

    if ! gcloud artifacts repositories describe "${AR_REPO}" \
        --location="${REGION}" --project="${proj}" >/dev/null 2>&1; then
        echo "==> Creating Artifact Registry repository '${AR_REPO}' in ${REGION}..."
        gcloud artifacts repositories create "${AR_REPO}" \
            --repository-format=docker \
            --location="${REGION}" \
            --description="Sqwakvox container images" \
            --project="${proj}"
    fi
}

cmd_build() {
    echo "==> Building ${LOCAL_TAG} with Podman (storage in ${PODMAN_BASE})..."
    local vol_args=()
    if [[ -d "/home2/app2/uv" ]]; then
        vol_args=("-v" "/home2/app2/uv:/home2/app2/uv" "--build-arg" "UV_CACHE_DIR=/home2/app2/uv")
    fi
    podman "${PODMAN_ARGS[@]}" build "${vol_args[@]}" -t "${LOCAL_TAG}" .
    echo "==> Built ${LOCAL_TAG}"
}

cmd_local() {
    echo "==> Running ${LOCAL_TAG} locally at http://127.0.0.1:${LOCAL_PORT} ..."
    local env_flags=()
    for var in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY DEEPSEEK_API_KEY SQWAKVOX_WEB_TOKEN SQWAKVOX_TELEMETRY_BUCKET SQWAKVOX_TELEMETRY_PREFIX SQWAKVOX_TELEMETRY_EXPORTER SQWAKVOX_TELEMETRY_GCS_COMPRESS SQWAKVOX_TELEMETRY_GCS_TIMEOUT OTEL_BSP_SCHEDULE_DELAY; do
        if [[ -n "${!var:-}" ]]; then
            env_flags+=("-e" "${var}=${!var}")
        fi
    done
    local adc_file="${HOME}/.config/gcloud/application_default_credentials.json"
    if [[ -n "${SQWAKVOX_TELEMETRY_BUCKET:-}" && -f "${adc_file}" ]]; then
        env_flags+=(
            "-v" "${adc_file}:/run/secrets/gcp-adc.json:ro"
            "-e" "GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-adc.json"
        )
    fi
    if [[ -n "${SQWAKVOX_LOCAL_DATA_DIR:-}" ]]; then
        mkdir -p "${SQWAKVOX_LOCAL_DATA_DIR}"
        env_flags+=(
            "-v" "${SQWAKVOX_LOCAL_DATA_DIR}:${DATA_MOUNT_PATH}"
            "-e" "SQWAKVOX_DATA_DIR=${DATA_MOUNT_PATH}"
        )
    fi
    podman "${PODMAN_ARGS[@]}" run --rm -it \
        -p "127.0.0.1:${LOCAL_PORT}:8080" \
        "${env_flags[@]}" \
        "${LOCAL_TAG}"
}

cmd_push() {
    local proj image_uri
    proj="$(get_project_id)"
    image_uri="$(remote_image_uri)"

    ensure_ar_repo "${proj}"

    echo "==> Authenticating Podman against ${REGION}-docker.pkg.dev..."
    gcloud auth print-access-token | podman "${PODMAN_ARGS[@]}" login \
        -u oauth2accesstoken \
        --password-stdin \
        "https://${REGION}-docker.pkg.dev"

    echo "==> Tagging and pushing ${image_uri}..."
    podman "${PODMAN_ARGS[@]}" tag "${LOCAL_TAG}" "${image_uri}"
    podman "${PODMAN_ARGS[@]}" push "${image_uri}"
    echo "==> Pushed ${image_uri}"
}

ensure_cloudbuild_iam() {
    local proj="$1"
    local project_number build_sa staging_bucket
    project_number="$(gcloud projects describe "${proj}" --format='value(projectNumber)')"
    build_sa="${SQWAKVOX_CLOUDBUILD_SA:-${project_number}-compute@developer.gserviceaccount.com}"
    staging_bucket="${proj}_cloudbuild"

    # Newer GCP projects (and regional Cloud Build) run builds as the default
    # Compute Engine SA. Grant the exact roles needed to read/log in the
    # Cloud Build staging bucket, push to Artifact Registry, and write logs.
    echo "==> Ensuring Cloud Build service account '${build_sa}' has build & staging permissions..."
    for role in roles/cloudbuild.builds.builder roles/logging.logWriter; do
        gcloud projects add-iam-policy-binding "${proj}" \
            --member="serviceAccount:${build_sa}" \
            --role="${role}" \
            --condition=None >/dev/null
    done

    gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" \
        --location="${REGION}" \
        --project="${proj}" \
        --member="serviceAccount:${build_sa}" \
        --role="roles/artifactregistry.writer" \
        --condition=None >/dev/null

    if gcloud storage buckets describe "gs://${staging_bucket}" --project="${proj}" >/dev/null 2>&1; then
        gcloud storage buckets add-iam-policy-binding "gs://${staging_bucket}" \
            --member="serviceAccount:${build_sa}" \
            --role="roles/storage.objectAdmin" \
            --condition=None >/dev/null
    else
        gcloud projects add-iam-policy-binding "${proj}" \
            --member="serviceAccount:${build_sa}" \
            --role="roles/storage.objectViewer" \
            --condition=None >/dev/null
    fi
}

cmd_cloud_build() {
    local proj image_uri
    proj="$(get_project_id)"
    image_uri="$(remote_image_uri)"

    ensure_ar_repo "${proj}"
    ensure_cloudbuild_iam "${proj}"

    echo "==> Submitting remote build to Google Cloud Build for ${image_uri}..."
    gcloud builds submit \
        --tag "${image_uri}" \
        --region="${REGION}" \
        --project="${proj}" \
        .
    echo "==> Built and pushed ${image_uri}"
}

ensure_service_account() {
    local proj="$1"
    local sa_email="${SQWAKVOX_SERVICE_ACCOUNT:-${SERVICE_ACCOUNT_NAME}@${proj}.iam.gserviceaccount.com}"

    if [[ -z "${SQWAKVOX_SERVICE_ACCOUNT:-}" ]]; then
        gcloud services enable iam.googleapis.com --project="${proj}" >/dev/null
        if ! gcloud iam service-accounts describe "${sa_email}" --project="${proj}" >/dev/null 2>&1; then
            echo "==> Creating least-privilege service account '${sa_email}'..." >&2
            gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
                --display-name="Sqwakvox Web Cloud Run Runtime" \
                --project="${proj}" >/dev/null
        fi
    fi

    echo "${sa_email}"
}

ensure_telemetry_bucket() {
    local proj="$1"
    local sa_email="$2"
    echo "==> Ensuring telemetry bucket gs://${TELEMETRY_BUCKET} exists in project '${proj}'..."
    gcloud services enable storage.googleapis.com --project="${proj}" >/dev/null

    if ! gcloud storage buckets describe "gs://${TELEMETRY_BUCKET}" --project="${proj}" >/dev/null 2>&1; then
        echo "==> Creating bucket '${TELEMETRY_BUCKET}' in ${REGION}..."
        gcloud storage buckets create "gs://${TELEMETRY_BUCKET}" \
            --project="${proj}" \
            --location="${REGION}" \
            --uniform-bucket-level-access \
            --public-access-prevention
    fi

    # Apply retention & cost-control lifecycle policy (Nearline after 30d,
    # delete after TELEMETRY_RETENTION_DAYS, abort stale multipart uploads).
    local lifecycle_file
    lifecycle_file="$(mktemp)"
    cat > "${lifecycle_file}" <<EOF
{
  "rule": [
    {
      "action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
      "condition": {"age": 30, "matchesStorageClass": ["STANDARD"]}
    },
    {
      "action": {"type": "Delete"},
      "condition": {"age": ${TELEMETRY_RETENTION_DAYS}}
    },
    {
      "action": {"type": "AbortIncompleteMultipartUpload"},
      "condition": {"age": 1}
    }
  ]
}
EOF
    gcloud storage buckets update "gs://${TELEMETRY_BUCKET}" \
        --project="${proj}" \
        --lifecycle-file="${lifecycle_file}" >/dev/null
    rm -f "${lifecycle_file}"

    # Grant the Cloud Run runtime service account write-only access so telemetry
    # can be uploaded but not read, listed, or deleted by the application.
    gcloud storage buckets add-iam-policy-binding "gs://${TELEMETRY_BUCKET}" \
        --member="serviceAccount:${sa_email}" \
        --role=roles/storage.objectCreator \
        --condition=None >/dev/null
}

ensure_data_bucket() {
    local proj="$1"
    local sa_email="$2"
    echo "==> Ensuring persistent agent-data bucket gs://${DATA_BUCKET} exists in project '${proj}'..."
    gcloud services enable storage.googleapis.com --project="${proj}" >/dev/null

    if ! gcloud storage buckets describe "gs://${DATA_BUCKET}" --project="${proj}" >/dev/null 2>&1; then
        echo "==> Creating agent-data bucket '${DATA_BUCKET}' in ${REGION}..."
        gcloud storage buckets create "gs://${DATA_BUCKET}" \
            --project="${proj}" \
            --location="${REGION}" \
            --uniform-bucket-level-access \
            --public-access-prevention
    fi

    # Enable Object Versioning (protects skills/sessions against accidental
    # overwrites) and scope lifecycle rules so uploads expire after
    # UPLOADS_RETENTION_DAYS while skills/chat_logs/checkpoints never auto-delete.
    local lifecycle_file
    lifecycle_file="$(mktemp)"
    cat > "${lifecycle_file}" <<EOF
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": ${UPLOADS_RETENTION_DAYS}, "matchesPrefix": ["uploads/"]}
    },
    {
      "action": {"type": "Delete"},
      "condition": {"age": 90, "matchesPrefix": ["cache/"]}
    },
    {
      "action": {"type": "Delete"},
      "condition": {"numNewerVersions": 5, "isLive": false}
    },
    {
      "action": {"type": "AbortIncompleteMultipartUpload"},
      "condition": {"age": 1}
    }
  ]
}
EOF
    gcloud storage buckets update "gs://${DATA_BUCKET}" \
        --project="${proj}" \
        --versioning \
        --lifecycle-file="${lifecycle_file}" >/dev/null
    rm -f "${lifecycle_file}"

    # Grant the Cloud Run runtime SA read/write/delete on the agent-data bucket only.
    gcloud storage buckets add-iam-policy-binding "gs://${DATA_BUCKET}" \
        --member="serviceAccount:${sa_email}" \
        --role=roles/storage.objectUser \
        --condition=None >/dev/null
}

cmd_up() {
    local proj image_uri sa_email env_vars=(
        "SQWAKVOX_DOCLING_CONCURRENCY=1"
        "SQWAKVOX_DOC_WORKER_CONCURRENCY=1"
        "OMP_NUM_THREADS=2"
        "MKL_NUM_THREADS=2"
        "SQWAKVOX_TELEMETRY_BUCKET=${TELEMETRY_BUCKET}"
        "SQWAKVOX_TELEMETRY_PREFIX=${TELEMETRY_PREFIX}"
        "SQWAKVOX_DATA_DIR=${DATA_MOUNT_PATH}"
    )
    proj="$(get_project_id)"
    image_uri="$(remote_image_uri)"

    sa_email="$(ensure_service_account "${proj}")"
    ensure_telemetry_bucket "${proj}" "${sa_email}"
    ensure_data_bucket "${proj}" "${sa_email}"

    # TELEMETRY_BUCKET/PREFIX and DATA_DIR are already pinned in env_vars above;
    # only forward opt-in extras from the caller's environment.
    for var in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY DEEPSEEK_API_KEY SQWAKVOX_WEB_TOKEN SQWAKVOX_TELEMETRY_EXPORTER SQWAKVOX_TELEMETRY_GCS_COMPRESS SQWAKVOX_TELEMETRY_GCS_TIMEOUT OTEL_BSP_SCHEDULE_DELAY; do
        if [[ -n "${!var:-}" ]]; then
            env_vars+=("${var}=${!var}")
        fi
    done

    local env_arg=()
    if [[ ${#env_vars[@]} -gt 0 ]]; then
        local IFS=","
        env_arg=("--update-env-vars" "${env_vars[*]}")
    fi

    echo "==> Deploying '${SERVICE_NAME}' to Cloud Run (${REGION}) in DEMO ON mode (SA: ${sa_email})..."
    gcloud run deploy "${SERVICE_NAME}" \
        --image="${image_uri}" \
        --region="${REGION}" \
        --project="${proj}" \
        --platform=managed \
        --execution-environment=gen2 \
        --service-account="${sa_email}" \
        --add-volume="name=agent-data,type=cloud-storage,bucket=${DATA_BUCKET}" \
        --add-volume-mount="volume=agent-data,mount-path=${DATA_MOUNT_PATH}" \
        --no-allow-unauthenticated \
        --max-instances=1 \
        --min-instances="${SQWAKVOX_MIN_INSTANCES:-1}" \
        --no-cpu-throttling \
        --cpu="${SQWAKVOX_CPU:-4}" \
        --memory="${SQWAKVOX_MEMORY:-8Gi}" \
        --timeout=3600 \
        --session-affinity \
        "${env_arg[@]}"

    echo ""
    echo "==> Service deployed! Run './deploy/cloudrun.sh proxy' to open http://127.0.0.1:${LOCAL_PORT}"
}

cmd_proxy() {
    local proj
    proj="$(get_project_id)"
    echo "==> Opening authenticated proxy to '${SERVICE_NAME}' at http://127.0.0.1:${LOCAL_PORT} ..."
    gcloud run services proxy "${SERVICE_NAME}" \
        --region="${REGION}" \
        --project="${proj}" \
        --port="${LOCAL_PORT}"
}

cmd_off() {
    local proj
    proj="$(get_project_id)"
    echo "==> Putting '${SERVICE_NAME}' into idle/off mode (--cpu-throttling --min-instances=0)..."
    gcloud run services update "${SERVICE_NAME}" \
        --region="${REGION}" \
        --project="${proj}" \
        --min-instances=0 \
        --cpu-throttling
    echo "==> '${SERVICE_NAME}' will now scale to 0 immediately with zero idle compute cost."
}

cmd_down() {
    local proj
    proj="$(get_project_id)"
    echo "==> Deleting Cloud Run service '${SERVICE_NAME}' (image stays in Artifact Registry)..."
    gcloud run services delete "${SERVICE_NAME}" \
        --region="${REGION}" \
        --project="${proj}" \
        --quiet
    echo "==> Service deleted. Re-run './deploy/cloudrun.sh up' anytime before a demo."
}

cmd_status() {
    local proj
    proj="$(get_project_id)"
    gcloud run services describe "${SERVICE_NAME}" \
        --region="${REGION}" \
        --project="${proj}"
}

case "${1:-help}" in
    build)       cmd_build ;;
    local)       cmd_local ;;
    push)        cmd_push ;;
    cloud-build) cmd_cloud_build ;;
    up|deploy)   cmd_up ;;
    proxy)       cmd_proxy ;;
    off)         cmd_off ;;
    down)        cmd_down ;;
    status)      cmd_status ;;
    *)
        sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
        ;;
esac
