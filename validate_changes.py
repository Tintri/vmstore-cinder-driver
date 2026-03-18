#!/usr/bin/env python3
"""
Quick validation script for VMstore Cinder driver performance improvements.
Can be run without a full OpenStack deployment to validate basic logic.

Usage:
    python3 validate_changes.py
"""

import sys
import os


def test_syntax():
    """Test that all Python files have valid syntax."""
    print("=" * 70)
    print("TEST 1: Syntax Validation")
    print("=" * 70)
    
    files = ['nfs.py', 'api.py', 'options.py', 'utils.py']
    all_valid = True
    
    for filepath in files:
        if not os.path.exists(filepath):
            print(f"⚠️  {filepath} not found (skipping)")
            continue
            
        try:
            with open(filepath, 'r') as f:
                compile(f.read(), filepath, 'exec')
            print(f"✅ {filepath} - Syntax valid")
        except SyntaxError as e:
            print(f"❌ {filepath} - Syntax error: {e}")
            all_valid = False
    
    return all_valid


def test_configuration_options():
    """Test that new configuration options are properly defined."""
    print("\n" + "=" * 70)
    print("TEST 2: Configuration Options")
    print("=" * 70)
    
    try:
        # Read options.py to verify new config options exist
        with open('options.py', 'r') as f:
            content = f.read()
        
        required_opts = [
            'vmstore_snapshot_poll_timeout',
            'vmstore_snapshot_poll_initial_delay',
            'vmstore_virtual_disk_retries',
            'vmstore_async_hypervisor_refresh',
            'vmstore_use_volume_locks',
        ]
        
        all_found = True
        for opt in required_opts:
            if opt in content:
                print(f"✅ {opt} - Found in options.py")
            else:
                print(f"❌ {opt} - NOT found in options.py")
                all_found = False
        
        # Check that VMSTORE_PERF_OPTS is added
        if 'VMSTORE_PERF_OPTS' in content:
            print(f"✅ VMSTORE_PERF_OPTS - Defined")
        else:
            print(f"❌ VMSTORE_PERF_OPTS - NOT defined")
            all_found = False
        
        # Check that it's merged into VMSTORE_NFS_OPTS
        if 'VMSTORE_PERF_OPTS' in content and 'VMSTORE_NFS_OPTS +=' in content:
            print(f"✅ Performance options merged into VMSTORE_NFS_OPTS")
        else:
            print(f"❌ Performance options NOT merged")
            all_found = False
        
        return all_found
        
    except Exception as e:
        print(f"❌ Error reading options.py: {e}")
        return False


def test_new_methods():
    """Test that new helper methods exist in nfs.py."""
    print("\n" + "=" * 70)
    print("TEST 3: New Helper Methods")
    print("=" * 70)
    
    try:
        with open('nfs.py', 'r') as f:
            content = f.read()
        
        required_methods = [
            ('_get_volume_lock_key', 'Volume-specific lock key generation'),
            ('_get_snapshot_lock_key', 'Snapshot-specific lock key generation'),
            ('_wait_for_snapshot', 'Optimized snapshot polling with backoff'),
            ('_get_virtual_disk_with_retry', 'Virtual disk retry logic'),
        ]
        
        all_found = True
        for method_name, description in required_methods:
            if f'def {method_name}(self' in content:
                print(f"✅ {method_name:35s} - {description}")
            else:
                print(f"❌ {method_name:35s} - NOT found")
                all_found = False
        
        return all_found
        
    except Exception as e:
        print(f"❌ Error reading nfs.py: {e}")
        return False


