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

"""Unit tests for VMstore NFS driver."""

import os
from unittest import mock

from oslo_utils import units

from cinder import context
from cinder import exception
from cinder.tests.unit import test
from cinder.tests.unit.volume.drivers.vmstore import set_vmstore_overrides
from cinder.volume.drivers.vmstore import api
from cinder.volume.drivers.vmstore import nfs as vmstore_nfs


VMSTORE_CONFIG = {
    'nas_host': '192.168.1.1',
    'nas_share_path': '/tintri/test_share',
    'vmstore_rest_address': '192.168.1.1',
    'vmstore_rest_port': 443,
    'vmstore_rest_protocol': 'https',
    'vmstore_user': 'admin',
    'vmstore_password': 'secret',
    'vmstore_refresh_openstack_region': 'RegionOne',
    'vmstore_mount_point_base': '/mnt/vmstore',
    'vmstore_sparsed_volumes': True,
    'vmstore_qcow2_volumes': False,
}

# Capacity tuple returned by _get_capacity_info mock:
# (total_size_bytes, available_bytes, provisioned_bytes)
_FAKE_CAPACITY = (100.0 * units.Gi, 50.0 * units.Gi, 10.0 * units.Gi)


class VmstoreNfsDriverTestCase(test.TestCase):
    """Test cases for VmstoreNfsDriver class."""

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreNfsDriverTestCase, self).setUp()
        self.context = context.get_admin_context()
        self.configuration = mock.Mock()
        for key, value in VMSTORE_CONFIG.items():
            setattr(self.configuration, key, value)
        self.configuration.reserved_percentage = 0
        self.configuration.max_over_subscription_ratio = 1.0
        self.configuration.nfs_sparsed_volumes = True
        self.configuration.nfs_qcow2_volumes = False
        self.configuration.nfs_mount_point_base = '/mnt/vmstore'
        self.configuration.nfs_mount_options = None
        self.configuration.volume_dd_blocksize = '1M'
        self.configuration.nas_secure_file_operations = 'auto'
        self.configuration.nas_secure_file_permissions = 'auto'
        self.configuration.nas_mount_options = None
        self.configuration.nfs_mount_attempts = 1

        def safe_get_side_effect(key):
            config_map = {
                'nfs_mount_options': 'lookupcache=pos,nolock,noacl,proto=tcp',
                'nas_mount_options': None,
                'volume_dd_blocksize': '1M',
                'nas_secure_file_operations': 'auto',
                'nas_secure_file_permissions': 'auto',
                'vmstore_openstack_hostname': None,
                'volume_backend_name': 'vmstore',
            }
            return config_map.get(key)

        self.configuration.safe_get = mock.Mock(
            side_effect=safe_get_side_effect)

    @mock.patch('os_brick.remotefs.remotefs.RemoteFsClient')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, '_check_snapshot_support')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, 'do_setup')
    def _get_driver(self, mock_do_setup, mock_check_snapshot, mock_remotefs):
        """Create and return a driver instance."""
        driver = vmstore_nfs.VmstoreNfsDriver(configuration=self.configuration)
        driver.vmstore = mock.Mock()
        driver._mounted_shares = ['192.168.1.1:/tintri/test_share']
        driver.shares = {'192.168.1.1:/tintri/test_share': None}
        driver.mount_point_base = '/mnt/vmstore'
        driver.nas_path = '/tintri/test_share'
        return driver

    def test_driver_version(self):
        """Test driver version is defined and non-empty."""
        self.assertIsNotNone(vmstore_nfs.VmstoreNfsDriver.VERSION)
        self.assertGreater(len(vmstore_nfs.VmstoreNfsDriver.VERSION), 0)

    def test_ci_wiki_name(self):
        """Test CI wiki name is defined."""
        self.assertEqual(
            'Vmstore_CI', vmstore_nfs.VmstoreNfsDriver.CI_WIKI_NAME)

    def test_get_driver_options(self):
        """Test get_driver_options returns options list."""
        options = vmstore_nfs.VmstoreNfsDriver.get_driver_options()
        self.assertIsInstance(options, list)
        self.assertTrue(len(options) > 0)

    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, '_get_mount_point_for_share',
                       return_value='/tmp/vmstore-test-nonexistent')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, '_get_provisioned_capacity',
                       return_value=10.0)
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, '_get_capacity_info',
                       return_value=_FAKE_CAPACITY)
    @mock.patch('cinder.objects.VolumeList.get_all_by_host')
    def test_backend_name(self, mock_get_volumes, mock_capacity,
                          mock_provisioned, mock_mnt):
        """Test backend name generation and stats structure."""
        mock_get_volumes.return_value = []
        driver = self._get_driver()
        driver._update_volume_stats()
        self.assertIsNotNone(driver._stats)
        self.assertIn('pools', driver._stats)
        self.assertEqual('vmstore', driver._stats['volume_backend_name'])

    def test_local_volume_dir_md5(self):
        """Test _local_volume_dir returns a path under the mount point base."""
        driver = self._get_driver()
        volume = mock.Mock()
        volume.provider_location = '192.168.1.1:/tintri/test_share'

        # get_mount_point is provided by os_brick.RemoteFsClient (mocked here).
        # Return a realistic path so we can verify _local_volume_dir delegates
        # correctly: the basename should be a 32-character MD5 hex digest.
        fake_hash = 'a' * 32
        driver._remotefsclient.get_mount_point.return_value = (
            '/mnt/vmstore/' + fake_hash)

        vol_dir = driver._local_volume_dir(volume)
        self.assertTrue(vol_dir.startswith('/mnt/vmstore/'))
        hash_part = os.path.basename(vol_dir)
        self.assertEqual(32, len(hash_part))

    def test_load_shares_uses_nas_mount_options_not_global_nfs_options(self):
        """Per-share options must not duplicate global RemoteFsClient options."""
        driver = self._get_driver()

        driver._load_shares()

        self.assertEqual(
            {'192.168.1.1:/tintri/test_share': None},
            driver.shares)

    def test_ensure_share_mounted_does_not_pass_duplicate_mount_flags(self):
        """The mount call should rely on global options when no NAS override exists."""
        driver = self._get_driver()
        driver._remotefsclient.mount = mock.Mock()

        driver._load_shares()
        driver._ensure_share_mounted('192.168.1.1:/tintri/test_share')

        driver._remotefsclient.mount.assert_called_once_with(
            '192.168.1.1:/tintri/test_share', [])


