#!/usr/bin/env bash
# Runs INSIDE the Docker container — builds jdk25u-dev.
set -euo pipefail

echo "=== Building JDK for ${COMMIT_SHA:0:7} (Inside Container) ==="
echo "Using Boot JDK: ${BOOT_JDK}"
echo "Using jtreg: ${JTREG_HOME}"

if [ "${WORKTREE_MODE:-0}" != "1" ]; then
    echo "Checking out commit: ${COMMIT_SHA}"
    git checkout -f "${COMMIT_SHA}"
else
    echo "WORKTREE_MODE=1: using pre-applied worktree (HEAD=${COMMIT_SHA})"
fi

export BUILD_DIR_ABS="/repo/build_shared"
echo "--- Shared build dir: ${BUILD_DIR_ABS} ---"

NEED_CONFIGURE=false
if [ ! -f "${BUILD_DIR_ABS}/Makefile" ]; then
    echo "--- No existing Makefile — will configure ---"
    NEED_CONFIGURE=true
    mkdir -p "${BUILD_DIR_ABS}"
fi

cd "${BUILD_DIR_ABS}"

_do_configure() {
    echo "--- Configuring build ---"
    bash ../configure \
        --with-boot-jdk="${BOOT_JDK}" \
        --with-jtreg="${JTREG_HOME}" \
        --enable-ccache \
        --disable-warnings-as-errors \
        --with-debug-level=release \
        --with-native-debug-symbols=none
}

if [ "${NEED_CONFIGURE}" = true ]; then
    _do_configure
else
    echo "--- Skipping configure (incremental build) ---"
fi

echo "--- Running incremental make ---"
set +e
make JOBS="${MAKE_JOBS:-$(nproc)}" images COMPILER_WARNINGS_FATAL=false
MAKE_EXIT=$?
set -e

if [ ${MAKE_EXIT} -ne 0 ]; then
    if [ "${NEED_CONFIGURE}" = false ]; then
        echo "--- Incremental make failed — forcing reconfigure and retry ---"
        _do_configure
        set +e
        make JOBS="${MAKE_JOBS:-$(nproc)}" images COMPILER_WARNINGS_FATAL=false
        MAKE_EXIT2=$?
        set -e
        if [ ${MAKE_EXIT2} -ne 0 ]; then
            echo "--- Reconfigure retry failed — doing full clean rebuild ---"
            cd /repo
            rm -rf "${BUILD_DIR_ABS}"
            mkdir -p "${BUILD_DIR_ABS}"
            cd "${BUILD_DIR_ABS}"
            _do_configure
            make JOBS="${MAKE_JOBS:-$(nproc)}" images COMPILER_WARNINGS_FATAL=false
        fi
    else
        exit ${MAKE_EXIT}
    fi
fi

echo "=== Build OK ==="
