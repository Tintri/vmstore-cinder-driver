# VMstore Cinder Driver - Performance Testing Plan

## Summary of Optimizations Implemented

### Phase 1: Lock Granularity Refinement ✅
- **Removed** backend-wide lock from `initialize_connection` (pure read operation)
- **Changed** to volume-level locks in `create_cloned_volume` (locks only source volume)
- **Changed** to snapshot-level locks in `create_volume_from_snapshot` and `delete_snapshot`
- **Configurable** via `vmstore_use_volume_locks` (default: True)

### Phase 2: Polling Loop Optimization ✅
- **Replaced** inefficient tight loops with exponential backoff
- **Added** `_wait_for_snapshot()` helper with configurable timeout
- **Added** `_get_virtual_disk_with_retry()` helper
- **Reduced** default snapshot poll timeout from 30s to 10s
- **Added** proper `time.sleep()` in all polling loops

### Phase 3: Async Operations ✅
- **Made** `refresh_hypervisor()` support async mode (default: enabled)
- **Configurable** via `vmstore_async_hypervisor_refresh` (default: True)
- **Changed** clone operations to use non-blocking refresh

### Phase 4: REST API Optimization ✅
- **Optimized** snapshot lookup to extract UUID from creation response
- **Reduced** redundant API calls in clone operations
- **Added** VM UUID filtering to snapshot queries

### Phase 5: Configuration Options ✅
- `vmstore_snapshot_poll_timeout`: 10s (was 30s)
- `vmstore_snapshot_poll_initial_delay`: 0.5s
- `vmstore_virtual_disk_retries`: 3
- `vmstore_async_hypervisor_refresh`: True
- `vmstore_use_volume_locks`: True

---

## Testing Infrastructure Requirements

### Minimal Testing Setup (No Full OpenStack Needed)

#### Option 1: Unit Testing with Mocks
**Infrastructure**: Just your laptop/workstation
**Requirements**:
- Python 3.8+
- Mock/unittest libraries
- No VMstore appliance needed

**What can be tested**:
- ✅ Lock key generation logic
- ✅ Exponential backoff behavior
- ✅ VMstore API call patterns
- ✅ Error handling paths
- ✅ Configuration validation

#### Option 2: Integration Testing with DevStack
**Infrastructure**: Single VM or bare metal server
**Requirements**:
- Ubuntu 22.04 LTS (recommended) or Rocky Linux 9
- 8GB RAM minimum, 16GB recommended
- 4 CPU cores
- 60GB disk space
- VMstore appliance (can be lab/test instance)

**What can be tested**:
- ✅ Full Cinder workflow
- ✅ Volume creation/deletion
- ✅ Snapshot operations
- ✅ Clone operations
- ✅ Lock behavior under load
- ⚠️ Limited concurrency (single host)

#### Option 3: Full Scale Testing with Real OpenStack
**Infrastructure**: Multi-node OpenStack cluster
**Requirements**:
- OpenStack deployment (Yoga, Zed, or Antelope release)
- 3+ controller nodes
- 2+ volume nodes (Cinder services)
- VMstore appliance
- Load testing tools (Rally, Tempest)

**What can be tested**:
- ✅ Everything
- ✅ 128 concurrent operations
- ✅ Real-world performance metrics
- ✅ High availability scenarios

---

## Testing Strategy

### Level 1: Unit Tests (1-2 days)

#### Test Environment Setup
```bash
cd /home/freddy/work/tintri/vmstore-cinder-driver

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install test dependencies
pip install pytest pytest-mock pytest-cov mock
```

#### Create Unit Tests
Create file: `test_vmstore_nfs_performance.py`

