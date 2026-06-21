# VMstore API Call Mapping: nfs.py → api.py → HTTP Request

This document traces all API calls (GET, POST, DELETE) from the NFS driver through the API layer to the actual HTTP requests sent to the VMstore appliance.

## Call Flow Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            OpenStack Cinder                              │
│                                   ↓                                       │
│                         VmstoreNfsDriver (nfs.py)                        │
│                    (Volume Operations & NFS Management)                  │
└─────────────────────────────────────────────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                       Collection Classes (api.py)                        │
│  ┌────────────────┬──────────────────┬──────────────────────────────┐  │
│  │ VmstoreClones  │ VmstoreSnapshots │ VmstoreVirtualDisks          │  │
│  │ VmstoreCinder  │ VmstoreAppliance │                              │  │
│  │ Refresh        │                  │                              │  │
│  └────────────────┴──────────────────┴──────────────────────────────┘  │
│                        ↓ get(), list(), create(), delete()              │
└─────────────────────────────────────────────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                        VmstoreProxy (api.py)                             │
│                    (__getattr__ → VmstoreRequest)                        │
└─────────────────────────────────────────────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                      VmstoreRequest (api.py)                             │
│  • Retry logic with exponential backoff                                 │
│  • Authentication & session management                                   │
│  • Pagination handling (GET)                                             │
│  • Error handling & translation                                          │
└─────────────────────────────────────────────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────────┐
│              requests.Session.request(method, url, **kwargs)             │
│                    (Python Requests Library)                             │
└─────────────────────────────────────────────────────────────────────────┘
                                   ↓
                 ┌──────────────────────────────┐
                 │   VMstore Appliance REST API  │
                 │   https://host:port/api/v310  │
                 └──────────────────────────────┘
```

### Method Flow by HTTP Verb

**GET**: `nfs.py` → Collection.`get()`/`list()` → Proxy.`get()` → Request.`request('get')` → HTTP GET

**POST**: `nfs.py` → Collection.`create()` → Proxy.`post()` → Request.`request('post')` → HTTP POST

**DELETE**: `nfs.py` → Collection.`delete()` → Proxy.`delete()` → Request.`request('delete')` → HTTP DELETE

---

## Detailed API Call Mappings

All line numbers reference the actual code locations in the files.

### GET Requests

#### 1. Snapshot List for Polling (Line 198)

### nfs.py:198
```python
snapshots = self.vmstore.snapshots.list(filters)
# filters = {'contain': snapshot_name, 'vmUuid': vm_uuid}
```

### api.py: VmstoreSnapshots.list() (Lines 390-410)
```python
def list(self, filters=None):
    path = self.root  # 'snapshot'
    if filters and isinstance(filters, dict):
        query_params = []
        for key, value in filters.items():
            encoded_value = urlparse.quote_plus(str(value))
            query_params.append('%s=%s' % (key, encoded_value))
        if query_params:
            query_string = '&'.join(query_params)
            path = '%s?%s' % (self.root, query_string)
    return self.proxy.get(path)
```

### VmstoreProxy.__getattr__() (Line 477)
```python
def __getattr__(self, name):  # name = 'get'
    return VmstoreRequest(self, name)
```

### VmstoreRequest.request() (Lines 173-189)
```python
def request(self, method, path, payload):
    url = self.proxy.url(path)
    return self.proxy.session.request(method, url, **kwargs)
```

### VmstoreProxy.url() (Lines 507-512)
```python
def url(self, path=None):
    netloc = '%s:%d/api/v310' % (self.host, self.port)
    components = (self.scheme, netloc, path, None, None)
    return urlparse.urlunsplit(components)
```

### **Actual HTTP GET Request:**
```
GET https://<vmstore-host>:<port>/api/v310/snapshot?contain=<snapshot_name>&vmUuid=<vm_uuid>
```

---

#### 2. Appliance Info (Line 269)

### nfs.py:269
```python
appliance = self.vmstore.appliance.get(None)
```

### api.py: VmstoreAppliance (Lines 425-429)
Inherits from VmstoreCollections, uses base get() method:

```python
class VmstoreAppliance(VmstoreCollections):
    def __init__(self, proxy):
        super(VmstoreAppliance, self).__init__(proxy)
        self.root = 'appliance'
