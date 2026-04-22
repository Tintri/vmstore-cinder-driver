"""
Test fixtures for VMstore driver tests.

These fixtures provide reusable test data and mock objects.
"""

import pytest
from unittest import mock


# ============================================================================
# Configuration Fixtures
# ============================================================================

@pytest.fixture
def mock_config():
    """Create a mock configuration object for testing."""
    config = mock.Mock()
    
    # VMstore API configuration
    config.vmstore_rest_protocol = 'http'
    config.vmstore_rest_address = 'localhost'
    config.vmstore_rest_port = 8080
    config.vmstore_user = 'admin'
    config.vmstore_password = 'test_password'
    config.vmstore_rest_connect_timeout = 30.0
    config.vmstore_rest_read_timeout = 300.0
    config.vmstore_rest_backoff_factor = 0.5
    config.vmstore_rest_retries = 3
    config.vmstore_rest_status_retries = 3
    
    # NFS configuration
    config.nfs_shares_config = '/tmp/nfs_shares_test.txt'
    config.nfs_mount_point_base = '/tmp/cinder_nfs_test'
    config.nfs_mount_options = 'vers=3'
    
    # Volume configuration
    config.vmstore_qcow2_volumes = False
    config.vmstore_sparsed_volumes = True
    config.vmstore_refresh_openstack_region = ''
    config.vmstore_refresh_retry_count = 3
    config.vmstore_get_vd_timeout = 60
    
    # Backend configuration
    config.volume_backend_name = 'vmstore-test'
    config.backend_availability_zone = 'nova'
    config.max_over_subscription_ratio = 20.0
    config.reserved_percentage = 0
    
    return config


@pytest.fixture
def mock_context():
    """Create a mock OpenStack context object."""
    context = mock.Mock()
    context.project_id = 'test-project-id-12345'
    context.user_id = 'test-user-id-67890'
    context.is_admin = True
    return context


# ============================================================================
# Volume Fixtures
# ============================================================================

@pytest.fixture
def mock_volume():
    """Create a standard mock volume object."""
    return {
        'id': 'test-volume-id-12345',
        'name': 'volume-test-volume-id-12345',
        'display_name': 'test-volume',
        'size': 10,
        'status': 'available',
        'host': 'cinder@vmstore#vmstore-test',
        'availability_zone': 'nova',
        'provider_location': 'localhost:/nfs/cinder',
    }


@pytest.fixture
def mock_volume_small():
    """Create a small (1GB) mock volume."""
    return {
        'id': 'test-volume-small-id',
        'name': 'volume-test-volume-small-id',
        'display_name': 'test-volume-small',
        'size': 1,
        'status': 'available',
        'host': 'cinder@vmstore#vmstore-test',
        'availability_zone': 'nova',
    }


@pytest.fixture
def mock_volume_large():
    """Create a large (100GB) mock volume."""
    return {
        'id': 'test-volume-large-id',
        'name': 'volume-test-volume-large-id',
        'display_name': 'test-volume-large',
        'size': 100,
        'status': 'available',
        'host': 'cinder@vmstore#vmstore-test',
        'availability_zone': 'nova',
    }


# ============================================================================
# Snapshot Fixtures
# ============================================================================

@pytest.fixture
def mock_snapshot(mock_volume):
    """Create a mock snapshot object."""
    return {
        'id': 'test-snapshot-id-12345',
        'name': 'snapshot-test-snapshot-id-12345',
        'display_name': 'test-snapshot',
        'volume_id': mock_volume['id'],
        'volume': mock_volume,
        'status': 'available',
    }


# ============================================================================
# API Response Fixtures
# ============================================================================

@pytest.fixture
def vmstore_volume_response():
    """Mock VMstore API response for volume creation."""
    return {
        'uuid': {'uuid': 'vmstore-volume-uuid-123'},
        'name': 'test-volume',
        'size': 10,
        'status': 'ONLINE',
        'provisionedSize': 10,
        'usedSize': 0,
        'snapshot': False,
        'thin': True,
        'createTime': '2026-03-22T10:00:00.000+0000',
        'modifiedTime': '2026-03-22T10:00:00.000+0000',
        'datastore': {
            'uuid': 'datastore-001'
        }
    }


@pytest.fixture
def vmstore_snapshot_response():
    """Mock VMstore API response for snapshot creation."""
    return {
        'uuid': {'uuid': 'vmstore-snapshot-uuid-123'},
        'name': 'test-snapshot',
        'size': 10,
        'status': 'ONLINE',
        'snapshot': True,
        'createTime': '2026-03-22T10:00:00.000+0000',
        'sourceVolume': {
            'uuid': 'vmstore-volume-uuid-123'
        }
    }


@pytest.fixture
def vmstore_appliance_info():
    """Mock VMstore API response for appliance info."""
    return {
        'uuid': {'uuid': 'appliance-001-uuid'},
        'modelName': 'VMstore T5000',
        'serialNumber': 'VMST-001',
        'version': '6.0.1.1',
        'osVersion': 'Ubuntu 20.04',
        'status': 'HEALTHY',
        'totalCapacity': 10995116277760,
        'usedCapacity': 1099511627776,
        'availableCapacity': 9895604649984
    }


# ============================================================================
# Error Response Fixtures
# ============================================================================

@pytest.fixture
def vmstore_error_response():
    """Mock VMstore API error response."""
    return {
        'typeId': 'VmstoreError',
        'code': 'ERR_VOLUME_NOT_FOUND',
        'source': 'VMstoreAPI',
        'message': 'Volume not found',
        'causeDetails': 'The requested volume does not exist'
    }
