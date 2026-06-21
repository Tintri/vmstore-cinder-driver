#!/bin/bash
set -e

echo "========================================"
echo " VMstore Cinder Driver — Unit Tests"
echo "========================================"
echo "Python:     $(python --version 2>&1)"
echo "Cinder ref: $(cd /cinder && git log --oneline -1)"
DRIVER_VER=$(python -c 'import cinder.objects as o; o.register_all(); from cinder.volume.drivers.vmstore.nfs import VmstoreNfsDriver; print(VmstoreNfsDriver.VERSION)' 2>/dev/null || echo "n/a")
echo "Driver:     ${DRIVER_VER}"
echo "----------------------------------------"

cd /cinder

# If arguments are passed, use them as the stestr filter (e.g. a specific module or test)
if [ $# -gt 0 ]; then
    TEST_TARGET="$*"
else
    TEST_TARGET="cinder.tests.unit.volume.drivers.vmstore"
fi

echo "Running: stestr run --concurrency 1 ${TEST_TARGET}"
echo "----------------------------------------"

python -m stestr run --concurrency 1 ${TEST_TARGET}
EXIT_CODE=$?

echo "----------------------------------------"
if [ $EXIT_CODE -eq 0 ]; then
    echo "Result: ALL TESTS PASSED"
else
    echo "Result: FAILURES DETECTED (exit code ${EXIT_CODE})"
fi
echo "========================================"
exit $EXIT_CODE
