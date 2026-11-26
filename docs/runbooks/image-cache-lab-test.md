# Runbook: image cache lab test (VME-36)

Script: `test/lab/image-cache/run.sh`. Issue: [VME-36](https://ime-ddn.atlassian.net/browse/VME-36).

## What it tests

NetApp's biggest speed gain for boot volumes is making them as array clones of a cached
copy of the image instead of downloading and converting the image each time. Cinder has
this built in: with `image_volume_cache_enabled = True` in a backend, the first volume from
an image is a download, and Cinder then clones it into a cache volume (`image-<image id>`,
owned by Cinder's internal tenant). Later volumes from that image on the same cinder-volume
host are made with the driver's `create_cloned_volume`, followed by `extend_volume` when
they are larger. Our driver already has both calls, so no driver change is needed; the
script checks the path on a VMstore backend and reports, for every volume, how Cinder made
it.

**How a volume was made** is read from Cinder's cache entries (read-only query) before and
after it is created:

| Path | Meaning |
| --- | --- |
| `miss` | A new cache entry appeared: the image was downloaded into the volume, then cloned into a new cache volume. |
| `hit` | An existing entry's `last_used` moved: the volume was cloned from the cache volume. |
| `none` | No cache: downloaded (cache off), or cloned straight from the Glance image volume (see below). |

Cache entries belong to one cinder-volume host (`host@backend#pool`) unless the backend is
clustered. With two hosts serving the backend, a volume that lands on the other host is a
`miss` there; the script expects `hit` only when an entry exists for the volume's host.

| Check | What is verified |
| --- | --- |
| **C1** | First 1 GiB volume from the image: `available`, path as expected (`miss` with the cache on, `none` off), format record agrees with the file, content identical to the image (`qemu-img compare`), virtual size 1 GiB. |
| **C2** | One cache entry, whose volume is `available`, owned by the internal tenant, has a record that agrees with its file and the image's content. |
| **C3** | Second 1 GiB volume: same checks as C1, path as expected. |
| **C4** | 2 GiB volume: clone then extend; virtual size 2 GiB, content identical to the image (the extra space reads as zeros). |
| **C5** | Delete the first volume (the cache volume was cloned from it); the cache volume stays `available`; one more volume passes the C1 checks. |
| **C6** | At least one volume was a `hit`. |
| **C7** | Cleanup removed the test volumes, the cache volume, its cache entry and the image. |

**Format record.** The driver hands a file to Nova as raw when its format record (`admin_metadata['format']`) or the file says raw, otherwise as the file's own format, so a volume with no record is used in its real format (clones of qcow2 volumes have none, by design since 3.0.11). A check passes when the record is absent (`record=None`) or equals the file's format, and fails on a contradiction: `raw` on a qcow2 file, or a stale `qcow2` on a raw file.

With the cache off, the run is a **baseline**: C1, C3, C4 and C5 check the download path
(path `none`) and C2 and C6 are SKIP. The summary lists each volume's path and creation
time, so a baseline run and a cache run give the before and after.

**Glance images stored in Cinder.** If Glance keeps images as Cinder volumes (store
`cinder`) on the backend under test, and Cinder's `allowed_direct_url_schemes` includes
`cinder` (it does on the PCD lab checked on 2026-11-26), Cinder can clone the image's own
volume and never use the cache. The run detects it: right after the upload it looks for a
volume named `image-<copy id>`, and the first volume shows which path Cinder took. In that
case C2 and C6 are SKIP; set `IMAGE_STORE` to a store that is not Cinder to test the cache.

## Expected results

| Driver | Cache | C1 | C2 | C3 | C4 | C5 | C6 | C7 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3.0.12 (to be measured) | off (baseline) | PASS | SKIP | PASS | PASS | PASS | SKIP | PASS |
| 3.0.12 (to be measured) | on | PASS | PASS | PASS | PASS | PASS | PASS | PASS |

Run it with `vmstore_qcow2_volumes` both off and on: the cache sends more volumes through
the clone path, and qcow2 records on clones were part of VME-29.

## Switching the cache on (and back off)

This is a configuration change on shared hosts: agree it with the lab owners, as for the
`vmstore_qcow2_volumes` flip (`docs/runbooks/driver-swap.md`).

1. In the PCD cluster blueprint, add `image_volume_cache_enabled = True` to the backend's
   configuration (optional: `image_volume_cache_max_size_gb`, `image_volume_cache_max_count`;
   0 means no limit). PCD rewrites `cinder.conf` and restarts cinder-volume on the storage
   hosts.
2. Cinder's internal tenant (`cinder_internal_tenant_project_id` and
   `cinder_internal_tenant_user_id` in `[DEFAULT]`) must be set; the PCD lab checked on
   2026-11-26 already had both. The precheck prints `internal_project=set` or `unset` per
   host (IDs are never printed).
3. Run the script. Its precheck reads the settings on every host in `CONFIG_SSH` the way
   cinder-volume does, and stops if the hosts disagree.
4. Afterwards, remove the option in the blueprint and check with
   `test/lab/driver-swap/swap.sh status` that `cinder.conf` is back as found (PCD's
   `pf9_identifier_timestamp` lines are ignored).

## Prerequisites

- ssh keys (BatchMode) to the CLI host and the cinder-volume hosts. Admin credentials stay
  on the CLI host (`CLI_RC` is sourced there, never copied).
- On the cinder-volume host: `qemu-img` and the cinder-volume service's Python
  (`CINDER_VENV`), and the type's share mounted.
- A small qcow2 or raw image (cirros), about 20 MB of space under `/root` on the CLI host
  and the cinder-volume host for its copy.

## Run

```bash
cd test/lab/image-cache
cp lab.env.example my-lab.env     # edit hosts, type, image
./run.sh my-lab.env               # about 10 minutes
```

Exit codes: `0` every check passed (SKIP allowed), `1` a check failed, `2` precheck or setup
problem (nothing tested).

## Reading the output

- `### precheck`: one `CONFIG` line per host (`enabled`, limits, internal tenant set or not,
  `direct_url_schemes`, `clusters` of the backend's services: `None` means entries are per host), then `image cache: on|off`.
- `### private copy`: the copy's ID, and whether Glance stored it as a Cinder volume.
- Each volume: `made by: <path> (expected <path>)`, time to `available`, then
  `record= file= vsize= same_as_image= host= cluster=`.
- `### C2`: the `ENTRY` lines (cache volume, host, cluster, size, last used).
- `=== summary`: path and time per volume, then the C1 to C7 table.

## Safety and cleanup

- Objects: the private image `<PREFIX>-image`, volumes `<PREFIX>-v1` to `<PREFIX>-v4`, and
  the cache volume Cinder creates for the copy (`image-<copy id>`, recorded from the cache
  entries). The copy has its own image ID, so no other user's cache entry is touched. The
  precheck refuses to start if any `<PREFIX>-*` volume or image exists.
- Cleanup runs on any exit: test volumes (reset to `available` first if in error; only this
  run's), the cache volume (deleting it through the API also removes its cache entry), the
  image (Glance removes its own volume when its store is Cinder), and the helper directories.
- The run never writes to volume files; file checks are read-only (`qemu-img -U`).
- The script never changes configuration and never restarts a service.
- All other volumes and images are listed before and after, and any difference is reported.

### Manual cleanup after a hard interruption

On the CLI host:

```bash
source /root/admin.rc; export OS_REGION_NAME=<region>
openstack image list --private | grep vme36-image                  # note the ID, then:
openstack volume list --all-projects --name image-<copy id>         # delete (cache volume)
openstack volume list --all-projects --long | grep ' vme36-'        # delete
openstack image delete <copy id>
```

and remove `/root/vme36-lab` on the CLI host and the cinder-volume hosts. Replace `vme36`
with your `PREFIX`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `the hosts disagree on the cache` | The blueprint change reached only some hosts, or `CONFIG_SSH` lists a host serving another configuration. Check with `swap.sh status`. |
| `internal_project=unset` | Set Cinder's internal tenant; without it Cinder logs `Unable to get Cinder internal context, will not use image-volume cache` and every volume is a download. |
| `path: miss, expected hit` | A cache entry existed for the volume's host but Cinder downloaded again. Look in the cinder-volume log for `Failed to create volume from image-volume cache` (the clone failed and Cinder fell back) or an out-of-date entry. |
| `path: none, expected miss` | The cache was not used. Check the `CONFIG` lines, and whether Cinder cloned the Glance image volume instead. |
| C4 `virtual size 1073741824, expected 2 GiB` | The clone of a larger volume was not extended. |
| `image data does not match Glance's hash` | Glance could not serve `IMAGE_ID` to the CLI host (seen on the PCD lab on 2026-11-26: `openstack image save` returned no data, "checksum mismatch"). Set `IMAGE_SRC` to the host and path of Glance's own copy (PCD image library: `/var/opt/imagelibrary/data/glance/<IMAGE_ID>` on the library host); the run checks the SHA-512 before uploading. |
| `image not created: ... 413 Request Entity Too Large: nginx` | The Glance endpoint refuses the upload of the private copy (seen on the PCD lab on 2026-11-26 for a 21 MB image). Nothing is tested; raise it with whoever runs the cloud, or use a lab whose Glance accepts uploads. |
| C6 SKIP `no volume landed on a host with a cache entry` | Two or more hosts serve the backend; run again (entries persist only for this run's image, so a new run starts empty). |

## Run history

| Date | Lab | Driver | Cache | C1 | C2 | C3 | C4 | C5 | C6 | C7 | Times (miss / hit) | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
