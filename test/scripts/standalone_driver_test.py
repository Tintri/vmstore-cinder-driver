#!/usr/bin/env python3
"""Standalone Driver Validation Script.

This script implements the "inner loop" testing strategy from the Gemini discussion:
- Minimal OpenStack mocking (only what's needed to import modules)
- Real API calls to WireMock container
- Real driver method execution
- Validates driver logic without full DevStack deployment

Usage:
    python standalone_driver_test.py

Environment Variables:
    VMSTORE_REST_PROTOCOL: http or https (default: http)
    VMSTORE_REST_ADDRESS: VMstore API host (default: localhost)
    VMSTORE_REST_PORT: VMstore API port (default: 8080)
    VMSTORE_REST_USERNAME: API username (default: admin)
    VMSTORE_REST_PASSWORD: API password (default: admin)
"""

import os
import sys
from pathlib import Path
from unittest import mock

# Add parent directory to path to import driver modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# Mock OpenStack dependencies BEFORE any imports
print("Setting up OpenStack mocks...")

# Mock oslo modules
mock_oslo_log = mock.MagicMock()
mock_oslo_log.log.getLogger = mock.MagicMock(return_value=mock.MagicMock())
sys.modules['oslo_log'] = mock_oslo_log
sys.modules['oslo_log.log'] = mock_oslo_log.log

sys.modules['oslo_utils'] = mock.MagicMock()
sys.modules['oslo_utils.units'] = mock.MagicMock()
sys.modules['oslo_concurrency'] = mock.MagicMock()
sys.modules['oslo_concurrency.processutils'] = mock.MagicMock()

# Mock cinder modules
sys.modules['cinder'] = mock.MagicMock()
sys.modules['cinder.context'] = mock.MagicMock()
sys.modules['cinder.db'] = mock.MagicMock()
sys.modules['cinder.image'] = mock.MagicMock()
sys.modules['cinder.image.image_utils'] = mock.MagicMock()
sys.modules['cinder.objects'] = mock.MagicMock()
sys.modules['cinder.utils'] = mock.MagicMock()
sys.modules['cinder.volume'] = mock.MagicMock()
sys.modules['cinder.volume.configuration'] = mock.MagicMock()
sys.modules['cinder.volume.volume_utils'] = mock.MagicMock()

# Mock coordination decorator
mock_coordination = mock.MagicMock()


def mock_synchronized(lock_name):
    """Mock coordination.synchronized decorator - just pass through."""
    def decorator(func):
        return func
    return decorator


mock_coordination.synchronized = mock_synchronized
sys.modules['cinder.coordination'] = mock_coordination

# Mock interface decorator
mock_interface = mock.MagicMock()


def mock_volumedriver(cls):
    """Mock interface.volumedriver decorator."""
    return cls


mock_interface.volumedriver = mock_volumedriver
sys.modules['cinder.interface'] = mock_interface

# Mock exception classes
mock_exception = mock.MagicMock()


class MockVolumeDriverException(Exception):
    """Mock volume driver exception."""
    pass


mock_exception.VolumeDriverException = MockVolumeDriverException
mock_exception.InvalidVolume = type('InvalidVolume', (Exception,), {})
mock_exception.VolumeNotFound = type('VolumeNotFound', (Exception,), {})
sys.modules['cinder.exception'] = mock_exception

# Mock i18n
mock_i18n = mock.MagicMock()
mock_i18n._ = lambda x: x
sys.modules['cinder.i18n'] = mock_i18n

# Mock vmstore namespace so cinder.volume.drivers.vmstore works
sys.modules['cinder.volume.drivers'] = mock.MagicMock()
sys.modules['cinder.volume.drivers.vmstore'] = mock.MagicMock()

# Mock NFS parent driver
mock_nfs_driver = mock.MagicMock()


class MockNfsDriver:
    """Minimal mock of NfsDriver parent class."""
    
    def __init__(self, *args, **kwargs):
        self.configuration = kwargs.get('configuration')
        self._stats = {}
    
    def _ensure_shares_mounted(self):
        pass
    
    def _find_share(self, volume):
        return '127.0.0.1:/export'


mock_nfs_driver.NfsDriver = MockNfsDriver
sys.modules['cinder.volume.drivers.nfs'] = mock_nfs_driver

# Mock os_brick modules
sys.modules['os_brick'] = mock.MagicMock()
sys.modules['os_brick.encryptors'] = mock.MagicMock()
sys.modules['os_brick.remotefs'] = mock.MagicMock()
sys.modules['os_brick.remotefs.remotefs'] = mock.MagicMock()

# Mock eventlet
sys.modules['eventlet'] = mock.MagicMock()
sys.modules['eventlet.greenthread'] = mock.MagicMock()

# Mock keystoneauth1
sys.modules['keystoneauth1'] = mock.MagicMock()
sys.modules['keystoneauth1.exceptions'] = mock.MagicMock()
sys.modules['keystoneauth1.exceptions.catalog'] = mock.MagicMock()

print("✓ OpenStack mocks configured")

