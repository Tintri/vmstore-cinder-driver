# VMstore Cinder Driver — Testing Plan

## Short answer on DevStack

DevStack is **not** required for levels 1–3. The driver can be fully exercised in
isolation, in-process, and in containers. DevStack is reserved for level 4 (release
sign-off with a real VMstore appliance).

---

## Testing stack

```
Level 4  DevStack + real VMstore appliance      ← release gate only
Level 3  Docker Compose: Cinder + mock VMstore  ← QA regression suite
Level 2  In-process Cinder functional tests     ← pre-merge gate
Level 1  Unit tests (mock everything)           ← inner loop, every commit
```

Levels 1–3 are fully portable and require no VMstore hardware.

---

## Level 1 — Unit tests

**What they cover:** logic of each method in isolation. `vmstore.snapshots`, `vmstore.clones`,
`vmstore.virtual_disk`, and the execution engine (`_execute`, `_collect`, `_check_error`)
are all mocked at the boundary.

**Location:** `../cinder/cinder/tests/unit/volume/drivers/vmstore/`

**Run command (from `../cinder`):**

```bash
# Install deps once
pip install -r requirements.txt -r test-requirements.txt
pip install -e .

# Run only the vmstore tests
stestr run cinder.tests.unit.volume.drivers.vmstore

# Or via tox
tox -e py3 -- cinder/tests/unit/volume/drivers/vmstore
```

**Current known gaps to fix before handoff:**

| File | Line | Issue |
|---|---|---|
| `test_nfs.py` | 92 | `assertEqual('3.0.3', ...)` — hardcoded version, now fails (current: 3.0.10). Change to `assertEqual(vmstore_nfs.VmstoreNfsDriver.VERSION, ...)` or use a constant. |
| `test_nfs.py` | all | `_get_virtual_disk_with_retry`, `_wait_for_snapshot` backoff paths not tested |
| `test_api.py` | — | `_next_link` not tested; paginated GET with >2 pages not tested |
| `test_utils.py` | — | Keystone fallback path (catalog miss → parse `auth_url`) not tested |

**Coverage target:** ≥ 85 % line coverage on `nfs.py`, `api.py`, `utils.py`.

```bash
# Run with coverage
coverage run -m stestr run cinder.tests.unit.volume.drivers.vmstore
coverage report --include="*/vmstore/*" --omit="*/tests/*"
```

---

## Level 2 — In-process Cinder functional tests

Cinder ships a functional test framework (`cinder/tests/functional/`) that runs the
Cinder API and Volume services **in-process** with an in-memory SQLite database. No
daemons, no message queue, no Nova, no Keystone, no VMstore.

This is the right level for testing the full Cinder volume lifecycle (create →
attach → snapshot → clone → delete) through the real Cinder API, with the VMstore
driver loaded but the VMstore REST API mocked at the HTTP level using `responses` or
`unittest.mock`.

**What a VMstore functional test looks like:**

