#!/bin/bash
# Quick Tempest Load Test Runner
# This replicates your coworker's batch testing strategy using Tempest

set -euo pipefail

CONCURRENCY=${1:-20}  # Number of parallel tests (default 20)
BATCHES=${2:-10}      # Number of batches (default 10)
SLEEP_TIME=${3:-10}   # Sleep between batches in seconds

echo "======================================"
echo "Tempest Load Test for VMstore Driver"
echo "======================================"
echo "Configuration:"
echo "  - Concurrency: $CONCURRENCY tests in parallel"
echo "  - Batches: $BATCHES"
echo "  - Sleep between batches: ${SLEEP_TIME}s"
echo ""

# Check if we're in the right environment
if [[ ! -d "/opt/stack/tempest" ]]; then
    echo "Error: Not on DevStack host or Tempest not found"
    exit 1
fi

cd /opt/stack/tempest
source /opt/stack/devstack/openrc admin admin

# Define test patterns that create volumes
VOLUME_TESTS=(
    "tempest.api.volume.test_volumes_actions.VolumesActionsTest.test_reserve_unreserve_volume"
    "tempest.api.volume.test_volumes_negative.VolumesNegativeTest.test_create_volume_with_invalid_size"
    "tempest.api.volume.test_volumes_list.VolumesListTestJSON.test_volume_list"
)

# Create a custom test list file
TEST_LIST_FILE="/tmp/vmstore_load_tests.txt"
cat > "$TEST_LIST_FILE" <<EOF
# Volume creation and basic operations
tempest.api.volume.test_volumes_actions.VolumesActionsTest
tempest.api.volume.test_volumes_snapshots.VolumesSnapshotTest
tempest.api.volume.test_volumes_list.VolumesListTestJSON
EOF

echo "Test list created at: $TEST_LIST_FILE"
echo ""

# Function to run a batch
run_batch() {
    local batch_num=$1
    echo "----------------------------------------"
    echo "Running Batch $batch_num of $BATCHES"
    echo "Time: $(date)"
    echo "----------------------------------------"
    
    # Run tests with specified concurrency
    if tempest run --load-list "$TEST_LIST_FILE" \
                   --concurrency "$CONCURRENCY" \
                   --suppress-attachments; then
        echo "✓ Batch $batch_num completed successfully"
    else
        echo "✗ Batch $batch_num had failures"
        echo "Check logs: tempest last --subunit | subunit2pyunit"
        
        # Show recent scheduler errors
        echo ""
        echo "Recent scheduler logs:"
        journalctl -u devstack@c-sch --no-pager -n 10 --since "1 minute ago" | grep -i error || echo "No errors in scheduler"
        
        # Ask if user wants to continue
        read -p "Continue with remaining batches? (y/n) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Aborting remaining batches"
            exit 1
        fi
    fi
    
    # Check backend capacity after each batch
    echo ""
    echo "Backend capacity check:"
    mysql cinder -N -e "SELECT CONCAT('Free: ', ROUND(free_capacity_gb, 2), ' GB, Provisioned: ', ROUND(provisioned_capacity_gb, 2), ' GB') FROM services WHERE binary='cinder-volume' LIMIT 1;" || echo "Could not query capacity"
    echo ""
}

# Main execution loop
echo "Starting load test at $(date)"
echo ""

for ((batch=1; batch<=BATCHES; batch++)); do
    run_batch "$batch"
    
    if [[ $batch -lt $BATCHES ]]; then
        echo "Sleeping for ${SLEEP_TIME} seconds before next batch..."
        sleep "$SLEEP_TIME"
    fi
done

echo ""
echo "======================================"
echo "Load Test Complete!"
echo "======================================"
echo "Completed at: $(date)"
echo ""

# Summary statistics
echo "Summary:"
echo "--------"
stestr last --subunit | subunit-stats || echo "Could not generate stats"

echo ""
echo "Final backend status:"
mysql cinder <<EOF
SELECT 
    ROUND(free_capacity_gb, 2) as free_gb,
    ROUND(total_capacity_gb, 2) as total_gb,
    ROUND(provisioned_capacity_gb, 2) as provisioned_gb,
    updated_at
FROM services 
WHERE binary='cinder-volume' 
LIMIT 1;
EOF

echo ""
echo "Volume counts by status:"
mysql cinder <<EOF
SELECT status, COUNT(*) as count 
FROM volumes 
WHERE deleted=0 
GROUP BY status;
EOF

echo ""
echo "To view detailed results:"
echo "  stestr last --subunit | subunit2html > /tmp/tempest_results.html"
echo "  firefox /tmp/tempest_results.html"
echo ""
echo "To see failed tests:"
echo "  stestr failing"
echo ""
echo "To rerun only failed tests:"
echo "  tempest run --failing"
