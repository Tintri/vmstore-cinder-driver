# Collapse the per-refresh share scans into one pass

[ADR 0001](0001-replace-du-with-stat-for-provisioned-capacity.md) replaced `du` with a
per-file `os.stat()` walk of the share root. That removed the subprocess and the full-tree
traversal, but left the walk itself duplicated: a single stats refresh walked the share root
three times.

The duplication follows from the parent chain. `RemoteFSDriver._update_volume_stats` calls
`_get_capacity_info` once per mounted share; `NfsDriver._update_volume_stats` then calls
`_get_provisioned_capacity`; and our own `_update_volume_stats` walked the directory a third
time to count volumes. The first two computed the *same* number — the sum of `st_size` over
`volume-*` files — each issuing one stat round trip per volume file. On a share with N volumes
that is 3 directory walks and 2N stat round trips over NFS, every refresh (~60s by default,
subject to `vmstore_stats_cache_period`).

`_scan_share()` now performs a single `os.scandir()` pass returning both metrics the chain
needs, `(volume_count, provisioned_bytes)`, and memoises the result in `self._share_scan`.
`_update_volume_stats` clears the memo before calling `super()`, so each refresh scans exactly
once and `_get_provisioned_capacity` and the volume count read the memo. Measured on a
50-volume share: 3 walks and 103 stat calls before, 1 walk and 50 after.

`_get_capacity_info` deliberately calls `_scan_share()` directly and never reads the memo. The
parent class also calls it from `_find_share` and `_is_share_eligible` on the volume-create
path, where its third return value feeds the oversubscription check; serving a cached
`total_allocated` there would let concurrent creates over-commit the share. `_get_provisioned_capacity`
is safe to serve from the memo because it has exactly one call site — `NfsDriver._update_volume_stats`,
immediately after the `_get_capacity_info` loop, with no greenthread yield in between.

`os.scandir` is used over `os.listdir` for the lazy iteration, not for speed: on Linux
`DirEntry.stat()` still costs one syscall per entry, so the win here comes from doing one pass
instead of three, not from the API swap.

## Considered Options

- **Swap `os.listdir` for `os.scandir` in place**: The two are not interchangeable —
  `scandir` yields `DirEntry` objects, not strings — and on POSIX it saves no syscall when
  `st_size` is needed. It addresses the walk, not the duplication, so the stat round trips
  (the dominant cost on NFS) would remain.
- **A time-based TTL cache on the scan**: Simpler to reason about across call paths, but it
  would also serve the create path, staling the oversubscription check by up to the TTL.
  Rejected: capacity checks must see current allocation.
- **Fold the volume count into `_get_capacity_info`'s return tuple**: Would change the
  signature of a method the parent class calls and unpacks as a 3-tuple. Rejected as a break
  with the upstream contract.
