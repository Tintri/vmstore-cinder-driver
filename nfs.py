# Copyright 2026 DDN, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""VMstore NFS Volume Driver for Cinder."""

import hashlib
import os
import re
import shutil
import time
from typing import List

from os_brick.remotefs import remotefs
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import units

from cinder import context
from cinder import coordination
from cinder import exception
from cinder.i18n import _
from cinder.image import image_utils
from cinder import interface
from cinder import objects
from cinder import utils as cinder_utils
from cinder.volume.drivers import nfs
from cinder.volume.drivers.vmstore import api
from cinder.volume.drivers.vmstore import options
from cinder.volume.drivers.vmstore import utils
from cinder.volume import volume_utils

LOG = logging.getLogger(__name__)


@interface.volumedriver
class VmstoreNfsDriver(nfs.NfsDriver):
    """Executes volume driver commands on VMstore Appliance.

    Version history:

    .. code-block:: none

        3.0-beta - Initial driver version.
        3.0.2 - Added vmstore_refresh_openstack_region parameter for
              hypervisor refresh API.
              - Added vmstore_refresh_retry_count specific for hypervisor
              refresh API.
        3.0.3 - refresh_hypervisor: poll for virtual disk after the API call
              to cinder/refresh and retry if not available.
        3.0.4 - Added logging, removed refresh_hypervisor from delete_volume
        3.0.5 - Moved all provisioning properties to pool in
              _update_volume_stats for thin provisioning.
        3.0.6 - Cache volume stats. Added filtering for all list snapshots
              operations.
        3.0.7 - Fix coordination lock held during VirtualDisk discovery.
              Extracted _wait_for_virtual_disk() helper; VD polling now
              occurs outside the lock in create_snapshot and
              create_cloned_volume. Fix infinite busy-wait loop in
              create_volume_from_snapshot (replaced with single-shot
              check). Fix undefined self.project in api.py lock key
              (now uses appliance UUID only). Add vmstore_get_vd_timeout
              config option.
        3.0.7a - Add exponential backoff with jitter to snapshot polling and
              virtual disk retrieval to reduce load on appliance and avoid
                thundering herd issues. Add more detailed logging around these
                operations to aid in troubleshooting. Add configuration options
                for tuning backoff parameters and timeouts. Ensure that locks are
                not held during backoff sleep periods to allow better concurrency.
        3.0.7b - Fix lock_key parameter in create_snapshot.   
        3.0.7c - Fix create_volume_from_snapshot to use unique clone name.
        3.0.7d - Fix BUG: wrong volume name (should use unique clone name), 
                 Fix BUG: Handle None from Virtual Disk Api Call ( _get_virtual_disk_with_retry ), 
                   logs error and raises exception VmstoreException NotFound instead of AttributeError,
                 Add Option for max delay in snapshot polling to avoid excessively long waits in case of issues,
                 Refactor: add TINTRI_PATH_PREFIX constant
        3.0.8 - Release version for April 2026
        3.0.9  - [VMS-4184]: Pass volume.id while querying virtual disk information. Also, populate
                    volumeId parameter in payload to /host/refresh API in refresh_hypervisor function.
        3.0.10 - [VMS-4243]: (PCD-4852) Call extend_volume when creating a volume from a
                    snapshot or clone and specifying a larger size for the new volume.
                 [VMS-4180]: Added _get_capacity_info and _get_provisioned_capacity overrides to avoid
                    the expensive du traversal. Uses os.stat() per volume file instead.
                 [VMS-4127] Update README and added extra documentation to repository.
                 Fix BUG: _get_provisioned_capacity was passing nas_path instead of host:path
                    to _get_mount_point_for_share, causing incorrect mount resolution.
                 Refactor: _get_virtual_disk_with_retry uses configuration vmstore_snapshot_poll_initial_delay 
                    and vmstore_get_vd_timeout for consistency
                 Refactor: Removed custom implementations of copy_image_to_volume, copy_volume_to_image,  _mount_share,
                    _ensure_share_mounted, _local_volume_dir, _do_create_volume, extend_volume methods, 
                    reducing code duplication and potential maintenance overhead.
                 Refactor: _update_volume_stats to call super() to stay in the Cinder parent stats chain. 

    """

    VERSION = '3.0.10'
    CI_WIKI_NAME = 'Vmstore_CI'

    vendor_name = 'DDN'
    product_name = 'VMstore'
    storage_protocol = 'NFS'
    driver_prefix = 'vmstore'
    driver_volume_type = 'nfs'
    TINTRI_PATH_PREFIX = '/tintri/'

    def __init__(self, execute=processutils.execute, *args, **kwargs):

        self._remotefsclient = None
        super().__init__(*args, **kwargs)
        if not self.configuration:
            code = 'ENODATA'
            message = (_('%(product_name)s %(storage_protocol)s '
                         'backend configuration not found')
                       % {'product_name': self.product_name,
                          'storage_protocol': self.storage_protocol})
            raise api.VmstoreException(code=code, message=message)

        self.configuration.append_config_values(options.VMSTORE_NFS_OPTS)
        root_helper = cinder_utils.get_root_helper()
        mount_point_base = self.configuration.vmstore_mount_point_base
        self.mount_point_base = os.path.realpath(mount_point_base)
        self.mount_options = (
            self.configuration.safe_get('nfs_mount_options') or '')
        self._mounted_shares = []

        required_mount_opts = [
            'lookupcache=pos', 'nolock', 'noacl', 'proto=tcp']
        for option in required_mount_opts:
            if option not in self.mount_options.split(','):
                if not self.mount_options:
                    self.mount_options = option
                else:
                    self.mount_options += ',%s' % option

        self._remotefsclient = remotefs.RemoteFsClient(
            self.driver_volume_type,
            root_helper, execute=execute,
            nfs_mount_point_base=self.mount_point_base,
            nfs_mount_options=self.mount_options)
        self.nas_driver = self.__class__.__name__
        self.ctxt = None
        self.backend_name = self._get_backend_name()
        self.nas_host = self.configuration.nas_host
        self.nas_path = self.configuration.nas_share_path
        self.nas_stat = None
        self.nas_share = None
        self.nas_mntpoint = None
        self.vmstore = None
        # Stats caching
        self._stats_cache = None
        self._stats_cache_timestamp = 0

    @staticmethod
    def get_driver_options():
        LOG.info('VmstoreNfsDriver get_driver_options')
        return options.VMSTORE_NFS_OPTS

    def initialize_connection(self, volume, connector):
        """Allow connection to connector and return connection info.

        NO LOCK NEEDED - This is a read-only operation on the file system.
        Multiple concurrent connections can be initialized safely.

        :param volume: volume reference
        :param connector: connector reference
        :returns: dictionary of connection information
        """
        LOG.info('Initialize volume connection for volume %(vol)s with '
                 'connector %(conn)s', {'vol': volume['name'], 'conn': connector})
        LOG.debug('Initialize volume connection for %(volume)s',
                  {'volume': volume['name']})
        volume_name = volume['name']
        volume_dir = self._local_volume_dir(volume)
        path_to_vol = os.path.join(volume_dir, volume_name)
        info = self._qemu_img_info(path_to_vol, volume_name)

        if info.file_format not in ['raw', 'qcow2']:
            msg = _('nfs volume must be a valid raw or qcow2 image.')
            raise exception.InvalidVolume(reason=msg)

        data = {
            'export': self._get_share_path(),
            'name': volume_name,
            'format': info.file_format
        }
        encryption_key_id = volume.get('encryption_key_id', None)
        data['encrypted'] = encryption_key_id is not None

        if self.mount_options:
            data['options'] = '-o %s' % self.mount_options
        info = {
            'driver_volume_type': self.driver_volume_type,
            'mount_point_base': self.mount_point_base,
            'data': data
        }
        LOG.debug('conn_info: %s', info)
        return info

    def _get_volume_lock_key(self, volume_id):
        """Generate volume-specific lock key.

        :param volume_id: Volume UUID or ID
        :returns: Lock key string for coordination
        """
        if self.configuration.vmstore_use_volume_locks:
            return f"{self.vmstore.lock}:volume:{volume_id}"
        # Fall back to backend-wide lock for compatibility
        return self.vmstore.lock

    def _get_snapshot_lock_key(self, snapshot_id):
        """Generate snapshot-specific lock key.

        :param snapshot_id: Snapshot UUID or ID
        :returns: Lock key string for coordination
        """
        if self.configuration.vmstore_use_volume_locks:
            return f"{self.vmstore.lock}:snapshot:{snapshot_id}"
        # Fall back to backend-wide lock for compatibility
        return self.vmstore.lock

    def _wait_for_snapshot(self, snapshot_name, vm_uuid=None, timeout=None):
        """Poll for snapshot with exponential backoff.

        :param snapshot_name: Name/description of snapshot to find
        :param vm_uuid: Optional VM UUID for filtering
        :param timeout: Maximum time to wait in seconds (uses config default)
        :returns: snapshot UUID or None
        """
        if timeout is None:
            timeout = self.configuration.vmstore_snapshot_poll_timeout

        max_delay = self.configuration.vmstore_snapshot_max_delay  # Cap backoff at configured max delay
        delay = self.configuration.vmstore_snapshot_poll_initial_delay
        elapsed = 0
        start_time = time.time()

        while elapsed < timeout:
            # Single API call with filtering
            filters = {'contain': snapshot_name}
            if vm_uuid:
                filters['vmUuid'] = vm_uuid

            snapshots = self.vmstore.snapshots.list(filters)

            for snap in snapshots:
                if snap.get('description') == snapshot_name:
                    LOG.debug(
                        'Found snapshot %(name)s after %(elapsed).2f sec.',
                        {'name': snapshot_name, 'elapsed': elapsed})
                    return snap['uuid']['uuid']

            # Exponential backoff with cap
            sleep_time = min(delay, max_delay)
            LOG.debug(
                'Snapshot %(name)s not found, waiting %(sleep).2f seconds '
                '(elapsed: %(elapsed).2f/%(timeout)s)',
                {'name': snapshot_name, 'sleep': sleep_time,
                 'elapsed': elapsed, 'timeout': timeout})
            time.sleep(sleep_time)
            elapsed = time.time() - start_time
            delay *= 2  # Exponential backoff

        LOG.warning('Snapshot %(name)s not found after %(timeout)s seconds', {'name': snapshot_name, 'timeout': timeout})
        return None

    def do_setup(self, ctxt) -> None:
        LOG.info('VmstoreNfsDriver do_setup for context: %s', ctxt)
        self.ctxt = ctxt
        self._validate_required_options()
        max_retries = self.configuration.vmstore_rest_retry_count
        retries = 0
        while not self._do_setup():
            retries += 1
            if retries > max_retries:
                raise exception.VolumeBackendAPIException(
                    data=_('Failed to initialize VMstore backend after '
                           '%d retries') % max_retries)
            if self.vmstore:
                self.vmstore.delay(retries)
            else:
                time.sleep(retries)
        # Mount the NFS share so _mounted_shares is populated before
        # _report_driver_status calls get_volume_stats. Without this,
        # the first stats report has zero capacity and the scheduler
        # rejects the backend until a volume create triggers mounting.
        self._ensure_shares_mounted()

    def _validate_required_options(self) -> None:
        """Validate that required configuration options are set."""
        LOG.info('VmstoreNfsDriver _validate_required_options')
        required_opts = ['vmstore_password', 'vmstore_rest_address']
        missing = []
        for opt in required_opts:
            if not getattr(self.configuration, opt, None):
                missing.append(opt)
        if missing:
            raise exception.InvalidConfigurationValue(
                option=', '.join(missing),
                value='<not set>',
                reason=_('Required VMstore configuration options are missing')
            )

    def _do_setup(self) -> bool:
        LOG.info('VmstoreNfsDriver _do_setup')
        try:
            self.vmstore = api.VmstoreProxy(
                self.driver_volume_type,
                self.backend_name,
                self.configuration,
                client_version='Tintri-Cinder-Driver-%s' % self.VERSION,
            )
        except api.VmstoreException as error:
            LOG.error('Failed to initialize RESTful API for backend '
                      '%(backend_name)s on host %(host)s: %(error)s',
                      {'backend_name': self.backend_name,
                       'host': self.host,
                       'error': error})
            return False
        return True

    def check_for_setup_error(self) -> None:
        LOG.info('Checking for setup error')
        max_retries = self.configuration.vmstore_rest_retry_count
        retries = 0
        while not self._check_for_setup_error():
            retries += 1
            if retries > max_retries:
                raise exception.VolumeBackendAPIException(
                    data=_('VMstore appliance not accessible after '
                           '%d retries') % max_retries)
            self.vmstore.delay(retries)

    def _check_for_setup_error(self):
        LOG.info('VmstoreNfsDriver _check_for_setup_error')
        appliance = self.vmstore.appliance.get(None)
        if appliance:
            return True
        return False

    def _get_backend_name(self) -> str:
        LOG.info('VmstoreNfsDriver _get_backend_name')
        backend_name = self.configuration.safe_get('volume_backend_name')
        if not backend_name:
            LOG.error('Failed to get configured volume backend name')
            backend_name = '%(product)s_%(protocol)s' % {
                'product': self.product_name,
                'protocol': self.storage_protocol
            }
        return backend_name

    def _ensure_shares_mounted(self) -> None:
        """Look for remote shares in the flags and mount them locally."""
        LOG.info('VmstoreNfsDriver _ensure_shares_mounted')
        mounted_shares: List[str] = []
        self._load_shares()

        for share in self.shares:
            try:
                self._ensure_share_mounted(share)
                mounted_shares.append(share)
            except Exception as exc:
                LOG.error('Exception during mounting %s', exc)

        self._mounted_shares = mounted_shares

        LOG.debug('Available shares %s', self._mounted_shares)

    def _load_shares(self) -> None:
        LOG.info('VmstoreNfsDriver _load_shares')
        self.shares = {}

        if all((self.configuration.nas_host,
                self.configuration.nas_share_path)):
            LOG.debug('Using nas_host and nas_share_path configuration.')

            share_address = self._get_share_path()

            if not re.match(self.SHARE_FORMAT_REGEX, share_address):
                msg = _('Share %(share)s ignored due to invalid format. '
                        'Must be of form address:/export. Please check '
                        'the nas_host and nas_share_path settings.'
                        ) % {'share': share_address}
                raise exception.InvalidConfigurationValue(msg)

            self.shares[share_address] = getattr(
                self.configuration, 'nas_mount_options', None)

        else:
            msg = 'nas_host or nas_share_path not configured.'
            LOG.error(msg)
            raise exception.InvalidConfigurationValue(msg)

        LOG.debug('shares loaded: %s', self.shares)

    def _mount_share(self, share) -> str:
        """Ensure that share is mounted on the host.

        :param share: nfs share
        :returns: mount point
        """
        LOG.info('VmstoreNfsDriver _mount_share for share: %s', share)
        return super()._mount_share(share)

    def refresh_hypervisor(self, volume):
        """Refresh VMstore hypervisor for the given volume.

        :param volume: volume reference
        """

        LOG.info('Refreshing hypervisor for volume %(vol)s', {'vol': volume.id})

        try:
            vmstore_subdir = self.nas_path.removeprefix(self.TINTRI_PATH_PREFIX)
            volume_path = os.path.join(vmstore_subdir, volume['name'])

            hostname = self.configuration.safe_get(
                'vmstore_openstack_hostname')
            if not hostname:
                hostname = utils.get_keystone_hostname()
            if not hostname:
                LOG.warning("No OpenStack hostname configured and "
                            "auto-discovery failed. Skipping refresh.")
                return

            payload = {
                'typeId': ('com.tintri.api.rest.v310.dto.domain.'
                           'beans.cinder.OpenStackHostRefreshSpec'),
                'hostname': hostname,
                'volumeFilePath': volume_path,
                'region': self.configuration.vmstore_refresh_openstack_region,
                'volumeId': volume['id'],
            }

            # Call refresh API
            self.vmstore.cinder_refresh.create(payload)
            LOG.debug('Async hypervisor refresh initiated for %s', volume.id)
            return

        except Exception as e:
            # In async mode, just log and continue
            LOG.warning("Async hypervisor refresh failed for %(vol)s: %(err)s",
                        {'vol': volume.id, 'err': e})

    def _get_virtual_disk_with_retry(self, volume):
        """Get virtual disk with exponential backoff retry
        and refresh hypervisor if not found.

        This is an improvement on previous version 3.0.7 _wait_for_virtual_disk to reduce load on appliance
        and avoid thundering herd issues.

        :param volume: volume reference
        :returns: Virtual disk info or None
        """
        max_retries = self.configuration.vmstore_virtual_disk_retries
        delay = self.configuration.vmstore_snapshot_poll_initial_delay
        max_delay = self.configuration.vmstore_get_vd_timeout
        for attempt in range(max_retries):
            vd = self.vmstore.virtual_disk.get(volume.id)
            if vd:
                LOG.debug('Found virtual disk for %(id)s on attempt %(attempt)s',
                          {'id': volume.id, 'attempt': attempt + 1})
                return vd

            if attempt < max_retries - 1:
                LOG.debug(
                    'Virtual disk for %(id)s not found, retry %(attempt)s/%(max)s '
                    'after %(delay).2f seconds',
                    {'id': volume.id, 'attempt': attempt + 1,
                     'max': max_retries, 'delay': delay})
                # Try refresh call
                LOG.info('VirtualDisk for %s not found, sleeping %d', volume.id, delay)
                self.refresh_hypervisor(volume)
                sleep_time = min(delay, max_delay)
                time.sleep(sleep_time)
                delay *= 2  # Exponential backoff

        LOG.warning(
            'Virtual disk for %(name)s not found after %(retries)s retries',
            {'name': volume['name'], 'retries': max_retries})
        return None

    def _get_provisioned_capacity(self) -> float:
        share_string = self._get_share_path()
        mount_point = self._get_mount_point_for_share(share_string)
        provisioned_bytes = 0
        if os.path.exists(mount_point):
            for filename in os.listdir(mount_point):
                if (filename.startswith('volume-') and
                        not filename.endswith('.info')):
                    filepath = os.path.join(mount_point, filename)
                    try:
                        provisioned_bytes += os.stat(filepath).st_size
                    except OSError:
                        continue
        return round(provisioned_bytes / float(units.Gi), 2)

    def _get_capacity_info(self, nfs_share: str) -> tuple[float, float, float]:
        """Calculate available space on the NFS share.

        Overrides base class to calculate provisioned capacity for thin
        provisioning support instead of actual disk usage (du).

        :param nfs_share: example 172.18.194.100:/var/nfs
        :returns: (total_size, total_available, provisioned_capacity)
        """
        mount_point = self._get_mount_point_for_share(nfs_share)

        # Get filesystem capacity using stat
        df, _ = self._execute('stat', '-f', '-c', '%S %b %a', mount_point,
                              run_as_root=self._execute_as_root)
        block_size, blocks_total, blocks_avail = map(float, df.split())
        total_available = block_size * blocks_avail
        total_size = block_size * blocks_total

        # Provisioned capacity: sum of logical sizes of volume files.
        # st_size reports logical/promised size for thin-provisioned files on
        # VMstore NFS. VMstore snapshots are appliance-internal and not visible
        # as NFS files, so volume-* at the share root is a complete inventory.
        provisioned_bytes = 0
        if os.path.exists(mount_point):
            for filename in os.listdir(mount_point):
                if (filename.startswith('volume-') and
                        not filename.endswith('.info')):
                    filepath = os.path.join(mount_point, filename)
                    try:
                        provisioned_bytes += os.stat(filepath).st_size
                    except OSError:
                        continue

        return total_size, total_available, provisioned_bytes

    def get_volume_stats(self, refresh=False) -> dict:
        """Get volume stats.

        Stats are cached based on vmstore_stats_cache_period configuration.
        """
        LOG.info('VmstoreNfsDriver get_volume_stats, refresh: %s', refresh)

        cache_period = self.configuration.vmstore_stats_cache_period
        current_time = time.time()
        cache_age = current_time - self._stats_cache_timestamp

        if (not self._stats_cache or refresh or
                cache_period == 0 or cache_age >= cache_period):
            LOG.debug('Updating volume stats: cache_age=%.2f, '
                      'cache_period=%d',
                      cache_age, cache_period)
            self._update_volume_stats()
            self._stats_cache = self._stats
            self._stats_cache_timestamp = current_time
        else:
            LOG.debug('Using cached volume stats: cache_age=%.2f, '
                      'cache_period=%d',
                      cache_age, cache_period)
            self._stats = self._stats_cache

        return self._stats

    def _update_volume_stats(self) -> None:
        LOG.info('VmstoreNfsDriver _update_volume_stats')

        # Run the standard NFS stats chain. Our overridden _get_capacity_info
        # and _get_provisioned_capacity are called here, so no du is invoked.
        super()._update_volume_stats()
        data = self._stats

        # Count total volumes — not tracked by the parent chain
        share_string = self._get_share_path()
        mount_path = self._get_mount_point_for_share(share_string)
        total_volumes = 0
        if os.path.exists(mount_path):
            for filename in os.listdir(mount_path):
                if (filename.startswith('volume-') and
                        not filename.endswith('.info')):
                    total_volumes += 1

        location_info = '%(driver)s:%(host)s:%(path)s' % {
            'driver': self.nas_driver,
            'host': self.nas_host,
            'path': self.nas_path
        }
        display_name = 'Capabilities of %(product)s %(protocol)s driver' % {
            'product': self.product_name,
            'protocol': self.storage_protocol
        }

        # There will always be exactly 1 pool for NFS driver.
        # VMstore always uses thin provisioning regardless of nfs_sparsed_volumes.
        pool = {
            'pool_name': share_string,
            'total_capacity_gb': data['total_capacity_gb'],
            'free_capacity_gb': data['free_capacity_gb'],
            'reserved_percentage': data['reserved_percentage'],
            'provisioned_capacity_gb': data['provisioned_capacity_gb'],
            'max_over_subscription_ratio': data['max_over_subscription_ratio'],
            'thin_provisioning_support': True,
            'thick_provisioning_support': False,
            'total_volumes': total_volumes,
            'multiattach': False,
            'QoS_support': False,
            'online_extend_support': False,
            'consistencygroup_support': False,
            'consistent_group_snapshot_enabled': False,
        }

        self._stats = {
            'backend_state': 'up',
            'driver_version': self.VERSION,
            'vendor_name': self.vendor_name,
            'storage_protocol': self.storage_protocol,
            'volume_backend_name': self.backend_name,
            'location_info': location_info,
            'display_name': display_name,
            'sparse_copy_volume': True,
            'pools': [pool],
        }
        LOG.debug('Updated volume backend statistics for host %(host)s '
                  'and volume backend %(backend_name)s: %(stats)s',
                  {'host': self.host,
                   'backend_name': self.backend_name,
                   'stats': self._stats})

    def create_volume(self, volume: objects.Volume) -> dict:
        """Creates a volume.

        :param volume: volume reference
        :returns: provider_location update dict for database
        """

        LOG.info('Creating volume %s', volume.name_id)
        if volume.encryption_key_id and not self._supports_encryption:
            message = _('Encryption is not yet supported.')
            raise exception.VolumeDriverException(message=message)

        LOG.debug('Creating volume %(vol)s', {'vol': volume.name_id})
        self._ensure_shares_mounted()

        volume.provider_location = self._get_share_path()

        LOG.info('casted to %s', volume.provider_location)

        self._do_create_volume(volume)
        self.refresh_hypervisor(volume)
        return {'provider_location': volume.provider_location}

    def delete_volume(self, volume):
        """Deletes a logical volume."""

        LOG.info('Deleting volume %s', volume.name_id)
        LOG.debug('Deleting volume %(vol)s, provider_location: %(loc)s',
                  {'vol': volume.name_id, 'loc': volume.provider_location})

        if not volume.provider_location:
            LOG.warning('Volume %s does not have provider_location '
                        'specified, skipping', volume.name)
            return

        # Delete all VMstore snapshots associated with this volume
        self._delete_volume_snapshots(volume)

        info_path = self._local_path_volume_info(volume)
        info = self._read_info_file(info_path, empty_if_missing=True)

        if info:
            base_volume_path = os.path.join(self._local_volume_dir(volume),
                                            info['active'])
            self._delete(info_path)
        else:
            base_volume_path = self._local_path_volume(volume)

        volume_path = base_volume_path
        self._delete(volume_path)

    def _delete_volume_snapshots(self, volume):
        """Delete all VMstore snapshots associated with the volume.

        :param volume: volume reference
        """
        LOG.info('VmstoreNfsDriver _delete_volume_snapshots for volume: %s',
                 volume.name_id)
        volume_id = volume.name_id
        LOG.debug('Checking for VMstore snapshots associated with '
                  'volume %(vol)s', {'vol': volume_id})
        try:
            snapshots = self.vmstore.snapshots.list({'contain': volume_id})
            for vmstore_snapshot in snapshots:
                if vmstore_snapshot.get('vmName') == volume_id:
                    snap_uuid = vmstore_snapshot['uuid']['uuid']
                    LOG.debug('Deleting VMstore snapshot %(snap_uuid)s '
                              'for volume %(vol)s',
                              {'snap_uuid': snap_uuid, 'vol': volume_id})
                    try:
                        self.vmstore.snapshots.delete(snap_uuid)
                    except api.VmstoreException as e:
                        LOG.warning('Failed to delete snapshot %(snap)s '
                                    'for volume %(vol)s: %(err)s',
                                    {'snap': snap_uuid, 'vol': volume_id,
                                     'err': e})
        except api.VmstoreException as e:
            LOG.warning('Failed to list snapshots for volume %(vol)s: %(err)s',
                        {'vol': volume_id, 'err': e})

    def _get_share_path(self):
        LOG.info('VmstoreNfsDriver _get_share_path')
        nas_host = self.configuration.nas_host
        nas_share_path = self.configuration.nas_share_path

        return '%s:%s' % (nas_host, nas_share_path)

    def _check_snapshot_support(self, setup_checking=False):
        LOG.info('VmstoreNfsDriver _check_snapshot_support, '
                 'setup_checking: %s', setup_checking)
        return True

    def create_snapshot(self, snapshot):
        """Creates a snapshot.

        VirtualDisk discovery is performed outside the coordination lock
        via _get_virtual_disk_with_retry() to avoid blocking other Cinder workers
        during potentially long VMstore cache population waits.

        :param snapshot: snapshot reference
        """
        LOG.info('Creating snapshot %s', snapshot['name'])
        volume = snapshot.volume

        # Get virtual disk with retry and hypervisor refresh
        vd = self._get_virtual_disk_with_retry(volume)
        if not vd:
            msg = f'Virtual disk for volume {volume["name"]} not found, cannot create snapshot'
            LOG.error(msg)
            raise api.VmstoreException(code='NotFound', message=msg)
        
        lock_key = self._get_snapshot_lock_key(snapshot.id)
        self._create_snapshot_locked(snapshot, vd, lock_key)

    @coordination.synchronized('{lock_key}')
    def _create_snapshot_locked(self, snapshot, vd, lock_key):
        """Creates a snapshot.

        Uses volume-level lock to allow concurrent snapshots of different volumes.

        :param snapshot: snapshot reference
        :param vd: virtual disk info for the snapshot's volume
        :param lock_key: coordination lock key
        """
        LOG.debug('Creating snapshot (with locking) after aquiring vd %s', snapshot['name'])
        volume = snapshot.volume
        vmstore_subdir = self.nas_path.removeprefix(self.TINTRI_PATH_PREFIX)
        volume_path = os.path.join(vmstore_subdir, volume['name'])
        payload = {
            'typeId': ('com.tintri.api.rest.v310.dto.domain.'
                       'beans.cinder.CinderSnapshotSpec'),
            'file': volume_path,
            'vmName': vd[0]['vmName'],
            'description': snapshot['name'],
            'vmTintriUuid': vd[0]['vmUuid']['uuid'],
            'instanceId': vd[0]['instanceUuid'],
            'snapshotCreator': 'Vmstore cinder driver',
            'deletionPolicy': 'DELETE_ON_EXPIRATION'
        }
        self.vmstore.snapshots.create(payload)
        LOG.info('Snapshot %s created successfully', snapshot['name'])

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot.

        Uses snapshot-level lock to allow concurrent deletion of different snapshots.

        :param snapshot: snapshot reference
        """
        lock_key = self._get_snapshot_lock_key(snapshot.id)
        return self._delete_snapshot_locked(snapshot, lock_key)

    @coordination.synchronized('{lock_key}')
    def _delete_snapshot_locked(self, snapshot, lock_key):
        """Deletes a snapshot with coordination lock.

        :param snapshot: snapshot reference
        :param lock_key: coordination lock key
        """
        LOG.info('Deleting snapshot %s', snapshot['name'])
        snapshots = self.vmstore.snapshots.list({'contain': snapshot['name']})
        snap_uuid = ''
        for vmstore_snapshot in snapshots:
            if snapshot['name'] == vmstore_snapshot['description']:
                snap_uuid = vmstore_snapshot['uuid']['uuid']
                break  # Found it, no need to continue

        if not snap_uuid:
            LOG.info('Did not find snapshot %(name)s, '
                     'this is ok for deletion.',
                     {'name': snapshot['name']})
            return

        try:
            self.vmstore.snapshots.delete(snap_uuid)
            LOG.info('Snapshot %s deleted successfully', snapshot['name'])
        except api.VmstoreException as e:
            if 'VM is still present' in str(e):
                LOG.warning(
                    'Snapshot %s has active clones, will be cleaned up '
                    'when parent volume is deleted: %s', snapshot['name'], e)
            else:
                raise

    def copy_image_to_volume(self,
                             context: context.RequestContext,
                             volume: objects.Volume,
                             image_service,
                             image_id: str,
                             disable_sparse: bool = False) -> None:
        """Fetch the image from image_service and write it to the volume."""
        LOG.info('VmstoreNfsDriver copy_image_to_volume, volume: %s, image_id: %s',
                 volume.id, image_id)
        super().copy_image_to_volume(
            context, volume, image_service, image_id, disable_sparse)

    def copy_volume_to_image(self,
                             context: context.RequestContext,
                             volume: objects.Volume,
                             image_service,
                             image_meta: dict) -> None:
        """Copy the volume to the specified image."""
        LOG.info('VmstoreNfsDriver copy_volume_to_image, volume: %s, image_id: %s',
                 volume.id, image_meta.get('id'))
        super().copy_volume_to_image(
            context, volume, image_service, image_meta)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Create new volume from other's snapshot on appliance.

        Uses snapshot-level lock to allow concurrent clones from different snapshots.

        :param volume: reference of volume to be created
        :param snapshot: reference of source snapshot
        """
        lock_key = self._get_snapshot_lock_key(snapshot.id)
        result = self._create_volume_from_snapshot_locked(volume, snapshot, lock_key)
        self._extend_volume_for_cloned_volume(volume, snapshot['volume_size'])
        return result

    @coordination.synchronized('{lock_key}')
    def _create_volume_from_snapshot_locked(self, volume, snapshot, lock_key):
        """Create new volume from snapshot with coordination lock.

        :param volume: reference of volume to be created
        :param snapshot: reference of source snapshot
        :param lock_key: coordination lock key
        """
        LOG.info('Creating volume %(vol)s from snapshot %(snap)s with lock %(lock)s',
                 {'vol': volume['name'], 'snap': snapshot['name'], 'lock': lock_key})

        # Optimized snapshot lookup with exponential backoff
        snap_uuid = self._wait_for_snapshot(snapshot['name'])

        if not snap_uuid:
            msg = f'Snapshot {snapshot["name"]} not found after polling timeout'
            LOG.error(msg)
            raise api.VmstoreException(code='NotFound', message=msg)

        vmstore_subdir = self.nas_path.removeprefix(self.TINTRI_PATH_PREFIX)
        clone_name = f'{snapshot["name"]}-vol-{volume.name_id}'
        clone_path = os.path.join(vmstore_subdir, clone_name)

        payload = {
            'typeId': ('com.tintri.api.rest.v310.dto.domain.'
                       'beans.cinder.CinderCloneSpec'),
            'tintriSnapshotUuid': snap_uuid,
            'destinationPaths': clone_path,
        }

        LOG.debug(
            'Creating clone from snapshot %(snap)s to %(path)s '
            'with lock %(lock)s',
            {'snap': snapshot['name'], 'path': clone_path, 'lock': lock_key})
        self.vmstore.clones.create(payload)

        # File system operations (no lock needed for these)
        mount_dir = self._get_mount_point_for_share(self._get_share_path())
        temp_clone_dir = os.path.join(mount_dir, clone_name)
        temp_clone_path = os.path.join(temp_clone_dir, snapshot['volume_name'])
        clone_destination = os.path.join(mount_dir, volume['name'])

        try:
            os.rename(temp_clone_path, clone_destination)
            os.rmdir(temp_clone_dir)
        except OSError as exc:
            LOG.error(
                'Failed to rename clone from %(src)s to %(dst)s: %(err)s — '
                'attempting cleanup of temp directory %(dir)s',
                {'src': temp_clone_path, 'dst': clone_destination,
                 'err': exc, 'dir': temp_clone_dir})
            shutil.rmtree(temp_clone_dir, ignore_errors=True)
            raise
        LOG.debug(
            'Clone renamed from %(src)s to %(dst)s with lock %(lock)s',
            {'src': temp_clone_path, 'dst': clone_destination,
             'lock': lock_key})

        # Async refresh - don't block waiting for hypervisor
        self.refresh_hypervisor(volume)
        volume.provider_location = self._get_share_path()

        return {'provider_location': volume.provider_location}

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume.

        VirtualDisk discovery is performed outside the coordination lock
        via _get_virtual_disk_with_retry() to avoid blocking other Cinder workers
        during potentially long VMstore cache population waits.

        :param volume: new volume reference
        :param src_vref: source volume reference
        """
        LOG.info('Creating cloned volume %(vol)s from source %(src)s', {'vol': volume['name'], 'src': src_vref['name']})

        # Get virtual disk with retry and hypervisor refresh
        vd = self._get_virtual_disk_with_retry(src_vref)
        if not vd:
            msg = f'Virtual disk for source volume {src_vref["name"]} not found, cannot create clone'
            LOG.error(msg)
            raise api.VmstoreException(code='NotFound', message=msg)
        
        lock_key = self._get_volume_lock_key(volume.id)
        result = self._create_cloned_volume_locked(volume, src_vref, vd, lock_key)
        self._extend_volume_for_cloned_volume(volume, src_vref['size'])
        return result

    @coordination.synchronized('{lock_key}')
    def _create_cloned_volume_locked(self, volume, src_vref, vd, lock_key):
        """Creates a clone of the specified volume.

        Uses source volume-level lock to allow concurrent clones from different
        source volumes. Multiple clones can be created from different sources
        in parallel.

        Create a snapshot with DELETE_ON_ZERO_CLONE_REFERENCES.
        Create a cloned volume from that snapshot.
        When the cloned volume is deleted, snapshot will get deleted from
        Vmstore automatically due to deletionPolicy

        :param volume: new volume reference
        :param src_vref: source volume reference
        :param vd: virtual disk info for the source volume
        :param lock_key: coordination lock key
        """
        LOG.info('Creating cloned volume %(vol)s from source %(src)s with lock %(lock)s',
                 {'vol': volume.name_id, 'src': src_vref['name'], 'lock': lock_key})

        src_name = src_vref['name']
        vm_uuid = vd[0]['vmUuid']['uuid']
        clone_name = f'clone-{src_name}-{volume.name_id}'
        vmstore_subdir = self.nas_path.removeprefix(self.TINTRI_PATH_PREFIX)

        # Create snapshot for cloning
        payload = {
            'typeId': ('com.tintri.api.rest.v310.dto.domain.'
                       'beans.cinder.CinderSnapshotSpec'),
            'file': os.path.join(vmstore_subdir, src_name),
            'vmName': vd[0]['vmName'],
            'description': clone_name,
            'vmTintriUuid': vm_uuid,
            'instanceId': vd[0]['instanceUuid'],
            'snapshotCreator': 'Vmstore cinder driver',
            'deletionPolicy': 'DELETE_ON_ZERO_CLONE_REFERENCES'
        }

        LOG.debug('Creating temporary snapshot for clone: %s', clone_name)
        resp = self.vmstore.snapshots.create(payload)

        # Try to extract UUID from response
        snap_uuid = None
        if resp:
            if isinstance(resp, list) and len(resp) > 0:
                # Response might be a list with UUID
                if isinstance(resp[0], dict) and 'uuid' in resp[0]:
                    snap_uuid = resp[0]['uuid']['uuid']
                elif isinstance(resp[0], str):
                    snap_uuid = resp[0]

        # If UUID not in response, poll for it with optimized backoff
        if not snap_uuid:
            LOG.debug('Snapshot UUID not in response, polling for %s', clone_name)
            snap_uuid = self._wait_for_snapshot(clone_name, vm_uuid=vm_uuid)

        if not snap_uuid:
            msg = f'Snapshot {clone_name} not found after creation'
            LOG.error(msg)
            raise api.VmstoreException(code='NotFound', causeDetails=msg)

        LOG.debug(
            'Snapshot %(name)s created with UUID %(uuid)s with lock %(lock)s',
            {'name': clone_name, 'uuid': snap_uuid, 'lock': lock_key})

        # Create clone from snapshot
        vmstore_subdir = self.nas_path.removeprefix(self.TINTRI_PATH_PREFIX)
        clone_path = os.path.join(vmstore_subdir, clone_name)

        clone_payload = {
            'typeId': ('com.tintri.api.rest.v310.dto.domain.'
                       'beans.cinder.CinderCloneSpec'),
            'tintriSnapshotUuid': snap_uuid,
            'destinationPaths': clone_path,
        }

        LOG.debug(
            'Creating clone from snapshot %(snap)s to %(path)s '
            'with lock %(lock)s',
            {'snap': clone_name, 'path': clone_path, 'lock': lock_key})
        self.vmstore.clones.create(clone_payload)

        # File system operations (no lock contention)
        mount_dir = self._get_mount_point_for_share(self._get_share_path())
        temp_clone_dir = os.path.join(mount_dir, clone_name)
        temp_clone_path = os.path.join(temp_clone_dir, src_name)
        clone_destination = os.path.join(mount_dir, volume['name'])

        try:
            os.rename(temp_clone_path, clone_destination)
            os.rmdir(temp_clone_dir)
        except OSError as exc:
            LOG.error(
                'Failed to rename clone from %(src)s to %(dst)s: %(err)s — '
                'attempting cleanup of temp directory %(dir)s',
                {'src': temp_clone_path, 'dst': clone_destination,
                 'err': exc, 'dir': temp_clone_dir})
            shutil.rmtree(temp_clone_dir, ignore_errors=True)
            raise
        LOG.debug(
            'Clone renamed from %(src)s to %(dst)s with lock %(lock)s',
            {'src': temp_clone_path, 'dst': clone_destination,
             'lock': lock_key})

        # Async refresh - don't block waiting for hypervisor
        self.refresh_hypervisor(volume)
        volume.provider_location = self._get_share_path()

        LOG.info(
            'Successfully created cloned volume %(vol)s from %(src)s '
            'with lock %(lock)s',
            {'vol': volume['name'], 'src': src_name, 'lock': lock_key})
        return {'provider_location': volume.provider_location}

    def _extend_volume_for_cloned_volume(self, volume, original_size: int) -> None:
        """Extend cloned volume if the requested size is larger than source."""
        new_size = volume['size']

        if new_size is None:
            raise exception.VolumeDriverException(
                message=_("New cloned volume size cannot be None"))

        if original_size is None:
            raise exception.VolumeDriverException(
                message=_("Original source volume size cannot be None"))
        
        LOG.debug(
            "Checking whether cloned volume %s needs resize. "
            "New size: %s, original size: %s.",
            volume['id'], new_size, original_size
        )

        if new_size > original_size:
            LOG.debug(
                "Resize the new volume %s to %s.",
                volume['id'], new_size
            )
            self.extend_volume(volume, new_size)
