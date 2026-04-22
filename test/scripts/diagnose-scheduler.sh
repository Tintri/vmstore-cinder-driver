#!/bin/bash
# Scheduler Diagnostics Script
# Run this on the DevStack host to diagnose scheduling errors

set -euo pipefail

echo "=================================="
echo "Cinder Scheduler Diagnostic Tool"
echo "=================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if running as correct user
if [[ ! -f "/opt/stack/devstack/openrc" ]]; then
    echo -e "${RED}Error: Not on DevStack host or DevStack not found${NC}"
    exit 1
fi

# Source OpenStack credentials
source /opt/stack/devstack/openrc admin admin

echo "1. Checking Cinder Services Status"
echo "-----------------------------------"
openstack volume service list --long
echo ""

echo "2. Checking Backend Capacity (Database View)"
echo "--------------------------------------------"
mysql cinder <<EOF
SELECT 
    host,
    ROUND(free_capacity_gb, 2) as free_gb,
    ROUND(total_capacity_gb, 2) as total_gb,
    ROUND(allocated_capacity_gb, 2) as allocated_gb,
    ROUND(provisioned_capacity_gb, 2) as provisioned_gb,
    updated_at,
    disabled,
    disabled_reason
FROM services 
WHERE binary='cinder-volume' AND deleted=0;
EOF
echo ""

echo "3. Checking NFS Mounts"
echo "---------------------"
if mount | grep -i cinder | grep -i nfs; then
    echo -e "${GREEN}✓ NFS mounts found${NC}"
    mount | grep -i cinder | grep -i nfs
else
    echo -e "${RED}✗ No NFS mounts found for Cinder${NC}"
fi
echo ""

echo "4. Recent Scheduler Logs (Last 50 scheduler messages)"
echo "-----------------------------------------------------"
journalctl -u devstack@c-sch --no-pager -n 50 --since "10 minutes ago" | grep -i "filter\|weighted\|capacity\|error" || echo "No recent scheduler activity"
echo ""

echo "5. Recent Volume Service Stats Updates"
echo "--------------------------------------"
journalctl -u devstack@c-vol --no-pager -n 100 --since "10 minutes ago" | grep -i "_update_volume_stats\|Updated volume backend statistics" | tail -20 || echo "No recent stats updates"
echo ""

echo "6. Checking Scheduler Configuration"
echo "-----------------------------------"
echo "Filters configured:"
grep "scheduler_default_filters" /etc/cinder/cinder.conf || echo "Using defaults"
echo ""
echo "Scheduler max attempts:"
grep "scheduler_max_attempts" /etc/cinder/cinder.conf || echo "Using default (3)"
echo ""

echo "7. VMstore Backend Configuration"
echo "--------------------------------"
echo "Looking for vmstore configuration sections:"
grep -A 15 "\[vmstore" /etc/cinder/cinder.conf | grep -E "max_over_subscription|reserved_percentage|vmstore_stats_cache" || echo "Not found in config"
echo ""

echo "8. Attempting Test Volume Creation"
echo "----------------------------------"
TEST_VOL_NAME="diagnostic-test-$(date +%s)"
echo "Creating volume: $TEST_VOL_NAME"

if openstack volume create --size 1 "$TEST_VOL_NAME" 2>&1; then
    echo -e "${GREEN}✓ Test volume created successfully${NC}"
    echo "Waiting 5 seconds..."
    sleep 5
    openstack volume show "$TEST_VOL_NAME" -c status -f value
    echo "Cleaning up test volume..."
    openstack volume delete "$TEST_VOL_NAME"
else
    echo -e "${YELLOW}⚠ Volume creation failed (this is the error we're diagnosing)${NC}"
    echo ""
    echo "Last scheduler error:"
    journalctl -u devstack@c-sch --no-pager -n 20 | grep -i "error\|no.*backend" | tail -5
fi
echo ""

echo "9. Volume Distribution on Backend"
echo "---------------------------------"
echo "Number of volumes by status:"
mysql cinder <<EOF
SELECT status, COUNT(*) as count 
FROM volumes 
WHERE deleted=0 
GROUP BY status;
EOF
echo ""

echo "10. Recommended Actions"
echo "----------------------"

# Check if free capacity is 0
FREE_CAPACITY=$(mysql cinder -N -e "SELECT COALESCE(free_capacity_gb, 0) FROM services WHERE binary='cinder-volume' AND deleted=0 LIMIT 1;")

if (( $(echo "$FREE_CAPACITY <= 0" | bc -l) )); then
    echo -e "${RED}⚠ ISSUE FOUND: free_capacity_gb is $FREE_CAPACITY ${NC}"
    echo ""
    echo "Likely causes:"
    echo "  1. max_over_subscription_ratio too low (check config)"
    echo "  2. NFS mount showing full or stale"
    echo "  3. Stats cache not refreshing"
    echo ""
    echo "Try these fixes:"
    echo "  a) Restart cinder-volume: sudo systemctl restart devstack@c-vol"
    echo "  b) Check NFS mount: df -h | grep cinder"
    echo "  c) Increase over-subscription in cinder.conf: max_over_subscription_ratio = 20.0"
else
    echo -e "${GREEN}✓ Backend reports available capacity: ${FREE_CAPACITY} GB${NC}"
    echo ""
    echo "If volumes still fail, check:"
    echo "  1. Volume type compatibility"
    echo "  2. Scheduler filter configuration"
    echo "  3. Recent scheduler logs above for filter rejections"
fi

echo ""
echo "=================================="
echo "Diagnostic complete!"
echo "=================================="
echo ""
echo "To monitor in real-time, run:"
echo "  journalctl -u devstack@c-sch -f"
echo "  journalctl -u devstack@c-vol -f"
