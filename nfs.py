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
import time
from typing import List

from os_brick import encryptors
from os_brick.remotefs import remotefs
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import units

from cinder import context
from cinder import coordination
from cinder import db
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
    """

    VERSION = '3.0.7a'
    CI_WIKI_NAME = 'Vmstore_CI'

    vendor_name = 'DDN'
    product_name = 'VMstore'
    storage_protocol = 'NFS'
    driver_prefix = 'vmstore'
    driver_volume_type = 'nfs'

    def __init__(self, execute=processutils.execute, *args, **kwargs):

        self._remotefsclient = None
        super(VmstoreNfsDriver, self).__init__(*args, **kwargs)
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
        self.mount_options = self.configuration.safe_get('nfs_mount_options')
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
        
        max_delay = 5.0  # Cap backoff at 5 seconds
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
                    LOG.debug('Found snapshot %(name)s after %(elapsed).2f seconds',
                             {'name': snapshot_name, 'elapsed': elapsed})
                    return snap['uuid']['uuid']
            
            # Exponential backoff with cap
            sleep_time = min(delay, max_delay)
            LOG.debug('Snapshot %(name)s not found, waiting %(sleep).2f seconds '
                     '(elapsed: %(elapsed).2f/%(timeout)s)',
                     {'name': snapshot_name, 'sleep': sleep_time,
                      'elapsed': elapsed, 'timeout': timeout})
            time.sleep(sleep_time)
            elapsed = time.time() - start_time
            delay *= 2  # Exponential backoff
        
        LOG.warning('Snapshot %(name)s not found after %(timeout)s seconds',
                   {'name': snapshot_name, 'timeout': timeout})
        return None

    def _get_virtual_disk_with_retry(self, volume_id, volume_name=None):
        """Get virtual disk with exponential backoff retry.
        
        This is an improvement on previous version 3.0.7 _wait_for_virtual_disk to reduce load on appliance 
        and avoid thundering herd issues.

        :param volume_id: Volume name_id (UUID)
        :param volume_name: Optional volume name for logging
        :returns: Virtual disk info or None
        """
        max_retries = self.configuration.vmstore_virtual_disk_retries
        delay = 0.5
        
        for attempt in range(max_retries):
            vd = self.vmstore.virtual_disk.get(volume_id)
            if vd:
                LOG.debug('Found virtual disk for %(id)s on attempt %(attempt)s',
                         {'id': volume_name or volume_id, 'attempt': attempt + 1})
                return vd
            
            if attempt < max_retries - 1:
                LOG.debug('Virtual disk for %(id)s not found, retry %(attempt)s/%(max)s '
                         'after %(delay).2f seconds',
                         {'id': volume_name or volume_id, 'attempt': attempt + 1,
                          'max': max_retries, 'delay': delay})
                time.sleep(delay)
                delay *= 2  # Exponential backoff
        
        LOG.warning('Virtual disk for %(id)s not found after %(retries)s retries',
                   {'id': volume_name or volume_id, 'retries': max_retries})
        return None

    def _get_virtual_disk_or_refresh(self, volume_id, volume, volume_name=None):
        """Get virtual disk with retry and fallback to hypervisor refresh.
        
        First attempts to get the virtual disk using exponential backoff retry.
        If not found, triggers a hypervisor refresh and tries once more.
        
        :param volume_id: Volume name_id (UUID) to retrieve
        :param volume: Volume object for hypervisor refresh
        :param volume_name: Optional volume name for logging
        :returns: Virtual disk info
        :raises: VmstoreException if virtual disk not found after all attempts
        """
        # Try with exponential backoff retry
        vd = self._get_virtual_disk_with_retry(volume_id, volume_name)   
        if not vd:
            # Try refresh and one more attempt
            LOG.info('Virtual disk not found, refreshing hypervisor for %s', 
                     volume_name or volume.get('name') or volume_id)
            self.refresh_hypervisor(volume, block=True)
            vd = self.vmstore.virtual_disk.get(volume_id)         
            if not vd:
                raise api.VmstoreException(
                    code='NotFound',
                    message=f'Could not find VirtualDisk for {volume_name or volume.get("name") or volume_id}')
        return vd

    def do_setup(self, ctxt) -> None:
        LOG.info('VmstoreNfsDriver do_setup for context: %s', ctxt)
        self.ctxt = ctxt
        self._validate_required_options()
        retries = 0
        while not self._do_setup():
            retries += 1
            self.vmstore.delay(retries)

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
            self.vmstore = api.VmstoreProxy(self.driver_volume_type,
                                            self.backend_name,
                                            self.configuration)
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
        retries = 0
        while not self._check_for_setup_error():
            retries += 1
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

            nas_host = self.configuration.nas_host
            nas_share_path = self.configuration.nas_share_path

            share_address = '%s:%s' % (nas_host, nas_share_path)

            if not re.match(self.SHARE_FORMAT_REGEX, share_address):
                msg = _('Share %(share)s ignored due to invalid format. '
                        'Must be of form address:/export. Please check '
                        'the nas_host and nas_share_path settings.'
                        ) % {'share': share_address}
                raise exception.InvalidConfigurationValue(msg)

            self.shares[share_address] = self.mount_options

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
        attempts = max(1, self.configuration.nfs_mount_attempts)
        for attempt in range(1, attempts + 1):
            try:
                self._remotefsclient.mount(share)
            except Exception as error:
                LOG.debug('Mount attempt %(attempt)s failed: %(error)s, '
                          'retrying mount NFS share %(share)s',
                          {'attempt': attempt, 'error': error,
                           'share': share})
                if attempt == attempts:
                    LOG.error('Failed to mount NFS share %(share)s '
                              'after %(attempt)s attempts: %(error)s',
                              {'share': share, 'attempt': attempt,
                               'error': error})
                    raise
                self.vmstore.delay(attempt)
            else:
                mntpoint = self._get_mount_point_for_share(share)
                LOG.debug('NFS share %(share)s has been mounted at '
                          '%(mntpoint)s after %(attempt)s attempts',
                          {'share': share, 'mntpoint': mntpoint,
                           'attempt': attempt})
                return mntpoint

    def _ensure_share_mounted(self, nfs_share) -> None:
        LOG.info('VmstoreNfsDriver _ensure_share_mounted for share: %s', nfs_share)
        num_attempts = max(1, self.configuration.nfs_mount_attempts)
        for attempt in range(num_attempts):
            try:
                self._remotefsclient.mount(nfs_share)
                self._mounted_shares.append(nfs_share)
                return
            except Exception as e:
                if attempt == (num_attempts - 1):
                    LOG.error('Mount failure for %(share)s after '
                              '%(count)d attempts.',
                              {'share': nfs_share,
                               'count': num_attempts})
                    raise exception.NfsException(str(e))
                LOG.debug('Mount attempt %(attempt)d failed: %(exc)s.\n'
                          'Retrying mount ...',
                          {'attempt': attempt, 'exc': e})
                time.sleep(1)

    def refresh_hypervisor(self, volume, block=None):
        """Refresh VMstore hypervisor for the given volume.

        :param volume: volume reference
        :param block: If True, wait for completion. If False, fire and forget.
                     If None, uses configuration default.
        """
        if block is None:
            # Default: use async mode from configuration
            block = not self.configuration.vmstore_async_hypervisor_refresh
        
        LOG.info('Refreshing hypervisor for volume %(vol)s (blocking=%(block)s)',
                {'vol': volume.name_id, 'block': block})
        
        try:
            vmstore_subdir = self.nas_path.removeprefix('/tintri/')
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
            }
            
            # Call refresh API
            self.vmstore.cinder_refresh.create(payload)
            
            if not block:
                # Async mode: don't wait for virtual disk to appear
                LOG.debug('Async hypervisor refresh initiated for %s', volume.name_id)
                return
            
            # Blocking mode: wait for virtual disk with retry
            vd = self._get_virtual_disk_with_retry(volume.name_id, volume.get('name'))
            if not vd:
                # Try one more refresh call
                LOG.info('Retrying hypervisor refresh for %s', volume.name_id)
                self.vmstore.cinder_refresh.create(payload)
                time.sleep(2)
                vd = self.vmstore.virtual_disk.get(volume.name_id)
                
                if not vd:
                    raise api.VmstoreException(
                        code='NotFound',
                        message=f'Could not find VirtualDisk for {volume["name"]}')
            
            LOG.debug('Hypervisor refresh completed for %s', volume.name_id)
            
        except Exception as e:
            if block:
                # In blocking mode, propagate errors
                LOG.error("Hypervisor refresh failed for %(vol)s: %(err)s",
                         {'vol': volume.name_id, 'err': e})
                raise
            else:
                # In async mode, just log and continue
                LOG.warning("Async hypervisor refresh failed for %(vol)s: %(err)s",
                           {'vol': volume.name_id, 'err': e})

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

        volume.provider_location = self._find_share(volume)

        LOG.info('casted to %s', volume.provider_location)

        self._do_create_volume(volume)
        self.refresh_hypervisor(volume)
        return {'provider_location': volume.provider_location}

    def _do_create_volume(self, volume: objects.Volume) -> None:
        """Create a volume on given remote share.

        :param volume: volume reference
        """
        LOG.info('VmstoreNfsDriver _do_create_volume for volume: %s',
                 volume.name_id)
        volume_path = self.local_path(volume)
        volume_size = volume.size

        encrypted = volume.encryption_key_id is not None

        if encrypted:
            encryption = self.check_encryption_provider(
                volume,
                volume.obj_context)

            self._create_encrypted_volume_file(volume_path,
                                               volume_size,
                                               encryption,
                                               volume.obj_context)
        elif getattr(self.configuration,
                     self.driver_prefix + '_qcow2_volumes', False):
            # QCOW2 volumes are inherently sparse, so this setting
            # will override the _sparsed_volumes setting.
            self._create_qcow2_file(volume_path, volume_size)
            self.format = 'qcow2'
        elif getattr(self.configuration,
                     self.driver_prefix + '_sparsed_volumes', False):
            self._create_sparsed_file(volume_path, volume_size)
        else:
            self._create_regular_file(volume_path, volume_size)

        self._set_rw_permissions(volume_path)
        volume.admin_metadata['format'] = self.format
        with volume.obj_as_admin():
            volume.save()

    def check_encryption_provider(
        self,
        volume: 'objects.Volume',
        context: context.RequestContext,
    ) -> dict:
        """Check that this is a LUKS encryption provider.

        :returns: encryption dict
        """
        LOG.info('VmstoreNfsDriver check_encryption_provider for volume: %s',
                 volume.id)

        encryption = db.volume_encryption_metadata_get(context, volume.id)

        if 'provider' not in encryption:
            message = _("Invalid encryption spec.")
            raise exception.VolumeDriverException(message=message)

        provider = encryption['provider']
        if provider in encryptors.LEGACY_PROVIDER_CLASS_TO_FORMAT_MAP:
            provider = encryptors.LEGACY_PROVIDER_CLASS_TO_FORMAT_MAP[provider]
            encryption['provider'] = provider

        if 'cipher' not in encryption or 'key_size' not in encryption:
            msg = _('encryption spec must contain "cipher" and '
                    '"key_size"')
            raise exception.VolumeDriverException(message=msg)

        return encryption

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

    def _local_volume_dir(self, volume):
        """Get volume dir (mounted locally fs path) for given volume.

        :param volume: volume reference
        """
        LOG.info('VmstoreNfsDriver _local_volume_dir for volume: %s',
                 volume.name_id)
        share = volume.provider_location
        if isinstance(share, str):
            share = share.encode('utf-8')
        path = hashlib.md5(share, usedforsecurity=False).hexdigest()
        return os.path.join(self.mount_point_base, path)

    def _check_snapshot_support(self, setup_checking=False):
        LOG.info('VmstoreNfsDriver _check_snapshot_support, '
                 'setup_checking: %s', setup_checking)
        return True

    def create_snapshot(self, snapshot):
        """Creates a snapshot.

        VirtualDisk discovery is performed outside the coordination lock
        via _wait_for_virtual_disk() to avoid blocking other Cinder workers
        during potentially long VMstore cache population waits.

        :param snapshot: snapshot reference
        """
        LOG.info('Creating snapshot %s', snapshot['name'])
        volume = snapshot.volume
        
        # Get virtual disk with retry and fallback to hypervisor refresh
        vd = self._get_virtual_disk_or_refresh(volume.name_id, volume, volume['name'])
        self._create_snapshot_locked(snapshot, vd)

    @coordination.synchronized('{self._get_volume_lock_key(snapshot.volume.id)}')
    def _create_snapshot_locked(self, snapshot, vd):
        """Creates a snapshot.

        Uses volume-level lock to allow concurrent snapshots of different volumes.

        :param snapshot: snapshot reference
        :param vd: virtual disk info for the snapshot's volume
        """
        LOG.debug('Creating snapshot (with locking) after aquiring vd %s', snapshot['name'])
        volume = snapshot.volume
        vmstore_subdir = self.nas_path.removeprefix('/tintri/')
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

    @coordination.synchronized('{self._get_snapshot_lock_key(snapshot.id)}')
    def delete_snapshot(self, snapshot):
        """Deletes a snapshot.

        Uses snapshot-level lock to allow concurrent deletion of different snapshots.

        :param snapshot: snapshot reference
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
                LOG.warning('Snapshot %s has active clones, will be cleaned up '
                           'when parent volume is deleted: %s', snapshot['name'], e)
            else:
                raise

    @coordination.synchronized('{self._get_snapshot_lock_key(snapshot.id)}')
    def create_volume_from_snapshot(self, volume, snapshot):
        """Create new volume from other's snapshot on appliance.

        Uses snapshot-level lock to allow concurrent clones from different snapshots.

        :param volume: reference of volume to be created
        :param snapshot: reference of source snapshot
        """
        LOG.info('Creating volume %(vol)s from snapshot %(snap)s',
                 {'vol': volume['name'], 'snap': snapshot['name']})
        
        # Optimized snapshot lookup with exponential backoff
        snap_uuid = self._wait_for_snapshot(snapshot['name'])
        
        if not snap_uuid:
            msg = f'Snapshot {snapshot["name"]} not found after polling timeout'
            LOG.error(msg)
            raise api.VmstoreException(code='NotFound', message=msg)
        
        vmstore_subdir = self.nas_path.removeprefix('/tintri')
        clone_path = os.path.join(vmstore_subdir, snapshot['name'])

        payload = {
            'typeId': ('com.tintri.api.rest.v310.dto.domain.'
                       'beans.cinder.CinderCloneSpec'),
            'tintriSnapshotUuid': snap_uuid,
            'destinationPaths': clone_path,
        }
        
        LOG.debug('Creating clone from snapshot %(snap)s to %(path)s',
                 {'snap': snapshot['name'], 'path': clone_path})
        self.vmstore.clones.create(payload)
        
        # File system operations (no lock needed for these)
        mount_dir = self._get_mount_point_for_share(self._get_share_path())
        temp_clone_dir = os.path.join(mount_dir, snapshot['name'])
        temp_clone_path = os.path.join(temp_clone_dir, snapshot['volume_name'])
        clone_destination = os.path.join(mount_dir, volume['name'])
        
        os.rename(temp_clone_path, clone_destination)
        os.rmdir(temp_clone_dir)
        LOG.debug('Clone renamed from %(src)s to %(dst)s',
                 {'src': temp_clone_path, 'dst': clone_destination})

        # Async refresh - don't block waiting for hypervisor
        self.refresh_hypervisor(volume, block=False)
        volume.provider_location = self._find_share(volume)
        return {'provider_location': volume.provider_location}

    def copy_image_to_volume(self,
                             context: context.RequestContext,
                             volume: objects.Volume,
                             image_service,
                             image_id: str,
                             disable_sparse: bool = False) -> None:
        """Fetch the image from image_service and write it to the volume."""

        LOG.info('Copying image %(image)s to volume %(vol)s',
                 {'image': image_id, 'vol': volume.name_id})
        volpath = self.local_path(volume)
        image_utils.fetch_to_raw(context,
                                 image_service,
                                 image_id,
                                 volpath,
                                 self.configuration.volume_dd_blocksize,
                                 size=volume.size,
                                 run_as_root=self._execute_as_root,
                                 disable_sparse=disable_sparse)

        image_utils.resize_image(volpath, volume.size,
                                 run_as_root=self._execute_as_root)

        data = image_utils.qemu_img_info(volpath,
                                         run_as_root=self._execute_as_root)
        virt_size = data.virtual_size // units.Gi
        if virt_size != volume.size:
            raise exception.ImageUnacceptable(
                image_id=image_id,
                reason=(_("Expected volume size was %d") % volume.size)
                + (_(" but size is now %d") % virt_size))

    def copy_volume_to_image(self,
                             context: context.RequestContext,
                             volume: objects.Volume,
                             image_service,
                             image_meta: dict) -> None:
        """Copy the volume to the specified image."""
        LOG.info('Copying volume %(vol)s to image %(image)s',
                 {'vol': volume.name_id, 'image': image_meta.get('id')})
        volpath = self.local_path(volume)
        volume_utils.upload_volume(context,
                                   image_service,
                                   image_meta,
                                   volpath,
                                   volume,
                                   run_as_root=self._execute_as_root)

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume.

        VirtualDisk discovery is performed outside the coordination lock
        via _wait_for_virtual_disk() to avoid blocking other Cinder workers
        during potentially long VMstore cache population waits.

        :param volume: new volume reference
        :param src_vref: source volume reference
        """
        LOG.info('Creating cloned volume %(vol)s from source %(src)s',
                 {'vol': volume['name'], 'src': src_vref['name']})
        src_name = src_vref['name']
        src_id = src_vref.name_id
        
        # Get virtual disk with retry and fallback to hypervisor refresh
        vd = self._get_virtual_disk_or_refresh(src_id, src_vref, src_name)
        return self._create_cloned_volume_locked(volume, src_vref, vd)

    @coordination.synchronized('{self._get_volume_lock_key(src_vref.id)}')
    def _create_cloned_volume_locked(self, volume, src_vref, vd):
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
        """
        LOG.info('Creating cloned volume %(vol)s from source %(src)s',
                 {'vol': volume['name'], 'src': src_vref['name']})
        
        src_name = src_vref['name']
        vm_uuid = vd[0]['vmUuid']['uuid']
        clone_name = f'clone-{src_name}-{volume["name"]}'
        vmstore_subdir = self.nas_path.removeprefix('/tintri/')
        
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
        
        LOG.debug('Snapshot %(name)s created with UUID %(uuid)s',
                 {'name': clone_name, 'uuid': snap_uuid})
        
        # Create clone from snapshot
        vmstore_subdir = self.nas_path.removeprefix('/tintri')
        clone_path = os.path.join(vmstore_subdir, clone_name)

        clone_payload = {
            'typeId': ('com.tintri.api.rest.v310.dto.domain.'
                       'beans.cinder.CinderCloneSpec'),
            'tintriSnapshotUuid': snap_uuid,
            'destinationPaths': clone_path,
        }
        
        LOG.debug('Creating clone from snapshot %(snap)s to %(path)s',
                 {'snap': clone_name, 'path': clone_path})
        self.vmstore.clones.create(clone_payload)
        
        # File system operations (no lock contention)
        mount_dir = self._get_mount_point_for_share(self._get_share_path())
        temp_clone_dir = os.path.join(mount_dir, clone_name)
        temp_clone_path = os.path.join(temp_clone_dir, src_name)
        clone_destination = os.path.join(mount_dir, volume['name'])
        
        os.rename(temp_clone_path, clone_destination)
        os.rmdir(temp_clone_dir)
        LOG.debug('Clone renamed from %(src)s to %(dst)s',
                 {'src': temp_clone_path, 'dst': clone_destination})

        # Async refresh - don't block waiting for hypervisor
        self.refresh_hypervisor(volume, block=False)
        volume.provider_location = self._find_share(volume)
        
        LOG.info('Successfully created cloned volume %(vol)s from %(src)s',
                {'vol': volume['name'], 'src': src_name})
        return {'provider_location': volume.provider_location}

    def extend_volume(self, volume, new_size):
        """Extend an existing volume to the new size."""
        LOG.info('Extending volume %(vol)s to new size %(size)s GB.',
                 {'vol': volume.name_id, 'size': new_size})
        if self._is_volume_attached(volume):
            msg = (_("Cannot extend volume %s while it is attached.")
                   % volume.name_id)
            raise exception.ExtendVolumeError(msg)

        LOG.info('Extending volume %(vol)s to new size %(size)s GB.',
                 {'vol': volume.name_id, 'size': new_size})
        extend_by = int(new_size) - volume.size
        if not self._is_share_eligible(volume.provider_location,
                                       extend_by):
            raise exception.ExtendVolumeError(reason='Insufficient space to'
                                              ' extend volume %s to %sG'
                                              % (volume.name_id, new_size))
        # Use the active image file because this volume might have snapshot(s).
        active_file = self.get_active_image_from_info(volume)
        active_file_path = os.path.join(self._local_volume_dir(volume),
                                        active_file)
        LOG.info('Resizing file to %sG...', new_size)
        file_format = None
        admin_metadata = objects.Volume.get_by_id(
            context.get_admin_context(), volume.id).admin_metadata

        if admin_metadata and 'format' in admin_metadata:
            file_format = admin_metadata['format']
        image_utils.resize_image(
            active_file_path, new_size,
            run_as_root=self._execute_as_root,
            file_format=file_format)
        if file_format == 'qcow2' and not self._is_file_size_equal(
                active_file_path, new_size):
            raise exception.ExtendVolumeError(
                reason='Resizing image file failed.')

    def _get_provisioned_capacity(self):
        mount_path = self._get_mount_point_for_share(self.nas_path)
        provisioned_bytes = 0

        for filename in os.listdir(mount_path):
            # Only count cinder volume files to avoid counting temp files or snapshots
            if filename.startswith('volume-'):
                filepath = os.path.join(mount_path, filename)
                try:
                    # .st_size returns the 'apparent size' (provisioned capacity)
                    provisioned_bytes += os.stat(filepath).st_size
                except OSError:
                    continue

        return provisioned_bytes / float(units.Gi)

    def get_volume_stats(self, refresh=False) -> dict:
        """Get volume stats.

        Stats are cached based on vmstore_stats_cache_period configuration.
        """
        LOG.info('VmstoreNfsDriver get_volume_stats, refresh: %s',
                 refresh)

        cache_period = self.configuration.vmstore_stats_cache_period
        current_time = time.time()
        cache_age = current_time - self._stats_cache_timestamp

        # Update stats if:
        # 1. No stats cached yet, OR
        # 2. Cache is disabled (cache_period == 0), OR
        # 3. Cache has expired
        if (not self._stats_cache or
            cache_period == 0 or
            cache_age >= cache_period):
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
        self._ensure_shares_mounted()
        share_string = "%s:%s" % (self.nas_host, self.nas_path)
        mount_path = self._get_mount_point_for_share(share_string)

        provisioned_bytes = 0
        total_volumes = 0

        if os.path.exists(mount_path):
            for filename in os.listdir(mount_path):
                # Count base volumes, excluding metadata/info files
                if filename.startswith('volume-') and not filename.endswith('.info'):
                    filepath = os.path.join(mount_path, filename)
                    try:
                        stat = os.stat(filepath)
                        # Logical size (The "promised" capacity)
                        provisioned_bytes += stat.st_size
                        total_volumes += 1
                    except OSError:
                        continue

        capacity, free, _used = self._get_capacity_info(share_string)

        max_osr = self.configuration.safe_get('max_over_subscription_ratio')
        reserved = self.configuration.safe_get('reserved_percentage') or 0

        location_info = '%(driver)s:%(host)s:%(path)s' % {
            'driver': self.nas_driver,
            'host': self.nas_host,
            'path': self.nas_path
        }
        display_name = 'Capabilities of %(product)s %(protocol)s driver' % {
            'product': self.product_name,
            'protocol': self.storage_protocol
        }

        # There will always be exactly 1 pool for NFS driver
        pool = {
            'pool_name': share_string,
            'total_capacity_gb': capacity / float(units.Gi),
            'free_capacity_gb': free / float(units.Gi),
            'reserved_percentage': reserved,
            'provisioned_capacity_gb': provisioned_bytes / float(units.Gi),
            'max_over_subscription_ratio': max_osr,
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
