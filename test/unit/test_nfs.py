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
import shutil
import tempfile
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

    def _build_share(self, root):
        """Populate a fake share root with volume files and noise."""
        sizes = {'volume-aaa': 4096, 'volume-bbb': 8192, 'volume-ccc': 0}
        for name, size in sizes.items():
            with open(os.path.join(root, name), 'wb') as handle:
                handle.write(b'\0' * size)
        # Noise that must not be counted.
        for name in ('volume-aaa.info', 'volumes-other', 'unrelated'):
            with open(os.path.join(root, name), 'w') as handle:
                handle.write('x')
        os.mkdir(os.path.join(root, 'clone-src-dst'))
        return sum(sizes.values()), len(sizes)

    def test_scan_share_counts_and_sums(self):
        """_scan_share returns volume count and summed logical sizes."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        expected_bytes, expected_count = self._build_share(root)

        driver = self._get_driver()
        count, provisioned = driver._scan_share(root)

        self.assertEqual(expected_count, count)
        self.assertEqual(expected_bytes, provisioned)

    def test_scan_share_missing_mount_point(self):
        """_scan_share degrades to zeros when the share is not mounted."""
        driver = self._get_driver()
        self.assertEqual((0, 0), driver._scan_share('/no/such/mount/point'))

    def test_scan_share_memoises_result(self):
        """_scan_share stores the scan for _cached_scan to reuse."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        expected_bytes, expected_count = self._build_share(root)

        driver = self._get_driver()
        driver._scan_share(root)

        self.assertEqual((root, expected_count, expected_bytes),
                         driver._share_scan)

    def test_cached_scan_reuses_memo_without_rescanning(self):
        """_cached_scan serves the memo instead of walking the share again."""
        driver = self._get_driver()
        driver._share_scan = ('/mnt/share', 7, 4096)

        with mock.patch.object(driver, '_scan_share') as mock_scan:
            self.assertEqual((7, 4096), driver._cached_scan('/mnt/share'))
        mock_scan.assert_not_called()

    def test_cached_scan_ignores_memo_for_a_different_mount(self):
        """A memo for another mount point must not be reused."""
        driver = self._get_driver()
        driver._share_scan = ('/mnt/other', 7, 4096)

        with mock.patch.object(driver, '_scan_share',
                               return_value=(1, 512)) as mock_scan:
            self.assertEqual((1, 512), driver._cached_scan('/mnt/share'))
        mock_scan.assert_called_once_with('/mnt/share')

    def test_get_capacity_info_never_uses_the_memo(self):
        """Capacity checks on the create path must always rescan.

        NfsDriver._find_share and _is_share_eligible call _get_capacity_info
        while creating a volume; a stale total_allocated there would weaken
        the oversubscription check.
        """
        driver = self._get_driver()
        driver._share_scan = ('/mnt/share', 99, 99 * units.Gi)
        driver._execute = mock.Mock(
            return_value=('4096 26214400 13107200', ''))

        with mock.patch.object(driver, '_get_mount_point_for_share',
                               return_value='/mnt/share'), \
                mock.patch.object(driver, '_scan_share',
                                  return_value=(2, 8192)) as mock_scan:
            _total, _avail, provisioned = driver._get_capacity_info(
                '192.168.1.1:/tintri/test_share')

        mock_scan.assert_called_once_with('/mnt/share')
        self.assertEqual(8192, provisioned)

    @mock.patch('cinder.objects.VolumeList.get_all_by_host')
    def test_update_volume_stats_scans_share_once(self, mock_get_volumes):
        """One stats refresh walks the share root exactly once.

        _get_capacity_info performs the scan; _get_provisioned_capacity and
        the total_volumes count reuse the memo.
        """
        mock_get_volumes.return_value = []
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        expected_bytes, expected_count = self._build_share(root)

        driver = self._get_driver()
        driver._mounted_shares = ['192.168.1.1:/tintri/test_share']
        driver._execute = mock.Mock(
            return_value=('4096 26214400 13107200', ''))

        real_scandir = os.scandir
        calls = []

        def counting_scandir(path):
            calls.append(path)
            return real_scandir(path)

        with mock.patch.object(driver, '_ensure_shares_mounted'), \
                mock.patch.object(driver, '_get_mount_point_for_share',
                                  return_value=root), \
                mock.patch.object(vmstore_nfs.os, 'scandir',
                                  side_effect=counting_scandir):
            driver._update_volume_stats()

        self.assertEqual(1, len(calls))
        pool = driver._stats['pools'][0]
        self.assertEqual(expected_count, pool['total_volumes'])
        self.assertEqual(round(expected_bytes / float(units.Gi), 2),
                         pool['provisioned_capacity_gb'])

    @mock.patch('cinder.objects.VolumeList.get_all_by_host')
    def test_update_volume_stats_drops_stale_memo(self, mock_get_volumes):
        """Each refresh rescans rather than trusting the previous memo."""
        mock_get_volumes.return_value = []
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        self._build_share(root)

        driver = self._get_driver()
        driver._mounted_shares = ['192.168.1.1:/tintri/test_share']
        driver._execute = mock.Mock(
            return_value=('4096 26214400 13107200', ''))
        # A memo left over from an earlier refresh.
        driver._share_scan = (root, 99, 99 * units.Gi)

        with mock.patch.object(driver, '_ensure_shares_mounted'), \
                mock.patch.object(driver, '_get_mount_point_for_share',
                                  return_value=root):
            driver._update_volume_stats()

        self.assertEqual(3, driver._stats['pools'][0]['total_volumes'])
