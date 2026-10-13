# VMstore Cinder Driver — Plan for v3.0.10

---

## Bugs (by severity)

### HIGH

**B1 `api.py:150,229` — TypeError when `causeDetails` is missing from error response**
`'live VM is still present' in content.get('causeDetails')` raises `TypeError: argument of type 'NoneType' is not iterable` when `causeDetails` is absent. Same at line 229. Any API error without that field crashes and masks the real error.
```python
# Fix both occurrences:
content.get('causeDetails') or ''
```

**B2 `nfs.py:139` — AttributeError on startup if `nfs_mount_options` is not in cinder.conf**
`self.mount_options = self.configuration.safe_get('nfs_mount_options')` returns `None` when unset, then `None.split(',')` crashes at line 142.
```python
self.mount_options = self.configuration.safe_get('nfs_mount_options') or ''
```

**B3 `nfs.py:242–248` — Two problems in `do_setup`**
1. Infinite loop — no max retry count if the appliance is permanently unreachable
2. On the very first failure, `self.vmstore` is still `None`, so `self.vmstore.delay(retries)` raises `AttributeError`

**B4 `nfs.py:801–807, 950–956` — Temp clone directory not cleaned up on `os.rename` failure**
If `os.rename` fails (permissions, cross-device link), `temp_clone_dir` is left orphaned on the NFS share forever. On the `create_cloned_volume` path, the temporary VMstore snapshot with `DELETE_ON_ZERO_CLONE_REFERENCES` is also leaked.

**B11 `api.py:35–64` — `message=` kwarg to `VmstoreException` is silently dropped**
`VmstoreException(code='NotFound', message='...')` formats the final string using `causeDetails`, which defaults to `'No details'`. The `message` kwarg is stored but never used in the output string. Every `raise VmstoreException(code=..., message=...)` in `nfs.py` produces `No details (source: CinderDriver, ...)` in logs.

**B12 `nfs.py:1000` — `refresh=True` flag ignored in `get_volume_stats`**
Cinder calls `get_volume_stats(refresh=True)` when it needs guaranteed fresh data. The VMstore implementation ignores `refresh` and only checks cache age.
```python
# Fix condition at line 1000:
if not self._stats_cache or refresh or cache_period == 0 or cache_age >= cache_period:
```

---

### MEDIUM

**B5 `nfs.py:520–548` — `check_encryption_provider` re-implements `volume_utils.check_encryption_provider` with divergence**
The VMstore driver defines its own version using `db.volume_encryption_metadata_get(context, volume.id)`. The parent `RemoteFSDriver._do_create_volume` calls `volume_utils.check_encryption_provider` which uses the volume OVO directly. Creates a maintenance risk on future Cinder upgrades.
Fix: Remove `check_encryption_provider` and `_do_create_volume` from the VMstore driver; let `super()._do_create_volume(volume)` handle it.

**B6 `nfs.py:459` — `create_volume` override drops the parent's `@coordination.synchronized` lock**
`NfsDriver.create_volume` is decorated with `@coordination.synchronized('{self.driver_prefix}-{volume.id}')`. The VMstore override is not. Concurrent create retries on the same volume ID are unserialised.

**B7 `nfs.py:550` — `delete_volume` override drops the parent's `@coordination.synchronized` lock**
Same analysis as B6. `NfsDriver.delete_volume` is decorated; VMstore's override is not.

**B8 `api.py:149` — `return VmstoreException(content)` instead of `raise` for refresh errors**
The caller in `nfs.py` catches all exceptions but never checks whether the return value is an exception object, so refresh failures are silently swallowed.
```python
# Change:
return VmstoreException(content)
# To:
raise VmstoreException(content)
```

**B9 `nfs.py:585,591` — Snapshot cleanup misses migrated volumes**
`_delete_volume_snapshots` matches on `vmName == volume.name_id`, but snapshot creation stores `vd[0]['vmName']`. After `update_migrated_volume`, `name_id` diverges from the volume name so VMstore snapshots are never cleaned up on migrated volumes — snapshot leak.

---

### LOW

**B10 `api.py:28` — Circular import between `api.py` and `nfs.py`**
`api.py` imports `nfs` solely to read `nfs.VmstoreNfsDriver.VERSION` for the `Tintri-Api-Client` header.
Fix: Move `VERSION` to a standalone `version.py` constant, or pass it as a constructor argument to `VmstoreProxy`.

**B13 — Double directory scan per stats cycle**
`_get_capacity_info` and `_get_provisioned_capacity` both scan the same mount directory. `_update_volume_stats` then does a third scan to count volumes. Low overhead but redundant.

---

## API_CALL_MAPPING.md Discrepancies

Update doc.

---

## Implementation Premises

Rely on Openstack base class whenever possible (when we need real change, otherwise we can add logs but execute underlying code).
The order of the methods should be equal to the base class except for  our _locked sufixed methods which should be placed under their caller pair.
Follow Openstack cinder driver best practices.

- Method ordering (done)
- Removed Redundant overrides (should use base class directly)

### Other best-practice violations

**P1 — `do_setup` does not call `super().do_setup()`**
Skips NAS security option validation, mount.nfs binary check, and `_execute_as_root` initialisation via `set_nas_security_options`.
Fix: Call `super().do_setup(ctxt)` at the start of `do_setup`.

**P2 — `initialize_connection` drops two parent safety checks**
- Does not call `get_active_image_from_info()` — uses `volume['name']` directly, wrong if qcow2 snapshot chain is active
- Skips virtual size sanity check (anti-exploit, rejects volumes resized from inside VM)
Fix: Call `super().initialize_connection(volume, connector)` and only add the VMstore-specific `mount_point_base` field.

**P3 — `get_volume_stats` ignores `refresh` flag (see B12)**

**P4 — `do_setup` / `check_for_setup_error` infinite retry loop (see B3)**

---

## utils.py Audit

`utils.py` contains one public function: `get_keystone_hostname()`, used in `nfs.py` as a fallback when `vmstore_openstack_hostname` is not configured.

**Issues:**

**U1 — `_ensure_keystone_opts` re-registers standard `keystonemiddleware` opts**
These are already provided by keystonemiddleware which Cinder loads. Re-registering risks version divergence; the `DuplicateOptError` silently swallowed hides any mismatch.

**U2 — `_cached_hostname` is a module-level global with no invalidation**
Stale after a Keystone endpoint change or multi-region reconfiguration. Acceptable for a value that rarely changes — document the limitation.

**U3 — `get_keystone_hostname` performs a full token-fetch network call on cache miss**
Occurs inside the `do_setup` path. If Keystone is slow or unavailable, `do_setup` delays before the retry loop engages. Should add an explicit timeout.

**U5 — `getattr(CONF, 'register_opt')` pattern is obfuscation**
Used to "avoid genopts pattern detection." Better: use keystonemiddleware's already-registered opts directly without re-registration.

---

## Architecture Refactor Plan

### 1. Transport layer — proxy dispatch + request engine (A+B) · **IN PROGRESS**

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


## Prioritised Implementation Order

1. **B1, B2, B3, B4, B8, B11, B12** — correctness bugs that surface under load or misconfiguration
2. **Method reordering** — low risk, high maintainability payoff
3. **Remove redundant overrides** (`_do_create_volume`)
4. **Update `docs/API_CALL_MAPPING.md`** — D1–D5
5. **Enable Multi-Attach** — one-line pool stat change + testing

__INFO__: 5 will be set from a configuration setting for easily testing and validation.