```python
# cinder/tests/functional/test_vmstore_driver.py

import json
from unittest import mock

from cinder.tests.functional import functional_helpers as helpers


class VmstoreDriverFunctionalTest(helpers.ApiSampleTestCase):

    def setUp(self):
        super().setUp()
        # Patch requests.Session so no HTTP calls leave the process
        self.mock_session = mock.patch('requests.Session').start()
        self._wire_vmstore_responses()

    def _wire_vmstore_responses(self):
        """Configure the mock session to return realistic VMstore responses."""
        session = self.mock_session.return_value
        appliance = [{'uuid': {'uuid': 'aaaa-bbbb-cccc'}}]
        vd = [{'vmName': 'vol', 'vmUuid': {'uuid': 'vd-uuid'},
               'instanceUuid': 'inst-uuid'}]

        def fake_request(method, url, **kwargs):
            r = mock.Mock()
            r.ok = True
            r.status_code = 200
            r.cookies = {}
            r.request = mock.Mock()
            r.request.method = method.upper()
            if 'appliance' in url:
                r.content = json.dumps({'items': appliance}).encode()
            elif 'virtualDisk' in url:
                r.content = json.dumps({'items': vd}).encode()
            elif 'snapshot' in url and method == 'GET':
                r.content = json.dumps({'items': []}).encode()
            elif 'snapshot' in url and method == 'POST':
                r.status_code = 201
                r.content = json.dumps({'items': ['snap-uuid']}).encode()
            elif 'clone' in url:
                r.content = json.dumps({}).encode()
            elif 'refresh' in url:
                r.content = json.dumps({}).encode()
            else:
                r.content = b''
            return r

        session.request.side_effect = fake_request

    def test_create_and_delete_volume(self):
        vol = self.api.post_volume({'volume': {
            'size': 1,
            'availability_zone': 'nova'
        }})
        self.assertEqual('available', self._wait_for_state(vol['id'], 'available'))
        self.api.delete_volume(vol['id'])
        self._wait_for_deletion(vol['id'])
```

**Run command (from `../cinder`):**

```bash
tox -e functional -- cinder/tests/functional/test_vmstore_driver.py
```

---

## Level 3 — Containerised integration (Docker Compose)

Runs real Cinder processes against a mock VMstore REST API server. NFS operations are
stubbed via a bind-mounted temp directory so no kernel NFS support is needed.

### Architecture

```
┌──────────────┐    HTTP     ┌─────────────────────┐
│ cinder-api   │────────────▶│ mysql               │
│ cinder-vol   │             └─────────────────────┘
└──────┬───────┘    AMQP     ┌─────────────────────┐
       │───────────────────▶│ rabbitmq             │
       │             └─────────────────────┘
       │  REST       ┌─────────────────────┐
       └────────────▶│ vmstore-mock        │  ← Flask app
                     │ :8080               │
                     └─────────────────────┘
```

### Mock VMstore REST API

A minimal Flask application that returns realistic VMstore responses. Store as
`test/mock-vmstore/app.py` in the driver repo:

```python
"""Minimal VMstore REST API mock for integration testing."""
import uuid as _uuid
from flask import Flask, jsonify, request

app = Flask(__name__)

APPLIANCE_UUID = str(_uuid.uuid4())
snapshots = {}
disks = {}


@app.post('/api/v310/session/login')
def login():
    from flask import make_response
    r = make_response(jsonify({}))
    r.set_cookie('JSESSIONID', 'test-session-token')
    return r


@app.get('/api/v310/appliance')
def appliance():
    return jsonify({'items': [{'uuid': {'uuid': APPLIANCE_UUID}}]})


@app.get('/api/v310/virtualDisk')
def virtual_disk():
    vol_uuid = request.args.get('uuid', '')
    if vol_uuid in disks:
        return jsonify({'items': [disks[vol_uuid]]})
    return jsonify({'items': []})


@app.get('/api/v310/snapshot')
def list_snapshots():
    contain = request.args.get('contain', '')
    matches = [s for s in snapshots.values()
               if contain in s.get('description', '')
               or contain in s.get('vmName', '')]
    return jsonify({'items': matches})


@app.post('/api/v310/cinder/snapshot')
def create_snapshot():
    snap_id = str(_uuid.uuid4())
    body = request.get_json()
    snapshots[snap_id] = {
        'uuid': {'uuid': snap_id},
        'description': body.get('description', ''),
        'vmName': body.get('vmName', ''),
    }
    vm_name = body.get('vmName', 'unknown')
    if vm_name not in disks:
        disks[vm_name] = {
            'vmName': vm_name,
            'vmUuid': {'uuid': str(_uuid.uuid4())},
            'instanceUuid': str(_uuid.uuid4()),
        }
    return jsonify({'items': [snap_id]}), 201


@app.delete('/api/v310/snapshot/<snap_uuid>')
def delete_snapshot(snap_uuid):
    snapshots.pop(snap_uuid, None)
    return '', 204


@app.post('/api/v310/cinder/clone')
def create_clone():
    return jsonify({}), 201


@app.post('/api/v310/cinder/host/refresh')
def refresh():
    return jsonify({}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
```