# Now import the actual driver modules
print("\nImporting driver modules...")
try:
    import api
    import nfs
    print("✓ Modules imported successfully")
except Exception as e:
    print(f"✗ Failed to import modules: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)


def test_api_client():
    """Test API client creation and basic connectivity."""
    print("\n" + "=" * 60)
    print("TEST 1: API Client (VmstoreProxy)")
    print("=" * 60)
    
    # Get configuration from environment
    protocol = os.getenv('VMSTORE_REST_PROTOCOL', 'http')
    address = os.getenv('VMSTORE_REST_ADDRESS', 'localhost')
    port = int(os.getenv('VMSTORE_REST_PORT', '8080'))
    username = os.getenv('VMSTORE_REST_USERNAME', 'admin')
    password = os.getenv('VMSTORE_REST_PASSWORD', 'admin')
    
    print(f"Configuration: {protocol}://{address}:{port}")
    
    # Create mock configuration matching what the driver uses
    conf = mock.MagicMock()
    conf.vmstore_rest_protocol = protocol
    conf.vmstore_rest_address = address
    conf.vmstore_rest_port = port
    conf.vmstore_user = username
    conf.vmstore_password = password
    conf.vmstore_rest_retry_count = 3
    conf.vmstore_refresh_retry_count = 3
    conf.vmstore_rest_backoff_factor = 1
    conf.vmstore_rest_connect_timeout = 10
    conf.vmstore_rest_read_timeout = 30
    conf.driver_ssl_cert_verify = False
    conf.driver_ssl_cert_path = None
    
    # Create API proxy (this is what the driver uses)
    try:
        proxy = api.VmstoreProxy(
            proto='nfs',
            backend='vmstore-test',
            conf=conf
        )
        print("✓ API proxy instantiated")
    except Exception as e:
        print(f"✗ Failed to create API proxy: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test appliance info call
    print("\nTesting API call: GET appliance info")
    try:
        info = proxy.appliance.get(None)
        print(f"✓ API call successful")
        if isinstance(info, dict):
            keys = list(info.keys())[:5]
            print(f"  Response keys: {', '.join(keys)}")
        return True
    except Exception as e:
        print(f"✗ API call failed: {e}")
        print(f"  Note: Make sure WireMock container is running (cd test && make start)")
        return False


def test_driver_instantiation():
    """Test driver instantiation."""
    print("\n" + "=" * 60)
    print("TEST 2: Driver Instantiation")
    print("=" * 60)
    
    # Create mock configuration
    config = mock.MagicMock()
    config.vmstore_rest_protocol = 'http'
    config.vmstore_rest_address = 'localhost'
    config.vmstore_rest_port = 8080
    config.vmstore_rest_username = 'admin'
    config.vmstore_rest_password = 'admin'
    config.vmstore_verify_ssl = False
    config.max_over_subscription_ratio = 20.0
    config.reserved_percentage = 0
    
    print("Configuration created")
    
    # Instantiate driver
    try:
        driver = nfs.VmstoreNfsDriver(configuration=config)
        print("✓ Driver instantiated successfully")
        print(f"  Driver class: {driver.__class__.__name__}")
        return True
    except Exception as e:
        print(f"✗ Failed to instantiate driver: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_syntax_validation():
    """Test Python syntax of all driver modules."""
    print("\n" + "=" * 60)
    print("TEST 3: Syntax Validation")
    print("=" * 60)
    
    modules = ['nfs.py', 'api.py', 'utils.py', 'options.py']
    base_path = Path(__file__).parent.parent.parent
    
    all_valid = True
    for module_name in modules:
        module_path = base_path / module_name
        if not module_path.exists():
            print(f"⚠ Module not found: {module_name}")
            continue
        
        try:
            import py_compile
            py_compile.compile(str(module_path), doraise=True)
            print(f"✓ {module_name}: Syntax valid")
        except Exception as e:
            print(f"✗ {module_name}: Syntax error - {e}")
            all_valid = False
    
    return all_valid


def main():
    """Run all standalone tests."""
    print("\n" + "=" * 60)
    print("VMstore Cinder Driver - Standalone Validation")
    print("Inner Loop Testing (Minimal OpenStack Mocking)")
    print("=" * 60)
    
    results = []
    
    # Test 1: Syntax validation (fastest)
    results.append(("Syntax Validation", test_syntax_validation()))
    
    # Test 2: Driver instantiation
    results.append(("Driver Instantiation", test_driver_instantiation()))
    
    # Test 3: API client (requires running containers)
    results.append(("API Client", test_api_client()))
    
    # Print summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    passed = 0
    failed = 0
    for test_name, result in results:
        status = "PASS" if result else "FAIL"
        symbol = "✓" if result else "✗"
        print(f"{symbol} {test_name:30s} {status}")
        if result:
            passed += 1
        else:
            failed += 1
    
    print("=" * 60)
    print(f"Total: {passed} passed, {failed} failed")
    
    if failed > 0:
        print("\n💡 Tip: For API tests, ensure containers are running: cd test && make start")
    
    print("=" * 60)
    
    # Return exit code
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