```

### VmstoreCollections.get() (Lines 326-329)
```python
def get(self, payload):
    path = self.root  # 'appliance'
    return self.proxy.get(path, payload)
```

### **Actual HTTP GET Request:**
```
GET https://<vmstore-host>:<port>/api/v310/appliance
```

---

#### 3. Virtual Disk Lookup (Line 434)

### nfs.py:434
```python
vd = self.vmstore.virtual_disk.get(volume.name_id)
```

### api.py: VmstoreVirtualDisks.get() (Lines 377-379)
```python
class VmstoreVirtualDisks(VmstoreCollections):
    def __init__(self, proxy):
        self.root = 'virtualDisk'
    
    def get(self, uuid):
        path = '%s?uuid=%s' % (self.root, uuid)
        return self.proxy.get(path)
```

### **Actual HTTP GET Request:**
```
GET https://<vmstore-host>:<port>/api/v310/virtualDisk?uuid=<volume.name_id>
```

---

#### 4. List Snapshots for Volume Deletion (Line 587)

### nfs.py:587
```python
snapshots = self.vmstore.snapshots.list({'contain': volume_id})
```

### api.py: VmstoreSnapshots.list()
Same as #1 above, with different filter:

### **Actual HTTP GET Request:**
```
GET https://<vmstore-host>:<port>/api/v310/snapshot?contain=<volume_id>
```

---

#### 5. List Snapshots for Snapshot Deletion (Line 739)

### nfs.py:739
```python
snapshots = self.vmstore.snapshots.list({'contain': snapshot['name']})
```

### api.py: VmstoreSnapshots.list()
Same as #1 above, with different filter:

### **Actual HTTP GET Request:**
```
GET https://<vmstore-host>:<port>/api/v310/snapshot?contain=<snapshot_name>
```

---

## Request Execution Flow

All HTTP methods (GET, POST, DELETE) follow the same execution path:

### 1. VmstoreRequest.__call__() (Lines 92-169)
- Constructs full request info
- Implements retry logic (vmstore_rest_retry_count)
- Calls `self.request(method, path, payload)`

### 2. VmstoreRequest.request() (Lines 173-189)
- Validates method and path
- Constructs full URL via `self.proxy.url(path)`
- For POST: adds payload as JSON in request body
- For GET/DELETE: payload used for query params (GET) or in URL path (DELETE)
- Executes: `self.proxy.session.request(method, url, **kwargs)`
  - `method` = 'get', 'post', or 'delete'
  - `url` = full URL with scheme, host, port, and path
  - `kwargs` includes:
    - `hooks`: response hooks for pagination, auth refresh
    - `timeout`: (connect_timeout, read_timeout)
    - `data`: JSON-encoded payload for POST requests

### 3. Response Handling via hook() (Lines 191-266)
- Handles HTTP status codes:
  - **200 OK**: Returns data, handles pagination (GET only)
  - **201 Created**: Returns created items from response (POST)
  - **401 Unauthorized**: Refreshes auth token, retries
  - **404 Not Found**: Treats as success for DELETE operations
  - **500 Server Error**: Checks for RESOURCE_BUSY, may retry
- Pagination: Automatically follows 'next' links in response (GET only)
- Error handling: Converts errors to VmstoreException

### Method-Specific Behavior

#### GET Requests
- Payload used as query parameters (filtered via list() methods)
- Supports pagination via 'next' links in response
- Returns list of items or single object

#### POST Requests
- Payload sent as JSON in request body
- Used for create operations (snapshots, clones, refresh)
- Returns created items or operation result
- May return 201 Created status with items array

#### DELETE Requests
- Resource identifier (UUID) appended to URL path
- No request body
- 404 errors treated as success (idempotent delete)
- Used for cleanup operations

---

## URL Construction Details

### Configuration (from options.py)
```python
vmstore_rest_protocol = 'https'  # or 'http'
vmstore_rest_address = '<vmstore-ip-or-hostname>'
vmstore_rest_port = 443  # default
```

### Full URL Format
```
<scheme>://<host>:<port>/api/v310/<path>
```

Example:
```
https://vmstore.example.com:443/api/v310/snapshot?contain=my-volume-123
```

---

## Session and Authentication

### Session Headers (api.py Lines 450-457)
```python
self.headers = {
    'Content-Type': 'application/json',
    'X-XSS-Protection': '1',
    'Tintri-Api-Client': 'Tintri-Cinder-Driver-3.0.8'
}
```

### Authentication Flow (VmstoreRequest.auth(), Lines 273-285)
1. POST to `/session/login` with credentials
2. Receives JSESSIONID cookie
3. Stores token and adds to subsequent requests:
   ```python
   bearer = 'JSESSIONID=%s' % token
   self.session.headers['cookie'] = bearer
   ```

---

## Retry and Error Handling

### Retry Configuration (options.py)
```python
vmstore_rest_retry_count = 5  # default
vmstore_rest_backoff_factor = 1  # default
vmstore_rest_connect_timeout = 30  # seconds
vmstore_rest_read_timeout = 300  # seconds
```

### Retry Logic (VmstoreRequest.__call__(), Lines 92-169)
- Retries on connection errors
- Retries on VMstore retriable errors
- Uses exponential backoff: `backoff * (2 ** (attempt - 1))`
- Refreshes host info between retries

### Error Types
- **RESOURCE_NOT_FOUND**: Resource doesn't exist
- **RESOURCE_BUSY**: Resource locked, may retry
- **RESOURCE_EXIST**: Resource already exists (ignored in create)

---

---

### POST Requests

#### 6. Hypervisor Refresh (Line 412)

### nfs.py:412
```python
self.vmstore.cinder_refresh.create(payload)
# payload contains hostname, volumeFilePath, region
```

### api.py: VmstoreCinderRefresh (Lines 431-435)
Inherits from VmstoreCollections, uses base create() method:

```python
class VmstoreCinderRefresh(VmstoreCollections):
    def __init__(self, proxy):
        self.root = 'cinder/host/refresh'