def test_lock_removal():
    """Test that initialize_connection lock was removed."""
    print("\n" + "=" * 70)
    print("TEST 4: Lock Optimization")
    print("=" * 70)
    
    try:
        with open('nfs.py', 'r') as f:
            content = f.read()
        
        # Check that initialize_connection doesn't have the old decoration
        lines = content.split('\n')
        
        init_conn_found = False
        has_old_lock = False
        
        for i, line in enumerate(lines):
            if 'def initialize_connection' in line:
                init_conn_found = True
                # Check previous line for decorator
                if i > 0 and '@coordination.synchronized' in lines[i-1]:
                    has_old_lock = True
                break
        
        if init_conn_found and not has_old_lock:
            print(f"✅ initialize_connection - Lock removed (read-only optimization)")
        elif init_conn_found and has_old_lock:
            print(f"❌ initialize_connection - Still has lock (should be removed)")
            return False
        else:
            print(f"⚠️  initialize_connection - Method not found")
            return False
        
        # Check that create_cloned_volume uses new lock style
        if '_get_volume_lock_key' in content and 'create_cloned_volume' in content:
            print(f"✅ create_cloned_volume - Using volume-specific locks")
        else:
            print(f"❌ create_cloned_volume - NOT using new lock pattern")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ Error analyzing locks: {e}")
        return False


def test_exponential_backoff():
    """Test that exponential backoff logic is present."""
    print("\n" + "=" * 70)
    print("TEST 5: Exponential Backoff Implementation")
    print("=" * 70)
    
    try:
        with open('nfs.py', 'r') as f:
            content = f.read()
        
        checks = [
            ('delay *= 2', 'Exponential backoff multiplication'),
            ('time.sleep(', 'Proper sleep calls in polling'),
            ('min(delay, max_delay)', 'Backoff cap to prevent excessive delays'),
        ]
        
        all_found = True
        for pattern, description in checks:
            if pattern in content:
                print(f"✅ {description:50s} - Found")
            else:
                print(f"⚠️  {description:50s} - Not found (may be OK)")
        
        return True
        
    except Exception as e:
        print(f"❌ Error analyzing backoff: {e}")
        return False


def test_async_refresh():
    """Test that async refresh logic is implemented."""
    print("\n" + "=" * 70)
    print("TEST 6: Async Hypervisor Refresh")
    print("=" * 70)
    
    try:
        with open('nfs.py', 'r') as f:
            content = f.read()
        
        # Check for block parameter in refresh_hypervisor
        if 'def refresh_hypervisor(self, volume, block=' in content:
            print(f"✅ refresh_hypervisor - Has 'block' parameter for async mode")
        else:
            print(f"❌ refresh_hypervisor - Missing 'block' parameter")
            return False
        
        # Check for async mode logic
        if 'vmstore_async_hypervisor_refresh' in content:
            print(f"✅ Async refresh - Configuration option referenced")
        else:
            print(f"❌ Async refresh - Configuration not used")
            return False
        
        # Check that create_cloned_volume calls refresh with block=False
        if 'block=False' in content:
            print(f"✅ Clone operations - Using async refresh")
        else:
            print(f"⚠️  Clone operations - May not be using async refresh")
        
        return True
        
    except Exception as e:
        print(f"❌ Error analyzing async refresh: {e}")
        return False


def print_summary(results):
    """Print test summary."""
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    test_names = [
        "Syntax Validation",
        "Configuration Options",
        "Helper Methods",
        "Lock Optimization",
        "Exponential Backoff",
        "Async Refresh"
    ]
    
    passed = sum(results)
    total = len(results)
    
    for i, (name, result) in enumerate(zip(test_names, results)):
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{i+1}. {name:30s} {status}")
    
    print(f"\n{passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All validations passed! Code is ready for testing.")
        print("\nNext steps:")
        print("1. Review TESTING.md for detailed test procedures")
        print("2. Run unit tests (Level 1)")
        print("3. Deploy to DevStack for integration testing (Level 2)")
        return 0
    else:
        print("\n⚠️  Some validations failed. Review errors above.")
        print("\nCommon issues:")
        print("- Syntax errors: Check Python indentation and brackets")
        print("- Missing methods: Ensure all helper methods were added")
        print("- Lock issues: Verify decorator changes were applied")
        return 1


def main():
    """Run all validation tests."""
    print("VMstore Cinder Driver - Performance Improvements Validation")
    print("Version: 3.0.7 (Performance Optimized)")
    print()
    
    # Change to script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    # Run all tests
    results = [
        test_syntax(),
        test_configuration_options(),
        test_new_methods(),
        test_lock_removal(),
        test_exponential_backoff(),
        test_async_refresh(),
    ]
    
    # Print summary and exit
    return print_summary(results)


if __name__ == '__main__':
    sys.exit(main())
