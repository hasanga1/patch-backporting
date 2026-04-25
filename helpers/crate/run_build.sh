#!/bin/bash
# This script builds the Docker image and compiles the code.
set -e # Exit on error

echo "--- Building code for ${COMMIT_SHA:0:7} ---"
# BUILD_DIR must be outside PROJECT_DIR to avoid recursive Docker build context
BUILD_DIR="${BUILD_DIR:-/tmp/crate-build-${COMMIT_SHA:0:7}}"
mkdir -p "${BUILD_DIR}"

MAX_CPU="${MAX_CPU:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1)}"
MAVEN_THREADS="${MAVEN_THREADS:-${MAX_CPU}}"

echo "CPU detected: ${MAX_CPU}"
echo "Maven threads: ${MAVEN_THREADS}"

echo "--- Changing directory to ${PROJECT_DIR} ---"
cd "${PROJECT_DIR}"

# Only reset repo state when NOT in worktree/patch-applied mode.
# When WORKTREE_MODE=1, the ValidationToolkit has already applied patches
# to the working tree — do NOT wipe them with git checkout/clean.
if [ "${WORKTREE_MODE:-0}" != "1" ]; then
    echo "--- Checking out commit... ---"
    git checkout -f ${COMMIT_SHA}
    git clean -fd
fi

# Determine Docker command (with or without sudo)
DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then
        echo "Docker requires sudo. Using 'sudo docker'."
        DOCKER_CMD="sudo docker"
    else
        echo "Warning: Docker command failed. Continuing with 'docker' but expect errors."
    fi
fi

# Create persistent Maven cache volume
${DOCKER_CMD} volume create maven-cache-crate 2>/dev/null || true

HELPER_DIR="${TOOLKIT_DIR:-$(dirname "$0")}"
if ${DOCKER_CMD} image inspect "${IMAGE_TAG}" > /dev/null 2>&1; then
    echo "--- Docker image ${IMAGE_TAG} already exists, skipping build ---"
else
    echo "--- Building Docker image... ---"
    ${DOCKER_CMD} build -t ${IMAGE_TAG} "${HELPER_DIR}"
fi

echo "--- Setting cache permissions... ---"
${DOCKER_CMD} run --rm -u root \
    -v "maven-cache-crate:/root/.m2" \
    -v "${BUILD_DIR}/build:/repo/build_outputs/build" \
    ${IMAGE_TAG} \
    chown -R root:root /root/.m2

echo "--- Building with Maven... ---"
# Build and install to local repo, skip tests
run_maven_build() {
${DOCKER_CMD} run --rm \
    --dns=8.8.8.8 \
    -v "maven-cache-crate:/root/.m2" \
    -v "${PROJECT_DIR}:/repo" \
    -w /repo \
    ${IMAGE_TAG} \
    bash -c "git config --global --add safe.directory /repo && \
    export MAVEN_OPTS=\"\${MAVEN_OPTS:-} -XX:ActiveProcessorCount=${MAX_CPU}\" && \
    mkdir -p /root/.m2 && \
    cat <<EOF > /root/.m2/toolchains.xml
<toolchains>
  <toolchain>
    <type>jdk</type>
    <provides>
      <version>24.0.2</version>
      <vendor>temurin</vendor>
    </provides>
    <configuration>
      <jdkHome>/opt/java/openjdk</jdkHome>
    </configuration>
  </toolchain>
  <toolchain>
    <type>jdk</type>
    <provides>
      <version>22.0.2+9</version>
      <vendor>temurin</vendor>
    </provides>
    <configuration>
      <jdkHome>/opt/java/jdk-22.0.2+9</jdkHome>
    </configuration>
  </toolchain>
  <toolchain>
    <type>jdk</type>
    <provides>
      <version>21.0.4+7</version>
      <vendor>temurin</vendor>
    </provides>
    <configuration>
      <jdkHome>/opt/java/jdk-21.0.4+7</jdkHome>
    </configuration>
  </toolchain>
  <toolchain>
    <type>jdk</type>
    <provides>
      <version>17.0.12+7</version>
      <vendor>temurin</vendor>
    </provides>
    <configuration>
      <jdkHome>/opt/java/jdk-17.0.12+7</jdkHome>
    </configuration>
  </toolchain>
</toolchains>
EOF
    mvn clean install -DskipTests -T ${MAVEN_THREADS} \
        --global-toolchains /root/.m2/toolchains.xml \
        -Dforbiddenapis.skip=true \
        -Dmaven.javadoc.skip=true \
        -Dcheckstyle.skip=true \
        -Dpmd.skip=true \
    -Denforcer.skip=true"
}

if run_maven_build; then
    echo "✅ Build Succeeded"
    exit 0
else
  echo "⚠️ Build failed; cleaning cached JDK archives and retrying once..."
  ${DOCKER_CMD} run --rm -u root \
    -v "maven-cache-crate:/root/.m2" \
    ${IMAGE_TAG} \
    bash -c "rm -rf /root/.m2/jdks/OpenJDK23U-jdk_*_23.0.2_7.tar.gz /root/.m2/jdks/jdk-23* /root/.m2/jdks/.cache 2>/dev/null || true"

  if run_maven_build; then
    echo "✅ Build Succeeded (after cache cleanup retry)"
    exit 0
  fi

  echo "❌ Build Failed"
  exit 1
fi

echo "--- Build complete for ${COMMIT_SHA:0:7} ---"