```

### VmstoreCollections.create() (Lines 343-349)
```python
def create(self, payload=None):
    path = self.root  # 'cinder/host/refresh'
    return self.proxy.post(path, payload)
```

### **Actual HTTP POST Request:**
```
POST https://<vmstore-host>:<port>/api/v310/cinder/host/refresh
Body: {
    "typeId": "com.tintri.api.rest.v310.dto.domain.beans.cinder.OpenStackHostRefreshSpec",
    "hostname": "<openstack-hostname>",
    "volumeFilePath": "<volume-path>",
    "region": "<region>"
}
```

---

#### 8. Create Snapshot (Lines 718, 938)

### nfs.py:718, 938
```python
self.vmstore.snapshots.create(payload)
# payload contains file, vmName, description, vmTintriUuid, instanceId, etc.
```

### api.py: VmstoreSnapshots.create() (Lines 412-419)
**Note**: Snapshots have a custom create() that prepends 'cinder' to the path:

```python
def create(self, payload=None):
    path = posixpath.join('cinder', self.root)  # 'cinder/snapshot'
    return self.proxy.post(path, payload)
```

### **Actual HTTP POST Request:**
```
POST https://<vmstore-host>:<port>/api/v310/cinder/snapshot
Body: {
    "typeId": "com.tintri.api.rest.v310.dto.domain.beans.cinder.CinderSnapshotSpec",
    "file": "<volume-path>",
    "vmName": "<vm-name>",
    "description": "<snapshot-name>",
    "vmTintriUuid": "<vm-uuid>",
    "instanceId": "<instance-uuid>",
    "snapshotCreator": "Vmstore cinder driver",
    "deletionPolicy": "DELETE_ON_EXPIRATION" or "DELETE_ON_ZERO_CLONE_REFERENCES"
}
```

---

#### 9. Create Clone (Lines 808, 979)

### nfs.py:808, 979
```python
self.vmstore.clones.create(payload)
# payload contains tintriSnapshotUuid, destinationPaths
```

### api.py: VmstoreClones (Lines 367-371)
Inherits from VmstoreCollections, uses base create() method:

```python
class VmstoreClones(VmstoreCollections):
    def __init__(self, proxy):
        self.root = 'cinder/clone'