class VmstoreNfsDriverDeleteVolumeTestCase(test.TestCase):
    """Test cases for delete_volume functionality."""

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreNfsDriverDeleteVolumeTestCase, self).setUp()
        self.context = context.get_admin_context()

    @mock.patch('os_brick.remotefs.remotefs.RemoteFsClient')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, '_ensure_shares_mounted')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, '_check_snapshot_support')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, 'do_setup')
    def test_delete_volume_no_provider_location(
            self, mock_do_setup, mock_check_snapshot,
            mock_ensure_shares, mock_remotefs):
        """Test delete_volume with no provider_location returns gracefully."""
        configuration = mock.Mock()
        for key, value in VMSTORE_CONFIG.items():
            setattr(configuration, key, value)
        configuration.reserved_percentage = 0
        configuration.max_over_subscription_ratio = 1.0
        configuration.nfs_sparsed_volumes = True
        configuration.nfs_mount_point_base = '/mnt/vmstore'

        def safe_get_side_effect(key):
            config_map = {
                'nfs_mount_options': 'lookupcache=pos,nolock,noacl,proto=tcp',
                'volume_dd_blocksize': '1M',
                'nas_secure_file_operations': 'auto',
                'nas_secure_file_permissions': 'auto',
                'vmstore_openstack_hostname': None,
            }
            return config_map.get(key)

        configuration.safe_get = mock.Mock(side_effect=safe_get_side_effect)

        driver = vmstore_nfs.VmstoreNfsDriver(configuration=configuration)
        driver.vmstore = mock.Mock()

        volume = mock.Mock()
        volume.provider_location = None
        volume.name = 'test_volume'
        volume.id = 'test-id'

        driver.delete_volume(volume)


