# Call extend_volume outside the coordination lock after clone completes

When creating a volume from a snapshot or clone with a larger requested size, `extend_volume` must be called after the VMstore clone operation to resize the file from the source size to the requested size. The clone API inherits the source file size; the resize is a separate local operation (`qemu-img resize` via the base NFS driver).

The coordination lock protects VMstore API calls (snapshot lookup, clone creation, file rename) from racing on the same snapshot or volume. `extend_volume` is a local filesystem operation with no VMstore API dependency and does not need the lock. We call it in the outer (unlocked) method — after the locked clone operation returns but before returning `provider_location` to Cinder — so the lock is held only for VMstore operations while Cinder still does not consider the volume ready until the extend completes.

Calling extend inside the lock (the earlier approach) was safe but unnecessarily widened the lock window, blocking concurrent operations on the same snapshot or volume during what could be a multi-second file resize.

## Consequences

If `extend_volume` fails after a successful clone, the cloned file is left at the source size and Cinder marks the volume as `error`. There is no automatic rollback of the clone. This is consistent with other Cinder NFS driver failure modes and is acceptable — an admin can retry or delete the errored volume.
