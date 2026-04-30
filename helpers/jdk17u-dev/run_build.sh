#!/bin/bash
# Runs on the HOST — launches Docker to build jdk17u-dev.
set -euo pipefail

echo "=== Starting JDK 17 Build in Docker for ${COMMIT_SHA:0:7} ==="

BUILD_SCRIPT_PATH_IN_CONTAINER="/tmp/build.sh"
LOCAL_BUILD_SCRIPT="${TOOLKIT_DIR}/build.sh"

CCACHE_DIR="${PROJECT_DIR}/../.ccache_jdk17"
mkdir -p "${CCACHE_DIR}"

if docker run --rm --dns=8.8.8.8 \
    -v "${PROJECT_DIR}:/repo" \
    -v "${LOCAL_BUILD_SCRIPT}:${BUILD_SCRIPT_PATH_IN_CONTAINER}:ro" \
    -v "${CCACHE_DIR}:/root/.ccache" \
    -e "COMMIT_SHA=${COMMIT_SHA}" \
    -e "BUILD_DIR_NAME=${BUILD_DIR_NAME:-build_shared}" \
    -e "BOOT_JDK=${BOOT_JDK:-/opt/java/openjdk}" \
    -e "JTREG_HOME=${JTREG_HOME:-/opt/jtreg}" \
    -e "CCACHE_DIR=/root/.ccache" \
    -e "WORKTREE_MODE=${WORKTREE_MODE:-0}" \
    -w /repo \
    "${BUILDER_IMAGE_TAG}" \
    bash "${BUILD_SCRIPT_PATH_IN_CONTAINER}"
then
    echo "=== Build succeeded for ${COMMIT_SHA:0:7} ==="
    exit 0
else
    echo "=== Build FAILED for ${COMMIT_SHA:0:7} ==="
    exit 1
fi