```python
"""Unit tests for VMstore NFS driver performance optimizations."""

import time
import unittest
from unittest import mock

# Mock the Cinder imports
import sys
sys.modules['cinder'] = mock.MagicMock()
sys.modules['cinder.volume'] = mock.MagicMock()
sys.modules['cinder.volume.drivers'] = mock.MagicMock()
sys.modules['oslo_config'] = mock.MagicMock()
sys.modules['oslo_log'] = mock.MagicMock()
sys.modules['oslo_utils'] = mock.MagicMock()
sys.modules['oslo_concurrency'] = mock.MagicMock()
sys.modules['os_brick'] = mock.MagicMock()
sys.modules['os_brick.remotefs'] = mock.MagicMock()

# Now import your driver
from nfs import VmstoreNfsDriver


class TestLockKeyGeneration(unittest.TestCase):
    """Test volume and snapshot lock key generation."""
    
    @mock.patch('nfs.processutils')
    def setUp(self, mock_processutils):
        """Set up test driver instance."""
        self.config = mock.MagicMock()
        self.config.vmstore_use_volume_locks = True
        self.driver = VmstoreNfsDriver()
        self.driver.configuration = self.config
        self.driver.vmstore = mock.MagicMock()
        self.driver.vmstore.lock = 'backend-uuid-123'
    
    def test_volume_specific_lock_key(self):
        """Test volume-specific lock key is different for different volumes."""
        volume_id_1 = 'vol-123'
        volume_id_2 = 'vol-456'
        
        lock_1 = self.driver._get_volume_lock_key(volume_id_1)
        lock_2 = self.driver._get_volume_lock_key(volume_id_2)
        
        self.assertNotEqual(lock_1, lock_2)
        self.assertIn('volume', lock_1)
        self.assertIn(volume_id_1, lock_1)
        self.assertIn('backend-uuid-123', lock_1)
    
    def test_backend_wide_lock_fallback(self):
        """Test fallback to backend-wide lock when disabled."""
        self.config.vmstore_use_volume_locks = False
        
        lock = self.driver._get_volume_lock_key('any-volume')
        
        self.assertEqual(lock, 'backend-uuid-123')


class TestExponentialBackoff(unittest.TestCase):
    """Test exponential backoff in polling loops."""
    
    @mock.patch('nfs.processutils')
    @mock.patch('time.sleep')
    def setUp(self, mock_sleep, mock_processutils):
        """Set up test driver instance."""
        self.mock_sleep = mock_sleep
        self.config = mock.MagicMock()
        self.config.vmstore_snapshot_poll_timeout = 10
        self.config.vmstore_snapshot_poll_initial_delay = 0.5
        self.driver = VmstoreNfsDriver()
        self.driver.configuration = self.config
        self.driver.vmstore = mock.MagicMock()
    
    def test_snapshot_polling_uses_exponential_backoff(self):
        """Test that polling uses exponential backoff."""
        # Mock: snapshot not found in first 2 calls, found in 3rd
        self.driver.vmstore.snapshots.list.side_effect = [
            [],  # First attempt
            [],  # Second attempt
            [{'description': 'test-snap', 'uuid': {'uuid': 'snap-123'}}]  # Found
        ]
        
        result = self.driver._wait_for_snapshot('test-snap')
        
        self.assertEqual(result, 'snap-123')
        # Should have slept with increasing delays
        self.assertEqual(self.mock_sleep.call_count, 2)
        # First sleep: 0.5s, second sleep: 1.0s (exponential)
        calls = [call[0][0] for call in self.mock_sleep.call_args_list]
        self.assertEqual(calls[0], 0.5)
        self.assertEqual(calls[1], 1.0)
    
    def test_snapshot_polling_timeout(self):
        """Test that polling respects timeout."""
        # Mock: snapshot never found
        self.driver.vmstore.snapshots.list.return_value = []
        
        result = self.driver._wait_for_snapshot('test-snap', timeout=2)
        
        self.assertIsNone(result)


class TestAsyncHypervisorRefresh(unittest.TestCase):
    """Test async hypervisor refresh behavior."""
    
    @mock.patch('nfs.processutils')
    def setUp(self, mock_processutils):
        """Set up test driver instance."""
        self.config = mock.MagicMock()
        self.config.vmstore_async_hypervisor_refresh = True
        self.config.vmstore_refresh_openstack_region = 'RegionOne'
        self.config.safe_get.return_value = 'controller.local'
        self.driver = VmstoreNfsDriver()
        self.driver.configuration = self.config
        self.driver.vmstore = mock.MagicMock()
        self.driver.nas_path = '/tintri/cinder'
    
    def test_async_refresh_does_not_block(self):
        """Test async refresh returns immediately without waiting."""
        volume = {'name': 'vol-1', 'name_id': 'vol-uuid-1'}
        
        # Should not call virtual_disk.get in async mode
        self.driver.refresh_hypervisor(volume, block=False)
        
        self.driver.vmstore.cinder_refresh.create.assert_called_once()
        self.driver.vmstore.virtual_disk.get.assert_not_called()
    
    def test_blocking_refresh_waits_for_vd(self):
        """Test blocking refresh waits for virtual disk."""
        volume = {'name': 'vol-1', 'name_id': 'vol-uuid-1'}
        self.driver._get_virtual_disk_with_retry = mock.MagicMock(
            return_value=[{'vmName': 'vol-1'}]
        )
        
        self.driver.refresh_hypervisor(volume, block=True)
        
        self.driver._get_virtual_disk_with_retry.assert_called_once()


if __name__ == '__main__':
    unittest.main()
```

