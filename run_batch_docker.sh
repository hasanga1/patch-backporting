#!/bin/bash
# Docker wrapper for batch Java backporting

set -e

# Default values
CONFIG_FILE="batch_java_config.yml"
IMAGE_NAME="patch-backporting"
CONTAINER_NAME="patch-backporting-batch"
BUILD_IMAGE=true

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --build)
            BUILD_IMAGE=true
            shift
            ;;
        --no-build)
            BUILD_IMAGE=false
            shift
            ;;
        --debug)
            DEBUG_FLAG="--debug"
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Build image by default so code changes in workspace are always reflected.
if [ "$BUILD_IMAGE" = true ] || ! docker image inspect $IMAGE_NAME >/dev/null 2>&1; then
    echo "Building Docker image: $IMAGE_NAME"
    docker build -t $IMAGE_NAME "$SCRIPT_DIR"
fi

# Get absolute paths
CONFIG_PATH=$(cd "$SCRIPT_DIR" && pwd)/"$CONFIG_FILE"
DATASET_PATH=$(cd "$SCRIPT_DIR" && pwd)/java_dataset
RESULTS_PATH=$(cd "$SCRIPT_DIR" && pwd)/java_results_with_retrofit

# Ensure directories exist
mkdir -p "$DATASET_PATH/repos"
mkdir -p "$RESULTS_PATH"

echo "=========================================="
echo "Batch Java Backporting - Docker Mode"
echo "=========================================="
echo "Config: $CONFIG_FILE"
echo "Dataset: $DATASET_PATH"
echo "Results: $RESULTS_PATH"
echo ""

# Clean up any existing container with the same name
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Removing previous container: $CONTAINER_NAME"
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

# Run Docker container
docker run --rm \
    -v "$CONFIG_PATH:/app/$CONFIG_FILE" \
    -v "$DATASET_PATH:/app/java_dataset" \
    -v "$RESULTS_PATH:/app/java_results_with_retrofit" \
    -v "/var/run/docker.sock:/var/run/docker.sock" \
    -e "DOCKER_HOST=unix:///var/run/docker.sock" \
    -e "HOST_APP_ROOT=$SCRIPT_DIR" \
    -e "HOST_JAVA_DATASET_DIR=$DATASET_PATH" \
    -e "HOST_JAVA_RESULTS_DIR=$RESULTS_PATH" \
    --name "$CONTAINER_NAME" \
    "$IMAGE_NAME" \
    python batch_java_backport.py --config "$CONFIG_FILE" $DEBUG_FLAG

echo ""
echo "=========================================="
echo "Batch processing complete!"
echo "Results saved to: $RESULTS_PATH"
echo "=========================================="