```

### VmstoreCollections.create() (Lines 343-349)
```python
def create(self, payload=None):
    path = self.root  # 'cinder/clone'
    return self.proxy.post(path, payload)
```

### **Actual HTTP POST Request:**
```
POST https://<vmstore-host>:<port>/api/v310/cinder/clone
Body: {
    "typeId": "com.tintri.api.rest.v310.dto.domain.beans.cinder.CinderCloneSpec",
    "tintriSnapshotUuid": "<snapshot-uuid>",
    "destinationPaths": "<clone-path>"
}
```

---

### DELETE Requests

#### 7. Delete Snapshot (Lines 595, 753)

### nfs.py:595, 753
```python
self.vmstore.snapshots.delete(snap_uuid)
```

### api.py: VmstoreSnapshots (Lines 383-389)
Inherits from VmstoreCollections, uses base delete() method:

```python
class VmstoreSnapshots(VmstoreCollections):
    def __init__(self, proxy):
        self.root = 'snapshot'
```

### VmstoreCollections.delete() (Lines 353-362)
```python
def delete(self, payload):
    path = self.path(payload)  # joins self.root with URL-encoded payload
    return self.proxy.delete(path, payload)

def path(self, name):
    quoted_name = urlparse.quote_plus(name)
    return posixpath.join(self.root, quoted_name)
```

### **Actual HTTP DELETE Request:**
```
DELETE https://<vmstore-host>:<port>/api/v310/snapshot/<snap_uuid>
```

---

## Summary Table

### GET Requests

| nfs.py Line | Collection Class | Method | API Path | Full URL |
|-------------|------------------|--------|----------|----------|
| 198 | VmstoreSnapshots | list() | `snapshot?contain=...` | `GET /api/v310/snapshot?contain=<name>` |
| 269 | VmstoreAppliance | get() | `appliance` | `GET /api/v310/appliance` |
| 434 | VmstoreVirtualDisks | get() | `virtualDisk?uuid=...` | `GET /api/v310/virtualDisk?uuid=<uuid>` |
| 587 | VmstoreSnapshots | list() | `snapshot?contain=...` | `GET /api/v310/snapshot?contain=<volume_id>` |
| 739 | VmstoreSnapshots | list() | `snapshot?contain=...` | `GET /api/v310/snapshot?contain=<snapshot_name>` |

### POST Requests

| nfs.py Line | Collection Class | Method | API Path | Full URL |
|-------------|------------------|--------|----------|----------|
| 412 | VmstoreCinderRefresh | create() | `cinder/host/refresh` | `POST /api/v310/cinder/host/refresh` |
| 718 | VmstoreSnapshots | create() | `cinder/snapshot` | `POST /api/v310/cinder/snapshot` |
| 808 | VmstoreClones | create() | `cinder/clone` | `POST /api/v310/cinder/clone` |
| 938 | VmstoreSnapshots | create() | `cinder/snapshot` | `POST /api/v310/cinder/snapshot` |
| 979 | VmstoreClones | create() | `cinder/clone` | `POST /api/v310/cinder/clone` |

### DELETE Requests

| nfs.py Line | Collection Class | Method | API Path | Full URL |
|-------------|------------------|--------|----------|----------|
| 595 | VmstoreSnapshots | delete() | `snapshot/<uuid>` | `DELETE /api/v310/snapshot/<snap_uuid>` |
| 753 | VmstoreSnapshots | delete() | `snapshot/<uuid>` | `DELETE /api/v310/snapshot/<snap_uuid>` |

### Notes:
- **No PUT/PATCH operations** are used in nfs.py
- All requests go through `VmstoreRequest` in api.py which handles retry logic, authentication, and error handling
- POST/DELETE requests follow the same execution flow as GET requests through `VmstoreRequest.request()` at line 189