#### Run Unit Tests
```bash
# Run tests with coverage
pytest test_vmstore_nfs_performance.py -v --cov=nfs --cov-report=html

# View coverage report
open htmlcov/index.html
```

**Expected Results**:
- All tests pass
- >80% code coverage on new methods
- Confirms lock key generation logic works
- Confirms exponential backoff behavior
- Confirms async/sync modes work correctly

---

### Level 2: DevStack Integration Testing (3-5 days)

#### Infrastructure Setup

**Deploy DevStack on Ubuntu 22.04**:
```bash
# On a fresh Ubuntu 22.04 VM (8GB RAM, 4 cores, 60GB disk)
sudo useradd -s /bin/bash -d /opt/stack -m stack
echo "stack ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/stack
sudo su - stack

# Clone DevStack
git clone https://opendev.org/openstack/devstack
cd devstack

# Create local.conf
cat > local.conf << 'EOF'
[[local|localrc]]
ADMIN_PASSWORD=secret
DATABASE_PASSWORD=$ADMIN_PASSWORD
RABBIT_PASSWORD=$ADMIN_PASSWORD
SERVICE_PASSWORD=$ADMIN_PASSWORD

# Enable Cinder
enable_service c-api c-sch c-vol

# Cinder config for VMstore
CINDER_ENABLED_BACKENDS=vmstore
CINDER_DEFAULT_VOLUME_TYPE=vmstore

# VMstore configuration
[[post-config|$CINDER_CONF]]
[vmstore]
volume_driver = cinder.volume.drivers.vmstore.nfs.VmstoreNfsDriver
volume_backend_name = vmstore
nas_host = <VMSTORE_DATA_IP>
nas_share_path = /tintri/cinder
nfs_mount_options = vers=3
vmstore_user = admin
vmstore_password = <VMSTORE_PASSWORD>
vmstore_rest_address = <VMSTORE_MGMT_IP>
vmstore_qcow2_volumes = False

# Performance tuning (enable new features)
vmstore_async_hypervisor_refresh = True
vmstore_use_volume_locks = True
vmstore_snapshot_poll_timeout = 10
vmstore_snapshot_poll_initial_delay = 0.5
EOF

# Run DevStack
./stack.sh
```

**Install VMstore Driver**:
```bash
# After DevStack is running
sudo mkdir -p /opt/stack/cinder/cinder/volume/drivers/vmstore
sudo cp /home/freddy/work/tintri/vmstore-cinder-driver/*.py \
    /opt/stack/cinder/cinder/volume/drivers/vmstore/

# Restart Cinder services
sudo systemctl restart devstack@c-vol
sudo systemctl restart devstack@c-sch
sudo systemctl restart devstack@c-api
```

#### Functional Tests

**Test 1: Basic Volume Operations**
```bash
source /opt/stack/devstack/openrc admin admin

# Create volume type
openstack volume type create vmstore
openstack volume type set --property volume_backend_name=vmstore vmstore

# Test volume creation
openstack volume create --size 1 --type vmstore test-vol-1
openstack volume show test-vol-1

# Test volume deletion
openstack volume delete test-vol-1
```

**Test 2: Snapshot Operations**
```bash
# Create volume and snapshot
openstack volume create --size 1 --type vmstore source-vol
openstack volume snapshot create --volume source-vol snap-1

# Verify snapshot created
openstack volume snapshot show snap-1

# Delete
openstack volume snapshot delete snap-1
openstack volume delete source-vol
```

