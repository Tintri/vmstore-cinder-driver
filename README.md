# VMstore Openstack cinder driver (NFS)

## Compatibility matrix

|Vmstore version|CSI driver version|
|---|---|
|>=6.0.1.1|>=3.0.8|

## Prerequisites

Install NFS client

```bash
apt install nfs-common
```

## Installation

Clone Vmstore driver for the desired version:

```bash
git clone -b <branch> https://github.com/Tintri/vmstore-cinder-driver.git
```

Create vmstore folder and copy the files

```bash
mkdir -p /usr/lib/python3/dist-packages/cinder/volume/drivers/vmstore
cp -r vmstore-cinder-driver/* /usr/lib/python3/dist-packages/cinder/volume/drivers/vmstore/
```

Note: the exact cinder location might differ depending on your installation.

Configure `/etc/cinder/cinder.conf` to use the Vmstore cinder driver.
Example configuration:

```conf
[DEFAULT]
default_volume_type = vmstore
enabled_backends = vmstore

[vmstore]
volume_driver = cinder.volume.drivers.vmstore.nfs.VmstoreNfsDriver
nas_host = <VMstoreDataIP>
nas_share_path = <VMstoreSharePath>  # example: /tintri/cinder
nfs_mount_options = vers=3
vmstore_user = <VMstore_UserName>
vmstore_password = <VMstore_Password>
vmstore_rest_address = <VMstoreAdminIP, or FQDN>
volume_backend_name = vmstore
vmstore_qcow2_volumes = False
```

### List of configuration Parameters

|Configuration Option|Type|Default Value|Required|Description|
|-|-|-|-|-|
|`vmstore_rest_address`|String|-|yes|IP address or hostname for management communication with Vmstore REST API interface.|
|`vmstore_rest_protocol`|String|`https`|no|Vmstore RESTful API interface protocol.|
|`vmstore_rest_port`|Integer|`443`|no|Vmstore RESTful API interface port.|
|`nas_host`|String|-|yes|Vmstore data IP for volume mount, IO operations.|
|`vmstore_user`|String|`admin`|yes|Username to connect to Vmstore REST API interface.|
|`vmstore_password`|String|-|yes|User password to connect to Vmstore RESTful API interface.|
|`vmstore_rest_connect_timeout`|Float|`30`|no|Specifies the time limit (in seconds) to establish connection to Vmstore REST API interface.|
|`vmstore_rest_read_timeout`|Float|`300`|no|Specifies the time limit (in seconds) for Vmstore REST API interface to send a response.|
|`vmstore_rest_backoff_factor`|Float|`1`|no|Specifies the backoff factor to apply between connection attempts to Vmstore REST API interface.|
|`vmstore_rest_retry_count`|Int|`5`|no|Specifies the number of times to repeat Vmstore REST API calls in case of connection errors or retriable errors.|
|`vmstore_refresh_retry_count`|Int|`1`|no|Specifies the number of times to repeat Vmstore RESTful API call to cinder/host/refresh in case of connection errors or Vmstore appliance retriable errors.|
|`vmstore_qcow2_volumes`|Boolean|`False`|no|Use qcow2 volumes.|
|`vmstore_mount_point_base`|String|`$state_path/mnt`|no|Base directory containing NFS share mount points.|
|`vmstore_sparsed_volumes`|Boolean|`True`|no|Defines whether the volumes need to be thin-provisioned.|
|`vmstore_dataset_description`|String|-|no|Human-readable description for the backend.|
|`vmstore_refresh_openstack_region`|String|``|no|OpenStack region for Vmstore hypervisor refresh call.|
|`vmstore_openstack_hostname`|String|-|no|OpenStack controller hostname or IP. Used for VMstore hypervisor refresh. If not set, attempts to resolve from Keystone config.|
|`vmstore_stats_cache_period`|Int|59|no|Period in seconds for caching volume statistics. Stats will be refreshed only if the cache is older than this value. Set to 0 to disable caching.|
|`vmstore_get_vd_timeout`|Int|`8`|no|Maximum time in seconds to wait for a single virtual disk lookup attempt.|
|`vmstore_virtual_disk_retries`|Int|`3`|no|Number of retries for virtual disk lookup before failing. Each retry triggers a hypervisor refresh and uses exponential backoff.|
|`vmstore_snapshot_poll_timeout`|Int|`30`|no|Maximum total time in seconds to poll for a snapshot to appear in the VMstore index after creation.|
|`vmstore_snapshot_poll_initial_delay`|Float|`0.5`|no|Initial delay in seconds between snapshot poll attempts. Doubles on each retry up to `vmstore_snapshot_max_delay`.|
|`vmstore_snapshot_max_delay`|Float|`12.0`|no|Cap in seconds on the exponential backoff delay for snapshot polling.|
|`vmstore_use_volume_locks`|Boolean|`True`|no|When True, coordination locks are scoped per volume, allowing concurrent operations on different volumes. Set to False for legacy backend-wide locking.|

### Performance Tuning

After a volume or snapshot is created, VMstore may take a moment to populate its internal index. The driver uses exponential backoff and retries to wait for this—controlled by the options below. These defaults are calibrated for a local-network deployment; high-latency or bursty environments benefit from higher values.

#### Virtual disk rediscovery

When a create or clone operation completes, the driver queries VMstore for the resulting virtual disk. If it is not yet visible, the driver fires a hypervisor refresh and retries with exponential backoff:

- `vmstore_virtual_disk_retries` — how many times to retry before failing (default: 3)
- `vmstore_get_vd_timeout` — per-attempt lookup timeout in seconds (default: 8)

#### Snapshot polling

After requesting a snapshot, the driver polls until it appears. Backoff starts at `vmstore_snapshot_poll_initial_delay` and doubles each cycle, capped at `vmstore_snapshot_max_delay`, until `vmstore_snapshot_poll_timeout` is reached:

- `vmstore_snapshot_poll_timeout` — total polling window in seconds (default: 30)
- `vmstore_snapshot_poll_initial_delay` — first poll delay in seconds (default: 0.5)
- `vmstore_snapshot_max_delay` — maximum single sleep in seconds (default: 12.0)

#### Concurrency

`vmstore_use_volume_locks` (default True) scopes coordination locks per volume so that operations on different volumes can proceed in parallel. Set to False only if you need the legacy single-lock behaviour for compatibility with older deployments.

#### Environment profiles

| Environment | Suggested overrides |
|---|---|
| Standard LAN deployment | Defaults are appropriate |
| High-latency or WAN-linked controller (e.g. PF9 multi-region) | `vmstore_snapshot_poll_timeout = 120`, `vmstore_virtual_disk_retries = 5`, `vmstore_rest_retry_count = 10` |
| Large deployment with many concurrent volumes | Keep `vmstore_use_volume_locks = True`; raise `vmstore_stats_cache_period` to 120–300 |

### Restart Openstack Cinder service

```bash
sudo systemctl restart openstack-cinder-volume.service
```
