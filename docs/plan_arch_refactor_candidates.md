# Architecture Refactor Plan

Merged findings from two independent reviews:
- **Review 1** (our session, 2026-06-21): candidates A–F
- **Review 2** (Architecture Review 01 PDF, 2026-06-21): candidates PDF-1–PDF-4

Candidates D (dead `vmstore_get_vd_timeout` option) is already fixed.
Candidates A+B are in progress (this plan's first item).

---

## Candidates

### 1. Transport layer — proxy dispatch + request engine (A+B) · **DONE** **3.0.10**

**Sources:** Review 1 candidates A and B.

**Problem (A):** `VmstoreProxy.__getattr__` synthesises `get`, `post`, `delete`, and `put`
as `VmstoreRequest` objects at call time. The proxy has no navigable interface; tests
cannot mock a specific HTTP verb without intercepting `__getattr__`. The only reason
`api.py` imported `nfs.py` was to read `VmstoreNfsDriver.VERSION` for the session
header — a circular import for one string.

**Problem (B):** `VmstoreRequest` conflates five concerns — retry loop, HTTP execution,
auth refresh (401 hook), pagination (recursive hook), and error translation — in one
object with no seams. The `requests` response-hook callbacks recurse back into
`self.request()` for pagination, making it impossible to test any concern in isolation.
The `cinder/host/refresh` path was special-cased by string match in three separate
spots.

**Solution:**
- Replace `__getattr__` with explicit `get / post / delete / put` methods on
  `VmstoreProxy`, each delegating to a private `_execute` method.
- Delete `VmstoreRequest`. Move its concerns into focused private methods on
  `VmstoreProxy`: `_execute` (retry loop), `_attempt` (single attempt + 401
  re-auth), `_collect` (explicit pagination loop — no hooks, no recursion),
  `_send_raw` (raw HTTP, no logic), `_check_error` (error translation), `_auth`
  (re-authentication).
- `VmstoreCinderRefresh.create()` overrides base `create()` to call
  `self.proxy._execute_strict(...)` — a dedicated method that raises immediately
  on any error and uses the `refresh_retries` budget. No string checks anywhere.
- Break circular import: `VmstoreProxy.__init__` receives `client_version` as a
  parameter; `nfs.py` passes `'Tintri-Cinder-Driver-%s' % self.VERSION` at
  instantiation. No `version.py` needed.
- `update_lock()` calls `self._attempt('get', 'appliance')` directly (single
  attempt) rather than `self.get(...)` to avoid infinite recursion: `_execute`
  calls `update_host()` on retry, which calls `update_lock()`.

**Locality gain:** Pagination is one explicit `while` loop in `_collect`.
Auth refresh is one method. Retry budget is the loop bound in `_execute`.

**Test gain:** `proxy.get` / `proxy.post` are real callables — `mock.patch.object`
works without `__getattr__` interceptors. Pagination tested by injecting a
two-page mock response into `_collect`. Auth refresh tested through `_attempt`
alone.

---

### 2. Clone pipeline collapse (PDF-1, supersedes Review 1 candidate F) · **STRONG**  **3.0.10**

**Sources:** PDF candidate "Collapse the clone pipeline" (STRONG, PORTS & ADAPTERS).
Supersedes Review 1 candidate F (duplicate Temp Clone Directory rename).

**Problem:** `create_volume_from_snapshot` and `create_cloned_volume` differ only in
how they locate the source (VMstore Snapshot UUID vs Virtual Disk), but share five
implementation steps: payload building, Temp Clone Directory rename, provider location
update, hypervisor refresh, and resize sequencing. These five steps are written out
twice in full, including error handling. Review 1 identified just the rename
duplication; the PDF correctly identifies the entire pipeline as the shallow module.

**Solution:** Deepen into a single `VMstore Clone Operation` module that accepts a
source adapter — either `VMstoreSnapshotSource` or `VirtualDiskSource` — and owns the
full lifecycle behind one seam. The source adapter resolves its own snapshot UUID;
everything downstream (payload, rename, refresh, resize) is written once.

**Locality gain:** Temp Clone Directory rename logic lives once. Resize sequencing
lives once.

**Test gain:** Clone lifecycle tested through one seam. Each source adapter tested
independently against a mock clone operation.

---

### 3. NFS Share inventory module (PDF-2) · **STRONG**  **3.0.10**

**Sources:** PDF candidate "Deepen the NFS Share inventory module" (STRONG, IN-PROCESS).
Not identified in Review 1.

**Problem:** Three methods each independently walk the NFS Share and re-encode the
`volume-*` filter rule:
- `_get_provisioned_capacity` — scans for provisioned bytes
- `_get_capacity_info` — scans for provisioned bytes (again) + filesystem totals
- `_update_volume_stats` — scans for volume count (again)

ADR-0001 documents the decision to use `os.stat` over `du` — but the rule "count only
`volume-*` files at the share root" is currently repeated in each method.

**Solution:** Extract a single `_scan_share(mount_point)` method (or small dataclass)
that performs one directory walk and returns `(total_size, available, provisioned_bytes,
volume_count)`. All three callers consume the result. The `volume-*` filter rule exists
once.

**Locality gain:** Filter rule lives once. A future change (e.g. snapshot visibility
rules) touches one place.

**Test gain:** Filesystem mock setup is written once, for one method. Stats chain
tested without duplicate filesystem setup across three test cases.

---

### 4. VMstore Snapshot Catalog (PDF-3 / Review 1 candidate C) · **WORTH EXPLORING**

**Sources:** PDF candidate "Pull snapshot lifecycle behind one seam" (WORTH EXPLORING,
PORTS & ADAPTERS). Review 1 candidate C (payload assembly leaking into orchestration).

**Problem:** VMstore Snapshot semantics leak across the driver: `nfs.py` knows `typeId`,
`vmTintriUuid`, `instanceId`, `deletionPolicy`, and `snapshotCreator` field names.
Delete exceptions (`live VM is still present`), response-shape parsing in
`create_cloned_volume`, and name-filter semantics (`contain=` query param) all live at
call sites. Review 1 identified this as payload assembly leaking; the PDF frames it as
a missing snapshot-lifecycle module that `VmstoreNfsDriver` should not need to know
about.

**Solution:** Introduce a `VMstoreSnapshotCatalog` that owns:
- `create_for_volume(file_path, vm_name, description, vm_uuid, instance_id,
  deletion_policy)` — builds payload, hides `typeId`
- `find_by_description(name, vm_uuid=None)` — owns `contain=` filter semantics
- `delete(uuid)` — owns the active-clone exception rule

`nfs.py` speaks domain terms only; `typeId` never appears outside `api.py`.

**Locality gain:** Lookup rules concentrate. Payload schema lives in one place.

**Test gain:** Snapshot semantics tested against a mock HTTP session, not through
the full driver. `nfs.py` test stubs become simple mock catalogs rather than REST
payload dicts.

---

### 5. Keystone Hostname Resolver (Review 1 candidate E) · **WORTH EXPLORING**

**Sources:** Review 1 candidate E. Not in PDF.

**Problem:** `utils.py` holds two module-level mutable globals (`_cached_hostname`,
`_keystone_opts_registered`) that persist across test runs in the same process. Tests
cannot independently exercise cache-hit, cache-miss, Keystone failure, and config
fallback paths without resetting module globals.

**Solution:** Replace the module-level functions with a `KeystoneHostnameResolver`
class that takes the configuration object in `__init__`. The driver constructs one
instance during `do_setup`; tests construct their own with a mock configuration and
get a clean cache each time.

**Locality gain:** All hostname resolution state is instance-scoped. Cache TTL or
invalidation logic has one home.

**Test gain:** Each path (cache hit, Keystone catalog, auth_url fallback, failure)
independently testable by constructing a fresh resolver.

---

### 6. VMstore Layout Adapter (PDF-4) · **SPECULATIVE**

**Sources:** PDF candidate "Separate filesystem layout from coordination policy"
(SPECULATIVE, LOCAL-SUBSTITUTABLE).

**Problem:** The seam between VMstore Appliance addressing, NFS Share path layout, and
coordination lock rules is blurry. Lock keys are built in the driver
(`_get_volume_lock_key`, `_get_snapshot_lock_key`), the `/tintri/` prefix is trimmed
in three methods, share strings are assembled in `_get_share_path`, Temp Clone
Directory paths are computed inline, and the backend lock hash lives in the proxy.

**Solution:** If path or lock rules keep changing (e.g. multi-share support, lock
scope changes), deepen them into a `VMstoreLayout` module. Apply the deletion test
first: would extracting these rules concentrate complexity or just move it? Only
proceed if a second layout adapter becomes real.

**Locality gain (if done):** Path formatting rules live once. Lock scope policy lives
once.

---

## Implementation Order

| # | Candidate | Source | Status |
|---|---|---|---|
| 0 | Dead `vmstore_get_vd_timeout` option (D) | Review 1 | **Done** |
| 1 | Transport layer: proxy dispatch + request engine (A+B) | Review 1 | **In progress** |
| 2 | NFS Share inventory module (PDF-2) | PDF | Next |
| 3 | Clone pipeline collapse (PDF-1 / F) | PDF + Review 1 | After PDF-2 |
| 4 | VMstore Snapshot Catalog (PDF-3 / C) | PDF + Review 1 | After clone collapse |
| 5 | Keystone Hostname Resolver (E) | Review 1 | Independent, any time |
| 6 | VMstore Layout Adapter (PDF-4) | PDF | Speculative — apply deletion test first |

---

## Feature Gap Analysis vs NetApp ONTAP NFS Driver

Updated 2026-11-26 (first version 2026-06-21). Tracking: VME-8 is split into one story per row (VME-35 to VME-46); stories that need new VMstore endpoints depend on VME-39.

**Why the gaps look the way they do.** The VMstore Cinder API is three VM-centric endpoints: `POST /v310/cinder/snapshot` (needs the VM name, Tintri UUID and instance ID of the file's VM), `/cinder/clone` (clones a snapshot into a new directory, keeping the source file name) and `/cinder/host/refresh`. NetApp's driver maps Cinder onto file-level ONTAP calls (`clone-create` from a source path to a destination path, `file-copy-start` between volumes), so it stays a thin mapping. Most of our driver's fragile logic adapts Cinder's file model to VMstore's VM model: virtual-disk lookups and hypervisor refreshes (VME-34, VMTS-279), temporary clone directories and renames (VMTS-225), and the rename risk behind VMTS-279. Candidates 2 and 4 above are the driver half of the fix; file-level endpoints (VME-39) are the array half.

| Feature | Cinder method(s) | Status | VMstore API requirement | Story | Notes |
|---|---|---|---|---|---|
| **Extend attached volume** | `extend_volume`, `online_extend_support` | **DONE** (3.0.11, kept in 3.0.12) | None | VME-10 | raw grown by the driver, qcow2 resized by Nova (or offline by the driver when every instance is shut off); attach-time grow as a safety net. ADR-0004. |
| **Format integrity (raw only, correct records)** | `copy_image_to_volume`, clone paths, `extend_volume`, `migrate_volume` | **3.0.12**; enforcement next | None | VME-29, VME-33, VME-37 | `vmstore_qcow2_volumes` deprecated; image volumes and clones record raw; extend trusts the file over a stale record. Next: every new volume raw whatever the option says (ADR-0005 step 3). |
| **Volume migration and retype, same share** | `migrate_volume` | **3.0.12** | None | VME-29 (PR #30) | Same VMstore share: nothing copied, Cinder only changes the host (as NetApp does for the same pool). |
| **Volume migration and retype, other share or array** | `migrate_volume`, `retype` | **NEEDS_VMSTORE_API** | File copy or move to another share or array | VME-40 | 3.0.12 refuses to byte-copy a qcow2 file and lets a raw file go to Cinder's generic copy only if the destination creates raw volumes. |
| **Multi-attach** | pool stat `multiattach: True` | **ACHIEVABLE** | None | VME-35 | NFS is multi-mount; check every attach-path step with a second live attachment. |
| **Image cache** | `clone_image` | **ACHIEVABLE** | None (existing clone call) | VME-36 | NetApp's biggest speed win: volumes from an image are array clones of a cached copy. |
| **Generic volume groups and group snapshots** | `create_group`, `delete_group`, `update_group`, `create_group_snapshot`, `delete_group_snapshot`, `create_group_from_src` | ACHIEVABLE (non-atomic) / NEEDS_VMSTORE_API (crash-consistent) | Atomic snapshot of several files | VME-38 | Group CRUD needs no array call; group snapshot as a loop of single snapshots until the atomic endpoint exists. Replaces the deprecated consistency group API. |
| **Revert to snapshot** | `revert_to_snapshot` | **NEEDS_VMSTORE_API** (clean) | Restore in place, or clone to a destination file path | VME-41 | Today Cinder falls back to a byte copy. Clone plus rename over the live file is the VMTS-279 risk; until the endpoint exists, refuse qcow2 reverts clearly. |
| **Manage and unmanage** | `manage_existing`, `manage_existing_get_size`, `unmanage` (and snapshot variants) | ACHIEVABLE | None (association refresh) | VME-42 | Adopting a file renames it into `volume-<id>`: same rename question as revert until the VMstore team confirms. |
| **QoS** | `QoS_support`, QoS on create, clone, retype | **TO CONFIRM** | Per-file QoS, or a volume to service-group mapping | VME-43 | VMstore exposes QoS on service groups; confirm whether per-file QoS works without the VM association. |
| **Volume replication** | `failover_host`, `enable_replication`, `disable_replication`, `failover_replication` | **NEEDS_VMSTORE_API** | Replication relationship, failover and promote endpoints | VME-44 | Largest gap; the existing replication path for cross-backend volume from snapshot is a starting point, not a relationship. |
| **Active/active HA** | `SUPPORTS_ACTIVE_ACTIVE`, `failover`, `failover_completed` | After replication | Same as replication | VME-45 | Also needs a review of locks and in-process caches (for example the 3.0.12 refresh cooldown is per process). |
| **Third-party CI** | (upstream requirement) | **NOT STARTED** | None | VME-46 | Without it, none of the above appears in the OpenStack Cinder support matrix. The in-repo lab suites and GitHub CI are building blocks. |

**VMstore endpoints requested (VME-39), and what each removes from the driver**

| Endpoint | Unlocks | Removes from the driver |
|---|---|---|
| Snapshot a file by path, no VM association | reliable snapshots | virtual-disk lookup and refresh dance |
| Clone a snapshot to a destination file path | clean clones, revert, image cache | temporary clone directories and renames |
| Copy or move a file to another share or array | migration and retype everywhere | refusal of cross-share qcow2 moves |
| Restore a file in place | revert | clone plus rename |
| Per-file QoS | QoS | |
| Replication relationship, failover, promote | replication, active/active | |
| Atomic multi-file snapshot | crash-consistent group snapshots | |
| Deterministic NotFound and association in every release | correct deletes and lookups | NotFound double-checks, refresh cooldown |