**Test 3: Clone Operations (Critical Performance Test)**
```bash
# Create source volume
openstack volume create --size 5 --type vmstore source-for-clones

# Create multiple clones (sequential test)
for i in {1..10}; do
    echo "Creating clone $i at $(date +%T)"
    time openstack volume create --source source-for-clones \
        --size 5 --type vmstore clone-$i
done

# Check all clones
openstack volume list | grep clone

# Check Cinder logs for lock behavior
sudo journalctl -u devstack@c-vol --since "5 minutes ago" | grep -i "coordinate\|lock"
```

**Expected Results**:
- Clone operations complete in < 2 minutes each (vs 3-11 minutes before)
- No "No valid backend was found" errors
- Logs show volume-specific locks, not backend-wide locks
- Async refresh messages in logs

**Test 4: Concurrent Clone Stress Test**
```bash
# Create source volumes
for i in {1..5}; do
    openstack volume create --size 1 --type vmstore source-$i
done

# Clone from multiple sources in parallel
for i in {1..5}; do
    for j in {1..3}; do
        openstack volume create --source source-$i \
            --size 1 --type vmstore clone-$i-$j &
    done
done

# Wait for all background jobs
wait

# Count successful clones
openstack volume list --status available | grep clone | wc -l
# Should be 15 (5 sources × 3 clones)
```

**Expected Results**:
- All 15 clones succeed
- Operations run concurrently (check timestamps in logs)
- Total time < 5 minutes (vs 25+ minutes before)

---

### Level 3: Performance Benchmarking (1 week)

#### Infrastructure: Production-Like OpenStack

**Requirements**:
- 3 controller nodes (HA)
- 2+ Cinder volume nodes
- VMstore appliance
- Rally benchmarking tool

#### Rally Benchmark Scenarios

**Install Rally**:
```bash
pip install rally-openstack
rally db create
```

**Create Rally Scenario: `vmstore_clone_benchmark.yaml`**
```yaml
---
VmstoreNfsDriver.create_clone_and_delete:
  -
    args:
      size: 5
      volume_type: vmstore
    runner:
      type: "constant"
      times: 50  # 50 clone operations
      concurrency: 10  # 10 concurrent workers
    context:
      users:
        tenants: 2
        users_per_tenant: 3
      quotas:
        cinder:
          volumes: -1
          snapshots: -1
    sla:
      max_seconds_per_iteration: 90  # Clone should complete in 90s
      failure_rate:
        max: 0  # No failures allowed
```

**Run Benchmark**:
```bash
rally task start vmstore_clone_benchmark.yaml

# View results
rally task report --out benchmark_report.html
```

**Key Metrics to Track**:
1. **Throughput**: Clones/minute
2. **Latency**: Average clone time
3. **P95/P99 Latency**: Worst-case clone time
4. **Error Rate**: % of failed operations
5. **Lock Contention**: Time spent waiting for locks

---

## Validation Without Full Deployment

### Quick Validation Tests (No OpenStack)

#### 1. Configuration Syntax Check
```bash
python3 -c "
import sys
sys.path.insert(0, '/home/freddy/work/tintri/vmstore-cinder-driver')
# This will fail on imports but validates syntax
try:
    import nfs
    import options
    import api
except ImportError:
    print('✅ Syntax valid (imports failed as expected without Cinder)')
except SyntaxError as e:
    print(f'❌ Syntax error: {e}')
    sys.exit(1)
"
```

#### 2. Static Code Analysis
```bash
# Install tools
pip install pylint flake8 mypy

# Run linting
pylint nfs.py options.py api.py --disable=import-error

# Run type checking
mypy nfs.py --ignore-missing-imports
```

#### 3. Mock API Response Testing

