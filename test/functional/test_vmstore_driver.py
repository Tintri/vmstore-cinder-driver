# Copyright 2026 DDN, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0

"""VMstore driver functional tests.

These tests exercise the real Cinder API stack (API service + Volume service +
Scheduler, all in-process, SQLite, fake oslo.messaging transport) with the
VMstore driver wired in as the backend.

What is mocked:
  - VMstore REST API  → stateful _MockVmstoreBackend handles all HTTP calls
  - NFS mount         → _ensure_share_mounted is a no-op; _get_mount_point_for_share
                        returns a real tmpdir so file operations work
  - run_as_root       → stripped from processutils.execute (container runs as root)

What runs for real:
  - Full Cinder API (HTTP on localhost)
  - Volume state machine (creating → available → deleting → deleted)
  - Our nfs.py driver methods (create_volume, create_snapshot, etc.)
  - Our api.py transport layer (_execute, _collect, _check_error, pagination)
  - Actual file creation (truncate / rm on tmpdir)
"""

import json
import os
import shutil
import tempfile
import uuid
from unittest import mock
from urllib.parse import unquote_plus

from oslo_concurrency import processutils

from cinder.tests.functional import functional_helpers
from cinder.volume import configuration
from cinder.volume.drivers.vmstore import nfs as vmstore_nfs


# ---------------------------------------------------------------------------
# Mock VMstore backend
# ---------------------------------------------------------------------------

_APPLIANCE_UUID = str(uuid.uuid4())


class _MockVmstoreBackend:
    """Stateful fake VMstore REST API.

    Tracks snapshots (with source-file metadata) and known volumes so that
    VirtualDisk lookup after refresh_hypervisor calls returns realistic data.
    Handles clone requests by creating the temp-directory structure the driver
    expects to find after a clone operation completes.
    """

    def __init__(self, mount_dir: str):
        self._mount_dir = mount_dir
        # snap_uuid → {'description': str, 'vmName': str, 'file': str}
        self._snapshots: dict = {}
        # volume IDs seen in a refresh payload → eligible for VD lookup
        self._known_volumes: set = set()

    def make_session(self) -> mock.MagicMock:
        session = mock.MagicMock()
        session.headers = {}
        session.verify = False
        session.auth = None
        session.request.side_effect = self._handle
        return session

    # ------------------------------------------------------------------
    # Request dispatcher
    # ------------------------------------------------------------------

    def _handle(self, method: str, url: str, **kwargs):
        method = method.upper()
        body = {}
        if kwargs.get('data'):
            try:
                body = json.loads(kwargs['data'])
            except (TypeError, ValueError):
                pass

        if '/session/login' in url:
            return self._resp(200, {}, cookies={'JSESSIONID': 'test-token'})

        if '/appliance' in url:
            return self._resp(200, {
                'items': [{'uuid': {'uuid': _APPLIANCE_UUID}}]
            })

        if 'virtualDisk' in url:
            return self._handle_virtual_disk(url)

        if 'cinder/host/refresh' in url and method == 'POST':
            return self._handle_refresh(body)

        if 'cinder/snapshot' in url and method == 'POST':
            return self._handle_snapshot_create(body)

        if 'cinder/clone' in url and method == 'POST':
            return self._handle_clone(body)

        if '/snapshot' in url and method == 'GET':
            return self._handle_snapshot_list(url)

        if '/snapshot/' in url and method == 'DELETE':
            snap_id = url.rstrip('/').split('/')[-1]
            self._snapshots.pop(snap_id, None)
            return self._resp(204, None)

        return self._resp(200, {})

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_virtual_disk(self, url: str):
        vol_id = ''
        if 'uuid=' in url:
            vol_id = unquote_plus(url.split('uuid=')[-1].split('&')[0])
        if vol_id and vol_id in self._known_volumes:
            vd = {
                'vmName': f'volume-{vol_id[:8]}',
                'vmUuid': {'uuid': str(uuid.uuid4())},
                'instanceUuid': str(uuid.uuid4()),
            }
            return self._resp(200, {'items': [vd]})
        return self._resp(200, {'items': []})

    def _handle_refresh(self, body: dict):
        vol_id = body.get('volumeId', '')
        if vol_id:
            self._known_volumes.add(vol_id)
        return self._resp(200, {})

    def _handle_snapshot_create(self, body: dict):
        snap_id = str(uuid.uuid4())
        self._snapshots[snap_id] = {
            'uuid': {'uuid': snap_id},
            'description': body.get('description', ''),
            'vmName': body.get('vmName', ''),
            # 'file' lets the clone handler find the source volume name
            'file': body.get('file', ''),
        }
        return self._resp(201, {'items': [snap_id]})

    def _handle_snapshot_list(self, url: str):
        contain = ''
        if 'contain=' in url:
            contain = unquote_plus(url.split('contain=')[1].split('&')[0])
        matches = [
            s for s in self._snapshots.values()
            if contain in s.get('description', '')
            or contain in s.get('vmName', '')
        ]
        return self._resp(200, {'items': matches})

    def _handle_clone(self, body: dict):
        """Create the temp-directory structure the driver expects after a clone.

        After calling POST /cinder/clone the driver does:
            os.rename(mount_dir/clone_name/src_vol_name, mount_dir/dst_vol_name)
            os.rmdir(mount_dir/clone_name)

        We create mount_dir/clone_name/src_vol_name so the rename succeeds.
        """
        dest = body.get('destinationPaths', '')
        # dest = 'vmstore_subdir/clone_name' — we only need the last part
        clone_name = dest.split('/')[-1] if '/' in dest else dest

        snap_uuid = body.get('tintriSnapshotUuid', '')
        snap = self._snapshots.get(snap_uuid, {})
        # snap['file'] = 'vmstore_subdir/volume-src-uuid'
        src_file = snap.get('file', '')
        src_vol_name = src_file.split('/')[-1] if '/' in src_file else src_file

        if clone_name and src_vol_name:
            temp_clone_dir = os.path.join(self._mount_dir, clone_name)
            os.makedirs(temp_clone_dir, exist_ok=True)
            open(os.path.join(temp_clone_dir, src_vol_name), 'w').close()

        return self._resp(201, {})

    # ------------------------------------------------------------------
    # Response factory
    # ------------------------------------------------------------------

    @staticmethod
    def _resp(status_code: int, body, cookies=None):
        r = mock.Mock()
        r.status_code = status_code
        r.ok = status_code < 400
        r.content = json.dumps(body).encode() if body is not None else b''
        r.cookies = cookies or {}
        r.request = mock.Mock()
        r.request.method = 'UNKNOWN'
        return r