class VmstoreNfsDriverSnapshotTestCase(test.TestCase):
    """Test cases for snapshot functionality."""

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreNfsDriverSnapshotTestCase, self).setUp()
        self.context = context.get_admin_context()
        self.configuration = mock.Mock()
        for key, value in VMSTORE_CONFIG.items():
            setattr(self.configuration, key, value)
        self.configuration.reserved_percentage = 0
        self.configuration.max_over_subscription_ratio = 1.0
        self.configuration.nfs_sparsed_volumes = True
        self.configuration.nfs_qcow2_volumes = False
        self.configuration.nfs_mount_point_base = '/mnt/vmstore'
        self.configuration.nfs_mount_options = None

        def safe_get_side_effect(key):
            config_map = {
                'nfs_mount_options': 'lookupcache=pos,nolock,noacl,proto=tcp',
                'volume_dd_blocksize': '1M',
                'nas_secure_file_operations': 'auto',
                'nas_secure_file_permissions': 'auto',
                'vmstore_openstack_hostname': None,
            }
            return config_map.get(key)

        self.configuration.safe_get = mock.Mock(
            side_effect=safe_get_side_effect)

    @mock.patch('os_brick.remotefs.remotefs.RemoteFsClient')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, '_check_snapshot_support')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, 'do_setup')
    def test_delete_snapshot_not_found(
            self, mock_do_setup, mock_check_snapshot, mock_remotefs):
        """Test delete_snapshot when snapshot not found returns gracefully."""
        driver = vmstore_nfs.VmstoreNfsDriver(configuration=self.configuration)
        driver.vmstore = mock.Mock()
        driver.vmstore.snapshots.list.return_value = []

        snapshot = mock.Mock()
        snapshot.__getitem__ = lambda s, key: {
            'name': 'non_existent_snapshot',
            'volume_id': 'test-volume-id'
        }[key]

        driver.delete_snapshot(snapshot)

    @mock.patch('os_brick.remotefs.remotefs.RemoteFsClient')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, '_check_snapshot_support')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, 'do_setup')
    def test_delete_snapshot_vm_present_logs_warning(
            self, mock_do_setup, mock_check_snapshot, mock_remotefs):
        """Test delete_snapshot logs warning when VM is still present."""
        driver = vmstore_nfs.VmstoreNfsDriver(configuration=self.configuration)
        driver.vmstore = mock.Mock()
        driver.vmstore.snapshots.list.return_value = [
            {'description': 'test_snapshot', 'uuid': {'uuid': 'snap-uuid'}}
        ]
        error = api.VmstoreException('VM is still present')
        driver.vmstore.snapshots.delete.side_effect = error

        snapshot = mock.Mock()
        snapshot.__getitem__ = lambda s, key: {
            'name': 'test_snapshot',
            'volume_id': 'test-volume-id'
        }[key]

        driver.delete_snapshot(snapshot)