Create `test_vmstore_api_mock.py`:
```python
"""Test VMstore API interactions with mock responses."""

import json
import time
from unittest import mock
import requests_mock

# Test actual API client behavior
from api import VmstoreProxy, VmstoreRequest


def test_snapshot_creation_returns_uuid():
    """Test that we correctly extract UUID from snapshot creation response."""
    
    with requests_mock.Mocker() as m:
        # Mock VMstore API responses
        m.post('https://vmstore.example.com:443/api/v310/session/login',
               cookies={'JSESSIONID': 'test-session-123'})
        
        # Mock snapshot creation - returns UUID in response
        m.post('https://vmstore.example.com:443/api/v310/cinder/snapshot',
               json={'items': ['snap-uuid-456']},
               status_code=201)
        
        config = mock.MagicMock()
        config.vmstore_rest_protocol = 'https'
        config.vmstore_rest_address = 'vmstore.example.com'
        config.vmstore_rest_port = 443
        config.vmstore_user = 'admin'
        config.vmstore_password = 'password'
        config.vmstore_rest_retry_count = 5
        config.vmstore_refresh_retry_count = 1
        config.vmstore_rest_backoff_factor = 1
        config.vmstore_rest_connect_timeout = 30
        config.vmstore_rest_read_timeout = 300
        config.driver_ssl_cert_verify = False
        
        proxy = VmstoreProxy('nfs', 'test-backend', config)
        
        # Test snapshot creation
        payload = {
            'typeId': 'com.tintri.api.rest.v310.dto.domain.beans.cinder.CinderSnapshotSpec',
            'vmName': 'test-vm',
            'description': 'test-snapshot'
        }
        
        start = time.time()
        result = proxy.post('cinder/snapshot', payload)
        duration = time.time() - start
        
        # Should get UUID directly from response
        assert result == ['snap-uuid-456']
        # Should be fast (< 1 second with no polling)
        assert duration < 1.0
        
        print(f"✅ Snapshot creation test passed (duration: {duration:.3f}s)")


if __name__ == '__main__':
    test_snapshot_creation_returns_uuid()
```

Run:
```bash
pip install requests-mock
python test_vmstore_api_mock.py
```

---

## Monitoring During Testing

### Key Metrics to Watch

#### 1. Cinder Service Logs
```bash
# Watch for lock coordination
tail -f /var/log/cinder/cinder-volume.log | grep -i "coordinate\|lock"

# Watch for performance improvements
tail -f /var/log/cinder/cinder-volume.log | grep -i "snapshot.*found\|clone.*created"
```

#### 2. Thread Pool Utilization
```bash
# Check oslo.service workers (should be < 50% utilized now)
ps aux | grep cinder-volume | wc -l

# Check thread states
sudo lsof -p $(pgrep cinder-volume) | wc -l
```

#### 3. VMstore API Performance
```bash
# Monitor API call latency (in driver logs)
grep "response time" /var/log/cinder/cinder-volume.log | tail -20
```

#### 4. RabbitMQ Queue Depth
```bash
# Should stay low now
sudo rabbitmqctl list_queues | grep cinder-volume
```

---

## Success Criteria

### Before Optimization (Baseline)
- ❌ API-to-Host delay: up to 25 minutes
- ❌ Clone operation: 3-11 minutes
- ❌ Concurrent clones: 1 (serialized)
- ❌ Worker thread utilization: 100% blocked
- ❌ Error rate: High (NotFound errors)

### After Optimization (Target)
- ✅ API-to-Host delay: < 30 seconds
- ✅ Clone operation: 30-90 seconds
- ✅ Concurrent clones: 20-50+ (volume-level locks)
- ✅ Worker thread utilization: 20-30% active
- ✅ Error rate: Near zero

---

## Rollback Plan

If issues arise:

1. **Disable volume-level locks** (fallback to backend-wide):
   ```ini
   [vmstore]
   vmstore_use_volume_locks = False
   ```

2. **Disable async refresh** (use blocking mode):
   ```ini
   [vmstore]
   vmstore_async_hypervisor_refresh = False
   ```

3. **Increase timeouts** (if polling fails):
   ```ini
   [vmstore]
   vmstore_snapshot_poll_timeout = 30
   vmstore_virtual_disk_retries = 5
   ```

4. **Full rollback**:
   ```bash
   git checkout main  # or previous stable branch
   # Redeploy old driver code
   sudo systemctl restart devstack@c-vol
   ```

---

## Next Steps

1. **Week 1**: Run unit tests (Level 1)
2. **Week 2**: Deploy DevStack and run functional tests (Level 2)
3. **Week 3**: Conduct stress tests on DevStack
4. **Week 4**: Deploy to staging/production and run Rally benchmarks (Level 3)
5. **Week 5**: Monitor production metrics and tune parameters

**Recommended Starting Point**: Begin with Level 1 (Unit Tests) to validate logic, then proceed to DevStack for integration testing.