# ---------------------------------------------------------------------------
# Functional test base
# ---------------------------------------------------------------------------

class VmstoreDriverFunctionalTest(functional_helpers._FunctionalTestBase):
    """Functional tests for VmstoreNfsDriver through the real Cinder API."""

    _vol_type_name = 'vmstore-functional'

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='vmstore-func-test-')
        self.addCleanup(shutil.rmtree, self._tmpdir, ignore_errors=True)

        self._vmstore = _MockVmstoreBackend(self._tmpdir)

        # ---- Patch requests.Session ----
        # Must be in place before VmstoreProxy is created inside do_setup().
        session_patcher = mock.patch('requests.Session',
                                     return_value=self._vmstore.make_session())
        session_patcher.start()
        self.addCleanup(session_patcher.stop)

        # ---- Strip run_as_root from processutils ----
        # The container runs as root so commands succeed without sudo/rootwrap.
        _orig_execute = processutils.execute

        def _execute_no_root(*args, **kwargs):
            kwargs.pop('run_as_root', None)
            kwargs.pop('root_helper', None)
            return _orig_execute(*args, **kwargs)

        execute_patcher = mock.patch(
            'oslo_concurrency.processutils.execute',
            side_effect=_execute_no_root)
        execute_patcher.start()
        self.addCleanup(execute_patcher.stop)

        # ---- Redirect NFS mount point to tmpdir ----
        mnt_patcher = mock.patch.object(
            vmstore_nfs.VmstoreNfsDriver,
            '_get_mount_point_for_share',
            return_value=self._tmpdir)
        mnt_patcher.start()
        self.addCleanup(mnt_patcher.stop)

        # ---- Make _ensure_share_mounted a no-op ----
        # The share is added to _mounted_shares by the calling loop on success.
        ensure_patcher = mock.patch.object(
            vmstore_nfs.VmstoreNfsDriver,
            '_ensure_share_mounted')
        ensure_patcher.start()
        self.addCleanup(ensure_patcher.stop)

        # ---- Fake capacity so _update_volume_stats doesn't call stat(1) ----
        from oslo_utils import units as oslo_units
        cap_patcher = mock.patch.object(
            vmstore_nfs.VmstoreNfsDriver,
            '_get_capacity_info',
            return_value=(
                100.0 * oslo_units.Gi,
                80.0 * oslo_units.Gi,
                5.0 * oslo_units.Gi,
            ))
        cap_patcher.start()
        self.addCleanup(cap_patcher.stop)

        prov_patcher = mock.patch.object(
            vmstore_nfs.VmstoreNfsDriver,
            '_get_provisioned_capacity',
            return_value=5.0)
        prov_patcher.start()
        self.addCleanup(prov_patcher.stop)

        # Start Cinder services — do_setup() is called here
        super().setUp()

        self.api.create_type(self._vol_type_name)

    def _get_flags(self):
        f = super()._get_flags()
        g = configuration.SHARED_CONF_GROUP

        f['volume_driver'] = {
            'v': 'cinder.volume.drivers.vmstore.nfs.VmstoreNfsDriver',
            'g': g}
        f['volume_backend_name'] = {'v': self._vol_type_name, 'g': g}
        f['nas_host'] = {'v': '192.168.1.100', 'g': g}
        f['nas_share_path'] = {'v': '/tintri/cinder', 'g': g}
        f['nfs_mount_options'] = {
            'v': 'vers=3,lookupcache=pos,nolock,noacl,proto=tcp', 'g': g}
        f['vmstore_rest_address'] = {'v': '192.168.1.100', 'g': g}
        f['vmstore_rest_protocol'] = {'v': 'http', 'g': g}
        f['vmstore_rest_port'] = {'v': 8080, 'g': g}
        f['vmstore_user'] = {'v': 'admin', 'g': g}
        f['vmstore_password'] = {'v': 'secret', 'g': g}
        f['vmstore_openstack_hostname'] = {'v': 'cinder-functional', 'g': g}
        f['vmstore_refresh_openstack_region'] = {'v': 'RegionOne', 'g': g}
        f['vmstore_rest_retry_count'] = {'v': 1, 'g': g}
        f['vmstore_refresh_retry_count'] = {'v': 1, 'g': g}
        f['vmstore_rest_backoff_factor'] = {'v': 0, 'g': g}
        f['vmstore_virtual_disk_retries'] = {'v': 2, 'g': g}
        f['vmstore_snapshot_poll_timeout'] = {'v': 5, 'g': g}
        f['vmstore_snapshot_poll_initial_delay'] = {'v': 0.1, 'g': g}
        f['vmstore_snapshot_max_delay'] = {'v': 1.0, 'g': g}
        f['vmstore_get_vd_timeout'] = {'v': 2, 'g': g}
        f['vmstore_use_volume_locks'] = {'v': True, 'g': g}
        f['vmstore_sparsed_volumes'] = {'v': True, 'g': g}
        f['vmstore_qcow2_volumes'] = {'v': False, 'g': g}
        f['vmstore_stats_cache_period'] = {'v': 0, 'g': g}
        f['default_volume_type'] = {'v': self._vol_type_name}
        f['report_interval'] = {'v': 1}
        f['service_down_time'] = {'v': 180}

        return f

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_volume(self, size=1, name=None):
        body = {'volume': {'size': size, 'volume_type': self._vol_type_name}}
        if name:
            body['volume']['name'] = name
        vol = self.api.post_volume(body)
        return self._poll_volume_while(vol['id'], ['creating'],
                                       expected_end_status='available')

    def _delete_volume(self, vol_id):
        self.api.delete_volume(vol_id)
        result = self._poll_volume_while(vol_id, ['deleting'])
        self.assertIsNone(result, 'Volume should be deleted')

    def _create_snapshot(self, vol_id):
        snap = self.api.post_snapshot({'snapshot': {'volume_id': vol_id}})
        return self._poll_snapshot_while(snap['id'], ['creating'],
                                         expected_end_status='available')

    def _delete_snapshot(self, snap_id):
        self.api.delete_snapshot(snap_id)
        result = self._poll_snapshot_while(snap_id, ['deleting'])
        self.assertIsNone(result, 'Snapshot should be deleted')

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_create_and_delete_volume(self):
        """Volume lifecycle: creating → available → deleting → gone.

        Verifies the full Cinder state machine for a volume backed by the
        VMstore driver. Also verifies that a volume file is created in the
        (mocked) NFS share directory and removed on deletion.
        """
        vol = self._create_volume(name='functional-test-vol')

        self.assertEqual('available', vol['status'])
        self.assertIsNotNone(vol['id'])

        # The driver should have created a real sparse file in tmpdir
        vol_file = os.path.join(self._tmpdir, f"volume-{vol['id']}")
        self.assertTrue(
            os.path.exists(vol_file),
            f'Expected volume file {vol_file} to exist after creation')

        # VMstore refresh was called — volume ID must be tracked
        self.assertIn(vol['id'], self._vmstore._known_volumes,
                      'refresh_hypervisor must register volume with VMstore')

        self._delete_volume(vol['id'])

        self.assertFalse(
            os.path.exists(vol_file),
            'Volume file should be removed after deletion')

    def test_create_and_delete_snapshot(self):
        """Snapshot lifecycle through the real Cinder API.

        Verifies that the driver calls the VMstore snapshot REST API correctly:
        the snapshot must appear in the mock backend's snapshot store after
        creation and disappear after deletion.
        """
        vol = self._create_volume()
        snap = self._create_snapshot(vol['id'])

        self.assertEqual('available', snap['status'])
        expected_snapshot_name = f"snapshot-{snap['id']}"

        # VMstore snapshot must have been registered in the mock backend
        snap_in_backend = any(
            s['description'] == expected_snapshot_name
            for s in self._vmstore._snapshots.values()
        )
        self.assertTrue(
            snap_in_backend,
            'Snapshot must be registered in VMstore mock backend')

        self._delete_snapshot(snap['id'])

        snap_in_backend_after = any(
            s['description'] == expected_snapshot_name
            for s in self._vmstore._snapshots.values()
        )
        self.assertFalse(
            snap_in_backend_after,
            'Snapshot must be removed from VMstore mock backend after deletion')

        self._delete_volume(vol['id'])

    def test_create_volume_from_snapshot(self):
        """Clone from snapshot through the real Cinder API.

        Verifies the snapshot → clone → rename pipeline.  The mock backend
        creates the temp-clone directory structure the driver expects; the
        driver renames the file and removes the temp dir.
        """
        # Create source volume and snapshot
        src_vol = self._create_volume(name='src-vol')
        snap = self._create_snapshot(src_vol['id'])

        # Create a new volume from the snapshot
        clone_body = {
            'volume': {
                'size': 1,
                'snapshot_id': snap['id'],
                'volume_type': self._vol_type_name,
            }
        }
        clone = self.api.post_volume(clone_body)
        clone = self._poll_volume_while(clone['id'], ['creating'],
                                        expected_end_status='available')

        self.assertEqual('available', clone['status'])

        # The cloned volume file should exist in tmpdir
        clone_file = os.path.join(self._tmpdir, f"volume-{clone['id']}")
        self.assertTrue(
            os.path.exists(clone_file),
            f'Cloned volume file {clone_file} should exist')

        # Cleanup
        self._delete_volume(clone['id'])
        self._delete_snapshot(snap['id'])
        self._delete_volume(src_vol['id'])

    def test_create_cloned_volume(self):
        """Direct volume clone (volume → volume) through the real Cinder API."""
        src_vol = self._create_volume(name='src-for-clone')

        clone_body = {
            'volume': {
                'size': 1,
                'source_volid': src_vol['id'],
                'volume_type': self._vol_type_name,
            }
        }
        clone = self.api.post_volume(clone_body)
        clone = self._poll_volume_while(clone['id'], ['creating'],
                                        expected_end_status='available')

        self.assertEqual('available', clone['status'])

        clone_file = os.path.join(self._tmpdir, f"volume-{clone['id']}")
        self.assertTrue(
            os.path.exists(clone_file),
            f'Cloned volume file {clone_file} should exist')

        self._delete_volume(clone['id'])
        self._delete_volume(src_vol['id'])

    def test_multiple_volumes_independent(self):
        """Two volumes can be created and deleted independently."""
        vol_a = self._create_volume(name='vol-a')
        vol_b = self._create_volume(name='vol-b')

        self.assertNotEqual(vol_a['id'], vol_b['id'])
        self.assertEqual('available', vol_a['status'])
        self.assertEqual('available', vol_b['status'])

        # Both files exist
        for vol in (vol_a, vol_b):
            self.assertTrue(
                os.path.exists(
                    os.path.join(self._tmpdir, f"volume-{vol['id']}")))

        self._delete_volume(vol_a['id'])
        # vol_b still exists
        self.assertTrue(
            os.path.exists(
                os.path.join(self._tmpdir, f"volume-{vol_b['id']}")))

        self._delete_volume(vol_b['id'])
