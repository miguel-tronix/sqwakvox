#!/usr/bin/env bash
# Helper script to build, test locally with Podman, and deploy/toggle Sqwakvox Web on GCP Cloud Run.
#
# Usage:
#   ./deploy/cloudrun.sh build        # Build container image locally with Podman (uses /home2/app2/podman)
#   ./deploy/cloudrun.sh local        # Run container locally on http://127.0.0.1:8760
#   ./deploy/cloudrun.sh push         # Push local Podman image to GCP Artifact Registry
#   ./deploy/cloudrun.sh cloud-build  # Build & push image remotely via Google Cloud Build (saves local disk/bandwidth)
#   ./deploy/cloudrun.sh up           # Deploy / enable service on Cloud Run (demo ON)
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
    for var in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY DEEPSEEK_API_KEY SQWAKVOX_WEB_TOKEN; do
        if [[ -n "${!var:-}" ]]; then
            env_flags+=("-e" "${var}=${!var}")
        fi
    done
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

cmd_cloud_build() {
    local proj image_uri
    proj="$(get_project_id)"
    image_uri="$(remote_image_uri)"

    ensure_ar_repo "${proj}"

    echo "==> Submitting remote build to Google Cloud Build for ${image_uri}..."
    gcloud builds submit \
        --tag "${image_uri}" \
        --region="${REGION}" \
        --project="${proj}" \
        .
    echo "==> Built and pushed ${image_uri}"
}

cmd_up() {
    local proj image_uri env_vars=()
    proj="$(get_project_id)"
    image_uri="$(remote_image_uri)"

    for var in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY DEEPSEEK_API_KEY SQWAKVOX_WEB_TOKEN; do
        if [[ -n "${!var:-}" ]]; then
            env_vars+=("${var}=${!var}")
        fi
    done

    local env_arg=()
    if [[ ${#env_vars[@]} -gt 0 ]]; then
        local IFS=","
        env_arg=("--set-env-vars" "${env_vars[*]}")
    fi

    echo "==> Deploying '${SERVICE_NAME}' to Cloud Run (${REGION}) in DEMO ON mode..."
    gcloud run deploy "${SERVICE_NAME}" \
        --image="${image_uri}" \
        --region="${REGION}" \
        --project="${proj}" \
        --platform=managed \
        --execution-environment=gen2 \
        --no-allow-unauthenticated \
        --max-instances=1 \
        --min-instances=0 \
        --no-cpu-throttling \
        --cpu=2 \
        --memory=4Gi \
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
        sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
        ;;
esac
