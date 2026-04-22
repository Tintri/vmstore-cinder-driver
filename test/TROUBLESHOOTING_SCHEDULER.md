# Troubleshooting: "Could not find any available weighted backend"

## Error Analysis

```
schedule allocate volume: Could not find any available weighted backend.
```

This error comes from **cinder-scheduler** and means the scheduler cannot find a backend that:
1. Has enough free capacity
2. Meets the volume type requirements
3. Is in a healthy state
4. Passes the configured filters

## Root Cause Analysis

### Most Likely Causes (in order of probability)

#### 1. **Capacity Reporting Issue** ⭐ MOST LIKELY

**Symptoms:**
- First 171 volumes succeed
- Then all subsequent volumes fail
- Error persists even after stopping parallel creates

**What's happening:**
Your driver's `_update_volume_stats()` method may be reporting incorrect capacity. Looking at your driver code:

```python
def _update_volume_stats(self) -> None:
    # ... code ...
    capacity, free, _used = self._get_capacity_info(share_string)
    
    pool = {
        'free_capacity_gb': free / float(units.Gi),
        'total_capacity_gb': capacity / float(units.Gi),
        'provisioned_capacity_gb': provisioned_bytes / float(units.Gi),
        'max_over_subscription_ratio': max_osr,
        # ...
    }
```

**The problem:** The scheduler uses this to calculate available space. If:
- `free_capacity_gb` reports 0 or negative
- NFS mount becomes stale
- Stat cache expires and refresh fails
- Over-subscription calculation hits limits

**How to diagnose:**

```bash
# On DevStack host, check what the scheduler sees
mysql cinder -e "SELECT host, free_capacity_gb, total_capacity_gb, allocated_capacity_gb, provisioned_capacity_gb, updated_at FROM services WHERE binary='cinder-volume';"

# Or via API
openstack volume service list --long

# Check actual backend stats
tail -f /var/log/cinder/cinder-volume.log | grep "_update_volume_stats\|get_volume_stats"
```

**Quick Fix Test:**

```bash
# Restart cinder-volume to force stats refresh
sudo systemctl restart devstack@c-vol

# Try creating a volume
openstack volume create --size 1 test-after-restart
```

If this works, it confirms a stats caching/reporting issue.

#### 2. **Stats Cache Not Being Refreshed**

Your driver has stats caching:

```python
cache_period = self.configuration.vmstore_stats_cache_period
```

**Issue:** If the cache period is too long, the scheduler uses stale data showing full capacity.

**Solution:** In `cinder.conf`, add or verify:

```ini
[vmstore_nfs]
vmstore_stats_cache_period = 60  # Refresh every 60 seconds (or 0 to disable)
```

#### 3. **Over-Subscription Ratio Exceeded**

The scheduler calculates: `available = (total_capacity_gb * max_over_subscription_ratio) - provisioned_capacity_gb`

If `provisioned_capacity_gb` exceeds the over-subscription limit, no more volumes can be scheduled.

**Check in cinder.conf:**

```ini
[vmstore_nfs]
max_over_subscription_ratio = 20.0  # Default is often 1.0 (no over-subscription)
reserved_percentage = 0  # Percentage to reserve
```

**Calculation example:**
- Total capacity: 1000 GB
- Over-subscription ratio: 1.0 (no over-subscription)
- Reserved: 5%
- Available for provisioning: 1000 * 1.0 * (1 - 0.05) = 950 GB

After creating 171 volumes of varying sizes, if `provisioned_capacity_gb` >= 950 GB, scheduling stops.

#### 4. **NFS Mount Issues**

**Check on DevStack host:**

```bash
# Verify NFS share is mounted
mount | grep vmstore
df -h | grep vmstore

# Check if mount is stale
ls -la /opt/stack/data/cinder/mnt/<hash>/  # Should list volumes

# Check NFS server connectivity
showmount -e <vmstore-nfs-ip>
```

**If mount is stale:**

```bash
# In cinder.conf, check:
[vmstore_nfs]
nfs_shares_config = /etc/cinder/nfs_shares

# Verify the shares file
cat /etc/cinder/nfs_shares
# Should contain: <vmstore-ip>:/export/path

# Force remount
sudo umount /opt/stack/data/cinder/mnt/<hash>
sudo systemctl restart devstack@c-vol
```

#### 5. **Backend in Error State**

**Check service status:**

```bash
openstack volume service list
# Look for "State" column - should be "up", not "down"
```

**If down:**

```bash
# Check cinder-volume logs
journalctl -u devstack@c-vol -n 100

# Common causes:
# - Configuration error
# - API connectivity issue
# - NFS mount failed during initialization
```

## Diagnostic Procedure

### Step 1: Check Scheduler Logs (SSH required)

```bash
# Your coworker should run this:
journalctl -u devstack@c-sch --since "10 minutes ago" | grep -i "weighted\|capacity\|filter"
```

This will show why backends are being filtered out.

### Step 2: Enable Scheduler Debug Logging

