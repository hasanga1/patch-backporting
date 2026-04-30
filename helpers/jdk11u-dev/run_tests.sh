#!/bin/bash
# Runs on the HOST — launches Docker to run jdk11u-dev tests.
set -euo pipefail

echo "=== Starting JDK 11 Tests in Docker for ${COMMIT_SHA:0:7} ==="

TEST_SCRIPT_PATH_IN_CONTAINER="/tmp/test.sh"
LOCAL_TEST_SCRIPT="${TOOLKIT_DIR}/test.sh"

if [ ! -f "${LOCAL_TEST_SCRIPT}" ]; then
    echo "Error: test.sh not found at ${LOCAL_TEST_SCRIPT}"
    exit 1
fi

if docker run --rm --dns=8.8.8.8 \
    -v "${PROJECT_DIR}:/repo" \
    -v "${LOCAL_TEST_SCRIPT}:${TEST_SCRIPT_PATH_IN_CONTAINER}:ro" \
    -e "COMMIT_SHA=${COMMIT_SHA}" \
    -e "BUILD_DIR_NAME=${BUILD_DIR_NAME:-build_shared}" \
    -e "TEST_TARGETS=${TEST_TARGETS:-NONE}" \
    -e "JTREG_HOME=${JTREG_HOME:-/opt/jtreg}" \
    -w /repo \
    "${BUILDER_IMAGE_TAG}" \
    bash "${TEST_SCRIPT_PATH_IN_CONTAINER}"
then
    echo "=== Tests passed for ${COMMIT_SHA:0:7} ==="
    exit 0
else
    echo "=== Tests failed for ${COMMIT_SHA:0:7} ==="
    exit 1
fi
