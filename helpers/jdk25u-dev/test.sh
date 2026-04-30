#!/bin/bash
# Runs INSIDE the Docker container — runs jtreg tests for jdk25u-dev.
set -e

echo "--- Inside Docker: Running tests for ${COMMIT_SHA:0:7} ---"
echo "Target(s): ${TEST_TARGETS}"

BUILD_DIR_ABS="/repo/build_shared"

if [ ! -d "${BUILD_DIR_ABS}" ]; then
    echo "Error: Build directory not found at ${BUILD_DIR_ABS}"
    echo "The build must succeed before running tests."
    exit 1
fi

cd "${BUILD_DIR_ABS}"

if [ "${TEST_TARGETS}" == "ALL" ]; then
    TEST_LIST="tier1"
elif [ "${TEST_TARGETS}" == "NONE" ]; then
    echo "No relevant source code changes found. Skipping tests."
    exit 0
else
    TEST_LIST="${TEST_TARGETS}"
fi

JTWORK_DIR="/repo/JTwork"
JTREPORT_DIR="/repo/JTreport"
rm -rf "${JTWORK_DIR}" "${JTREPORT_DIR}"
mkdir -p "${JTWORK_DIR}" "${JTREPORT_DIR}"

echo "--- Starting Test Execution in ${BUILD_DIR_ABS} ---"

FINAL_EXIT_CODE=0

for TARGET in ${TEST_LIST}; do
    echo "--- Running target: ${TARGET} ---"
    set +e

    if [[ "${TARGET}" == *.java ]]; then
        echo "Detected jtreg test file."
        JTREG_BIN="${JTREG_HOME}/bin/jtreg"
        if [ ! -x "${JTREG_BIN}" ]; then
            echo "jtreg executable not found at ${JTREG_BIN}"
            FINAL_EXIT_CODE=1
            set -e
            continue
        fi
        TARGET_ABS="/repo/${TARGET}"
        "${JTREG_BIN}" \
            -verbose:fail,error \
            -xml \
            -w "${JTWORK_DIR}" \
            -r "${JTREPORT_DIR}" \
            -jdk:"${BUILD_DIR_ABS}/images/jdk" \
            "${TARGET_ABS}"
        EXIT_CODE=$?
    else
        echo "Detected tier/group test. Using make test."
        make test TEST="${TARGET}" \
             JOBS=$(nproc) \
             JTREG="VERBOSE=fail,error" \
             JTREG_REPORT_DIR="${JTREPORT_DIR}" \
             JTREG_WORK_DIR="${JTWORK_DIR}"
        EXIT_CODE=$?
    fi

    set -e
    if [ ${EXIT_CODE} -ne 0 ]; then
        echo "Target ${TARGET} FAILED"
        FINAL_EXIT_CODE=1
    else
        echo "Target ${TARGET} PASSED"
    fi
done

if [ ${FINAL_EXIT_CODE} -eq 0 ]; then
    echo "=== ALL TESTS PASSED ==="
    exit 0
else
    echo "=== SOME TESTS FAILED ==="
    exit 1
fi