In `/etc/cinder/cinder.conf`:

```ini
[DEFAULT]
debug = True
scheduler_default_filters = AvailabilityZoneFilter,CapacityFilter,CapabilitiesFilter
```

Restart:
```bash
sudo systemctl restart devstack@c-sch
```

Now try creating a volume and check logs:
```bash
journalctl -u devstack@c-sch -f
```

You'll see exactly which filter is rejecting the backend.

### Step 3: Check Backend Statistics

```bash
# Query what cinder-volume is reporting
grep -A 20 "Updated volume backend statistics" /var/log/cinder/cinder-volume.log | tail -30
```

Look for:
```
free_capacity_gb: 0  # ← BAD! Should be > 0
provisioned_capacity_gb: 950  # ← Compare with total * over-subscription
max_over_subscription_ratio: 1.0
```

### Step 4: Force Stats Update

```python
# Your coworker can run this via DevStack:
from cinder import context
from cinder.volume import rpcapi

ctxt = context.get_admin_context()
volume_rpcapi = rpcapi.VolumeAPI()
# Force stats update
volume_rpcapi.publish_service_capabilities(ctxt)
```

## Quick Fixes to Try

### Fix 1: Increase Over-Subscription (Temporary workaround)

```ini
[vmstore_nfs]
max_over_subscription_ratio = 20.0  # Allow thin provisioning
```

### Fix 2: Disable Stats Caching (For debugging)

```ini
[vmstore_nfs]
vmstore_stats_cache_period = 0  # Force refresh every time
```

### Fix 3: Restart All Cinder Services

```bash
sudo systemctl restart devstack@c-sch devstack@c-vol devstack@c-api
```

### Fix 4: Clear Cached Stats Database

```bash
# This forces scheduler to re-query backend stats
mysql cinder -e "UPDATE services SET updated_at='2020-01-01 00:00:00' WHERE binary='cinder-volume';"
sudo systemctl restart devstack@c-sch
```

## Verification After Fix

```bash
# 1. Check backend is reporting capacity
openstack volume service list --long

# 2. Create test volume
openstack volume create --size 1 test-capacity-check

# 3. Verify it succeeded
openstack volume show test-capacity-check -c status

# 4. Check updated stats
mysql cinder -e "SELECT host, free_capacity_gb, provisioned_capacity_gb, updated_at FROM services WHERE binary='cinder-volume';"
```

## Prevention for Production

1. **Set appropriate over-subscription ratio** based on your typical workload
2. **Monitor capacity trends** - alert when `free_capacity_gb` approaches 0
3. **Implement stats refresh monitoring** - ensure `updated_at` timestamp is recent
4. **Add health checks** for NFS mounts before reporting stats
5. **Use Tempest regularly** to catch capacity calculation bugs

## Code Fix Suggestion

If the issue is in the stats calculation, you may need to add defensive checks in `_update_volume_stats()`:

```python
def _update_volume_stats(self) -> None:
    # ... existing code ...
    
    # Add validation
    if capacity <= 0:
        LOG.warning('Invalid capacity reported: %s, using fallback', capacity)
        capacity = 1 * units.Ti  # Fallback to avoid division by zero
    
    if free < 0:
        LOG.warning('Negative free space: %s, clamping to 0', free)
        free = 0
    
    # Log for debugging
    LOG.info('VMstore backend stats: total=%s GB, free=%s GB, provisioned=%s GB',
             capacity / units.Gi, free / units.Gi, provisioned_bytes / units.Gi)
```

## What Your Coworker Should Send You

Ask for these logs/outputs:

```bash
# 1. Service status
openstack volume service list --long > service_status.txt

# 2. Scheduler logs during failure
journalctl -u devstack@c-sch --since "1 hour ago" > scheduler_logs.txt

# 3. Volume service logs
journalctl -u devstack@c-vol --since "1 hour ago" | grep -i "stats\|capacity" > volume_stats_logs.txt

# 4. Database state
mysql cinder -e "SELECT host, free_capacity_gb, total_capacity_gb, provisioned_capacity_gb, updated_at FROM services WHERE binary='cinder-volume';" > db_capacity.txt

# 5. Configuration
grep -A 10 "\[vmstore" /etc/cinder/cinder.conf > cinder_config.txt

# 6. One failed volume creation attempt with timestamp
openstack volume create --size 1 debug-test 2>&1 | tee volume_create_error.txt
```

Send these files for analysis. The scheduler logs will show exactly which filter is blocking volume creation.

## Expected Resolution

Most likely this is a **capacity reporting + over-subscription** issue. The fix will be one of:

1. Increase `max_over_subscription_ratio` to allow thin provisioning
2. Fix NFS mount staleness causing `_get_capacity_info()` to return 0
3. Reduce `vmstore_stats_cache_period` to refresh capacity more frequently
4. Add error handling in `_update_volume_stats()` to handle edge cases

This is why **Tempest is valuable** - it has specific tests for scheduler capacity scenarios that would have caught this during development!