class VmstoreNfsDriverCreateSnapshotTestCase(test.TestCase):
    """Test cases for create_snapshot VD lookup."""

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreNfsDriverCreateSnapshotTestCase, self).setUp()
        self.context = context.get_admin_context()
        self.configuration = mock.Mock()
        for key, value in VMSTORE_CONFIG.items():
            setattr(self.configuration, key, value)
        self.configuration.reserved_percentage = 0
        self.configuration.max_over_subscription_ratio = 1.0
        self.configuration.nfs_sparsed_volumes = True
        self.configuration.nfs_qcow2_volumes = False
        self.configuration.nfs_mount_point_base = '/mnt/vmstore'
        self.configuration.nfs_mount_options = None
        # PERF_OPTS — must be integers/floats so range() and time.sleep() work
        self.configuration.vmstore_virtual_disk_retries = 1
        self.configuration.vmstore_snapshot_poll_initial_delay = 0.0
        self.configuration.vmstore_get_vd_timeout = 1
        self.configuration.vmstore_snapshot_poll_timeout = 1
        self.configuration.vmstore_snapshot_max_delay = 1.0
        self.configuration.vmstore_use_volume_locks = True

        def safe_get_side_effect(key):
            config_map = {
                'nfs_mount_options': 'lookupcache=pos,nolock,noacl,proto=tcp',
                'volume_dd_blocksize': '1M',
                'nas_secure_file_operations': 'auto',
                'nas_secure_file_permissions': 'auto',
                'vmstore_openstack_hostname': None,
            }
            return config_map.get(key)

        self.configuration.safe_get = mock.Mock(
            side_effect=safe_get_side_effect)

    @mock.patch('os_brick.remotefs.remotefs.RemoteFsClient')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, '_check_snapshot_support')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, 'do_setup')
    def test_create_snapshot_uses_volume_id(
            self, mock_do_setup, mock_check_snapshot, mock_remotefs):
        """Test create_snapshot uses volume.id for VD lookup (since 3.0.9).

        Prior to 3.0.9, volume.name_id was used. VMS-4184 changed this to
        volume.id. This test verifies the current behaviour.
        """
        driver = vmstore_nfs.VmstoreNfsDriver(configuration=self.configuration)
        driver.vmstore = mock.Mock()
        driver.nas_path = '/tintri/test_share'

        mock_volume = mock.Mock()
        mock_volume.id = 'volume-uuid-456'
        mock_volume.name_id = 'volume-name-id-456'
        mock_volume.__getitem__ = lambda s, key: {
            'name': 'volume-volume-name-id-456',
        }[key]

        driver.vmstore.virtual_disk.get.return_value = [{
            'vmName': 'test-vm',
            'vmUuid': {'uuid': 'vm-uuid-123'},
            'instanceUuid': 'instance-uuid-123'
        }]

        snapshot = mock.Mock()
        snapshot.volume = mock_volume
        snapshot.__getitem__ = lambda s, key: {
            'volume_name': 'volume-volume-name-id-456',
            'volume_id': 'volume-db-id-123',
            'name': 'snapshot-name'
        }[key]

        driver.create_snapshot(snapshot)

        # Since 3.0.9: volume.id is used, not name_id
        driver.vmstore.virtual_disk.get.assert_called_with('volume-uuid-456')


class VmstoreNfsDriverShareTestCase(test.TestCase):
    """Test cases for share loading functionality."""

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreNfsDriverShareTestCase, self).setUp()
        self.configuration = mock.Mock()
        for key, value in VMSTORE_CONFIG.items():
            setattr(self.configuration, key, value)
        self.configuration.reserved_percentage = 0
        self.configuration.max_over_subscription_ratio = 1.0
        self.configuration.nfs_sparsed_volumes = True
        self.configuration.nfs_mount_options = None
        self.configuration.nfs_mount_point_base = '/mnt/vmstore'

        def safe_get_side_effect(key):
            config_map = {
                'nfs_mount_options': 'lookupcache=pos,nolock,noacl,proto=tcp',
                'volume_dd_blocksize': '1M',
                'nas_secure_file_operations': 'auto',
                'nas_secure_file_permissions': 'auto',
                'vmstore_openstack_hostname': None,
            }
            return config_map.get(key)

        self.configuration.safe_get = mock.Mock(
            side_effect=safe_get_side_effect)

    @mock.patch('os_brick.remotefs.remotefs.RemoteFsClient')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, '_check_snapshot_support')
    @mock.patch.object(vmstore_nfs.VmstoreNfsDriver, 'do_setup')
    def test_load_shares_invalid_format_raises(
            self, mock_do_setup, mock_check_snapshot, mock_remotefs):
        """Test _load_shares raises on invalid share format."""
        self.configuration.nas_host = 'invalid'
        self.configuration.nas_share_path = 'no_leading_slash'

        driver = vmstore_nfs.VmstoreNfsDriver(configuration=self.configuration)
        driver.vmstore = mock.Mock()

        self.assertRaises(
            exception.InvalidConfigurationValue,
            driver._load_shares)