### `docker-compose.yml`

```yaml
version: '3.9'

services:
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: cinder
      MYSQL_DATABASE: cinder
      MYSQL_USER: cinder
      MYSQL_PASSWORD: cinder
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
      interval: 5s
      retries: 10

  rabbitmq:
    image: rabbitmq:3-management
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "check_running"]
      interval: 5s
      retries: 10

  vmstore-mock:
    build:
      context: test/mock-vmstore
    ports:
      - "8080:8080"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/api/v310/appliance"]
      interval: 3s
      retries: 10

  cinder-api:
    build:
      context: ../cinder
    depends_on:
      mysql:
        condition: service_healthy
      rabbitmq:
        condition: service_healthy
    environment:
      CINDER_DB_URL: mysql+pymysql://cinder:cinder@mysql/cinder
      CINDER_MQ_URL: amqp://guest:guest@rabbitmq:5672/
    command: cinder-api --config-file /etc/cinder/cinder.conf
    volumes:
      - ./test/cinder.conf:/etc/cinder/cinder.conf:ro
      - nfs-share:/var/lib/cinder/mnt

  cinder-volume:
    build:
      context: ../cinder
    depends_on:
      cinder-api:
        condition: service_started
      vmstore-mock:
        condition: service_healthy
    command: cinder-volume --config-file /etc/cinder/cinder.conf
    volumes:
      - ./test/cinder.conf:/etc/cinder/cinder.conf:ro
      - nfs-share:/var/lib/cinder/mnt
      # NFS share simulated as a bind mount — no kernel NFS needed
      - nfs-share:/mnt/vmstore-share

volumes:
  nfs-share:
```

### `test/cinder.conf` (no-auth mode, no Keystone)

```ini
[DEFAULT]
auth_strategy = noauth
transport_url = amqp://guest:guest@rabbitmq:5672/
enabled_backends = vmstore
default_volume_type = vmstore
state_path = /var/lib/cinder

[database]
connection = mysql+pymysql://cinder:cinder@mysql/cinder

[vmstore]
volume_driver = cinder.volume.drivers.vmstore.nfs.VmstoreNfsDriver
nas_host = vmstore-mock
nas_share_path = /mnt/vmstore-share
nfs_mount_options = vers=3
vmstore_user = admin
vmstore_password = admin
vmstore_rest_address = vmstore-mock
vmstore_rest_port = 8080
vmstore_rest_protocol = http
volume_backend_name = vmstore
vmstore_qcow2_volumes = False
```

### Running the suite

```bash
# Start the stack
docker compose up -d

# Wait for healthy
docker compose ps

# Run the integration test script
docker compose exec cinder-api python /tests/integration_test.py

# Tear down
docker compose down -v
```

The integration test script uses the Cinder API directly via `cinder` CLI or the
Python `cinderclient`:

```python
# test/integration_test.py
from cinderclient import client as cc

c = cc.Client('3', auth_url='noauth', token='admin',
              endpoint_override='http://cinder-api:8776/v3')

# Create volume
vol = c.volumes.create(size=1, name='test-vol', volume_type='vmstore')
wait_for(vol, 'available')

# Create snapshot
snap = c.volume_snapshots.create(vol.id, name='test-snap')
wait_for(snap, 'available')

# Clone from snapshot
clone = c.volumes.create(size=1, name='test-clone',
                          snapshot_id=snap.id)
wait_for(clone, 'available')

# Delete in reverse
c.volumes.delete(clone.id);      wait_for_gone(clone.id)
c.volume_snapshots.delete(snap.id); wait_for_gone(snap.id)
c.volumes.delete(vol.id);        wait_for_gone(vol.id)

print('All assertions passed.')
```

