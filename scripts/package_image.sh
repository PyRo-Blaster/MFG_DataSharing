#!/usr/bin/env bash

set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-mfg-data-sharing}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
ARCHIVE_NAME="${ARCHIVE_NAME:-${IMAGE_NAME//\//-}-${IMAGE_TAG}.tar}"
IMAGE_PLATFORM="${IMAGE_PLATFORM:-linux/amd64}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_DIR}"

echo "==> Building ${IMAGE_NAME}:${IMAGE_TAG} for ${IMAGE_PLATFORM}"
docker buildx build \
  --platform "${IMAGE_PLATFORM}" \
  --load \
  -t "${IMAGE_NAME}:${IMAGE_TAG}" \
  .

echo "==> Saving image to ${ARCHIVE_NAME}"
docker save -o "${ARCHIVE_NAME}" "${IMAGE_NAME}:${IMAGE_TAG}"

echo "==> Inspecting image platform"
docker image inspect "${IMAGE_NAME}:${IMAGE_TAG}" \
  --format 'OS/Arch: {{.Os}}/{{.Architecture}}'

echo "==> Done"
echo "Archive: ${PROJECT_DIR}/${ARCHIVE_NAME}"
