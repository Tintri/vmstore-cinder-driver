#!/bin/bash
set -e

DRIVER_VER=$(python -c 'import cinder.objects as o; o.register_all(); from cinder.volume.drivers.vmstore.nfs import VmstoreNfsDriver; print(VmstoreNfsDriver.VERSION)' 2>/dev/null || echo "n/a")

echo "========================================"
echo " VMstore Cinder Driver — Tests"
echo "========================================"
echo "Python:     $(python --version 2>&1)"
echo "Cinder ref: $(cd /cinder && git log --oneline -1)"
echo "Driver:     ${DRIVER_VER}"
echo "----------------------------------------"

cd /cinder

MODE="${1:-unit}"

run_unit() {
    echo "[ UNIT ] stestr run cinder.tests.unit.volume.drivers.vmstore"
    echo "----------------------------------------"
    python -m stestr run --concurrency 1 cinder.tests.unit.volume.drivers.vmstore
}

run_functional() {
    echo "[ FUNCTIONAL ] stestr run cinder.tests.functional.test_vmstore_driver"
    echo "----------------------------------------"
    OS_TEST_PATH=./cinder/tests/functional \
    python -m stestr run --concurrency 1 cinder.tests.functional.test_vmstore_driver
}

UNIT_EXIT=0
FUNC_EXIT=0

case "$MODE" in
    unit)
        run_unit || UNIT_EXIT=$?
        ;;
    functional)
        run_functional || FUNC_EXIT=$?
        ;;
    all)
        run_unit    || UNIT_EXIT=$?
        echo ""
        run_functional || FUNC_EXIT=$?
        ;;
    *)
        # Treat unknown args as a stestr filter (e.g. a specific test class)
        echo "[ CUSTOM ] stestr run --concurrency 1 $*"
        echo "----------------------------------------"
        python -m stestr run --concurrency 1 "$@"
        exit $?
        ;;
esac

echo ""
echo "----------------------------------------"
[ $UNIT_EXIT -eq 0 ]     && echo "Unit tests:       PASSED" || echo "Unit tests:       FAILED (exit $UNIT_EXIT)"
[ $FUNC_EXIT -eq 0 ]     && echo "Functional tests: PASSED" || echo "Functional tests: FAILED (exit $FUNC_EXIT)"
echo "========================================"

[ $((UNIT_EXIT + FUNC_EXIT)) -eq 0 ] && exit 0 || exit 1
