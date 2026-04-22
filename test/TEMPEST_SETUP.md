# Tempest Testing Guide for VMstore Cinder Driver

## What is Tempest?

Tempest is OpenStack's official integration test suite. Unlike unit tests, it treats your deployment as a black box and validates functionality through the REST API, just like a real user would.

## Setting Up Tempest on DevStack

### 1. Install Tempest in DevStack

Your coworker's DevStack likely already has Tempest installed. To verify:

```bash
# On the DevStack host
cd /opt/stack/tempest
source /opt/stack/devstack/openrc admin admin
tempest version
```

If not installed, add this to `local.conf` and rerun `stack.sh`:

```ini
[[local|localrc]]
enable_plugin tempest https://opendev.org/openstack/tempest
```

### 2. Configure Tempest for Cinder Testing

Create or update `etc/tempest.conf`:

```bash
cd /opt/stack/tempest
# Generate initial config
tempest init myconfig
cd myconfig
tempest-config-generator > etc/tempest.conf.sample
```

Key configuration sections for Cinder:

```ini
[volume]
# Backend name from cinder.conf
backend_names = vmstore_nfs
# Storage protocol
storage_protocol = NFS
# Volume type for testing
volume_type = vmstore

[volume-feature-enabled]
# Capabilities your driver supports
backup = False
snapshot = True
clone = True
manage_volume = False
manage_snapshot = False
extend_attached_volume = False
extend_attached_encrypted_volume = False
```

### 3. Running Cinder Tempest Tests

#### Basic Smoke Test (Quick Validation)
```bash
cd /opt/stack/tempest
tempest run --regex tempest.api.volume.test_volumes_actions
```

#### Full Volume Test Suite
```bash
tempest run --regex tempest.api.volume
```

#### Specific Test Categories
```bash
# Volume creation tests
tempest run --regex tempest.api.volume.test_volumes_negative

# Snapshot tests
tempest run --regex tempest.api.volume.test_volumes_snapshots

# Clone/extend tests
tempest run --regex tempest.api.volume.test_volumes_extend
```

### 4. Load Testing with Tempest

Tempest has a **parallel execution mode** perfect for load testing:

```bash
# Run 20 volume tests in parallel (adjust concurrency)
tempest run --concurrency 20 --regex tempest.api.volume.test_volumes_actions

# Full stress test - all Cinder tests in parallel
tempest run --concurrency 10 --regex '(tempest.api.volume|tempest.scenario.test_volume)'
```

### 5. Custom Load Test Configuration

Create a custom test list:

**File: `vmstore_load_test.txt`**
```
tempest.api.volume.test_volumes_actions.VolumesActionsTest.test_volume_bootable
tempest.api.volume.test_volumes_actions.VolumesActionsTest.test_reserve_unreserve_volume
tempest.api.volume.test_volumes_snapshots.VolumesSnapshotTest.test_snapshot_create_get_list_update_delete
tempest.api.volume.test_volumes_list.VolumesListTestJSON.test_volume_list
```

Run it:
```bash
tempest run --load-list vmstore_load_test.txt --concurrency 20
```

### 6. Interpreting Results

Tempest generates detailed reports:

```bash
# View results
cat .stestr.conf  # Shows results location

# Generate HTML report (if installed)
stestr last --subunit | subunit2html > tempest_results.html

# Check failed tests
stestr failing

# Rerun only failures
tempest run --failing
```

### 7. Debugging Failed Tests

Enable debug logging in `etc/tempest.conf`:

```ini
[DEFAULT]
debug = True
log_file = tempest.log
```

Check Cinder logs on DevStack:
```bash
journalctl -u devstack@c-vol -f
journalctl -u devstack@c-sch -f  # Scheduler logs (important for your error)
```

## Important Notes for Your Scenario

1. **Gradual Load Testing**: Start with concurrency=5, then 10, 20 to identify breaking points
2. **Monitor Scheduler**: Watch scheduler logs during parallel runs: `journalctl -u devstack@c-sch -f`
3. **Backend Capacity**: Tempest will respect capacity limits - the scheduling error reported likely indicates a capacity reporting issue
4. **Cleanup**: After load tests, clean up test volumes:
   ```bash
   openstack volume list --all-projects | grep available | awk '{print $2}' | xargs -n1 openstack volume delete
   ```

## Alternative: Custom Tempest Plugin

For VMstore-specific testing, you can create a custom Tempest plugin that validates:
- The `/cinder/host/refresh` API calls
- Snapshot ID filtering behavior
- NFS mount point handling