---

## Level 4 — DevStack with real VMstore

Reserved for release gate. Use when:
- A new VMstore firmware version is released
- Signing off a driver release candidate
- Validating NFS mount options against a real appliance

### Minimum host requirements

| Resource | Minimum |
|---|---|
| OS | Ubuntu 22.04 LTS |
| RAM | 16 GB |
| Disk | 100 GB |
| CPU | 4 cores |
| Network | Routable to VMstore management and data IPs |

### `local.conf`

```ini
[[local|localrc]]
HOST_IP=<your-host-ip>
ADMIN_PASSWORD=secret
DATABASE_PASSWORD=secret
RABBIT_PASSWORD=secret
SERVICE_PASSWORD=secret

# Minimal stack — no Nova/Neutron needed for Cinder validation
disable_all_services
enable_service cinder c-api c-vol c-sch
enable_service mysql rabbit

CINDER_ENABLED_BACKENDS=vmstore
CINDER_BRANCH=master

[[post-config|/etc/cinder/cinder.conf]]
[vmstore]
volume_driver = cinder.volume.drivers.vmstore.nfs.VmstoreNfsDriver
nas_host = <VMstoreDataIP>
nas_share_path = /tintri/cinder
nfs_mount_options = vers=3
vmstore_user = admin
vmstore_password = <password>
vmstore_rest_address = <VMstoreAdminIP>
volume_backend_name = vmstore
vmstore_qcow2_volumes = False
```

```bash
git clone https://opendev.org/openstack/devstack
cd devstack
cp <path-to-above>/local.conf .
./stack.sh          # ~45 min first run
```

After stacking, copy the driver files and restart the volume service:

```bash
cp -r vmstore-cinder-driver/* \
    /opt/stack/cinder/cinder/volume/drivers/vmstore/
sudo systemctl restart devstack@c-vol
```

---

## CI pipeline recommendation

```
On every commit:
  └── Level 1: stestr (unit)              ~2 min   ← block merge on failure

On every PR:
  ├── Level 1: stestr (unit)              ~2 min
  └── Level 2: functional (in-process)    ~5 min   ← block merge on failure

Nightly:
  └── Level 3: Docker Compose             ~15 min  ← alert on failure

On release candidate:
  └── Level 4: DevStack + VMstore lab     ~90 min  ← human sign-off required
```

### GitHub Actions — levels 1 + 2

```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]

jobs:
  unit-and-functional:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          path: vmstore-cinder-driver

      - uses: actions/checkout@v4
        with:
          repository: openstack/cinder
          path: cinder

      - name: Install driver into cinder tree
        run: |
          cp -r vmstore-cinder-driver/*.py \
            cinder/cinder/volume/drivers/vmstore/

      - name: Install dependencies
        working-directory: cinder
        run: pip install -r requirements.txt -r test-requirements.txt -e .

      - name: Unit tests
        working-directory: cinder
        run: stestr run cinder.tests.unit.volume.drivers.vmstore

      - name: Functional tests
        working-directory: cinder
        run: stestr --test-path=./cinder/tests/functional \
               run cinder.tests.functional.test_vmstore_driver
```

---

## Known test debt

| Area | Gap | Priority |
|---|---|---|
| `test_nfs.py:92` | Hardcoded version `'3.0.3'` fails now | **Fix immediately** |
| `api.py _next_link` | Multi-page pagination not tested at Level 1 | High |
| `_get_virtual_disk_with_retry` | Exponential backoff timing not asserted | Medium |
| `refresh_hypervisor` | Hostname auto-discovery fallback path untested | Medium |
| `create_cloned_volume` | Temp Clone Directory cleanup on rename failure | Medium |
| `utils.py` | Keystone session failure → `auth_url` parse fallback | Low |
| Level 3 | No test for concurrent volume operations | Low |
