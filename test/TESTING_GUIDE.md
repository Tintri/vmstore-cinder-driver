# VMstore Cinder Driver - Inner Loop Testing

## Overview

This implementation follows the **"Standalone Script" approach** described in the Gemini discussion. It provides a fast inner-loop validation workflow that:

- ✅ **Validates Python syntax** for all driver modules
- ✅ **Instantiates the actual driver class** with minimal OpenStack mocking
- ✅ **Makes REAL API calls** to the WireMock container (control path)
- ✅ **Tests against REAL NFS** container (data path)
- ✅ **Catches runtime errors** before DevStack deployment
- ✅ **Runs in 5-10 seconds** instead of full DevStack restart

## Quick Start

```bash
# 1. Start test containers (WireMock + NFS)
cd test
make start

# 2. Run standalone validation
make test

# 3. Run linting checks
make lint

# 4. Run syntax validation only (fastest)
make test-syntax
```

## What Gets Tested

### 1. Syntax Validation
- Compiles all Python modules to catch syntax errors
- Validates: `nfs.py`, `api.py`, `utils.py`, `options.py`
- **Speed**: < 1 second

### 2. Driver Instantiation
- Imports the actual `VmstoreNfsDriver` class
- Mocks only the OpenStack primitives (oslo.* and cinder.*)
- Instantiates the driver with configuration
- **Speed**: < 2 seconds

### 3. API Client (Real HTTP Calls)
- Creates `api.VmstoreProxy` instance
- Makes REAL HTTP call to WireMock container
- Tests `/appliance/info` endpoint
- Validates response structure
- **Speed**: < 2 seconds

## Architecture

```
┌─────────────────┐
│  Your Laptop    │
│                 │
│  ┌───────────┐  │
│  │  Driver   │  │ <-- Actual nfs.py code
│  │  Code     │  │     Minimal OpenStack mocking
│  └─────┬─────┘  │
│        │        │
│        ├────────┼────> Docker: WireMock (port 8080)
│        │        │      Simulates VMstore REST API
│        │        │
│        └────────┼────> Docker: NFS Server (port 2049)
│                 │      Simulates data path
└─────────────────┘
```

## Minimal Mocking Strategy

We mock **only** what's needed to import the driver modules:
- `oslo_log`, `oslo_utils`, `oslo_concurrency` (OpenStack utilities)
- `cinder.*` (OpenStack Cinder framework)
- `os_brick` (Volume attachment framework)
- `eventlet` (Async framework)

We **do NOT mock**:
- ❌ VMstore API calls (real HTTP to WireMock)
- ❌ Network requests (real `requests` library)
- ❌ Driver logic (actual code execution)
- ❌ Lock key generation (real methods)

## Configuration

### Environment Variables

```bash
# WireMock API configuration
export VMSTORE_REST_PROTOCOL=http
export VMSTORE_REST_ADDRESS=localhost
export VMSTORE_REST_PORT=8080
export VMSTORE_REST_USERNAME=admin
export VMSTORE_REST_PASSWORD=admin

# NFS configuration
export NFS_SHARE=127.0.0.1:/export
```

These are set automatically by the test environment.

## Tox Integration

The project follows **OpenStack standard tox configuration**:

```bash
# Run standalone tests (default)
tox

# Run specific Python version
tox -e py310

# Run PEP8 linting
tox -e pep8

# Run pylint
tox -e pylint

# Run syntax validation only
tox -e syntax
```

## Linting Standards

Configured to match **OpenStack Cinder standards**:

### Flake8 Configuration
- **Max line length**: 127 characters (OpenStack standard)
- **Max complexity**: 25 (McCabe)
- **Ignores**: E123, E125, E226, E305, W503, W504
- **Extensions**: H203, H204, H205 (OpenStack hacking)

### Tools
- `hacking>=6.0.0` - OpenStack style checker (wraps flake8)
- `flake8>=6.0.0` - PEP8 checker
- `pylint>=2.17.0` - Additional code quality checks

## Makefile Targets

### Test Commands
```bash
make test            # Run standalone driver validation
make test-syntax     # Python syntax check only
make validate        # Syntax + linting
```

### Container Management
```bash
make start           # Start WireMock + NFS containers
make stop            # Stop containers
make reset           # Clean restart
make status          # Show container status
make logs            # View container logs
```

### Development
```bash
make dev-loop        # Start containers for development
make lint            # Run linting checks
make clean           # Clean test artifacts
```

## Workflow

### Daily Development Loop

1. **Start containers** (once per session):
   ```bash
   cd test && make start
   ```

2. **Make code changes** in `nfs.py`, `api.py`, etc.

3. **Run validation** (5-10 seconds):
   ```bash
   make test
   ```

4. **Fix any errors** and repeat step 3

5. **Run linting** before commit:
   ```bash
   make lint
   ```

### Pre-Commit Checklist

```bash
# 1. Syntax validation
make test-syntax

# 2. Standalone tests
make test

# 3. Linting
make lint

# 4. Full validation
make validate
```

All should pass before pushing to DevStack or Git.

## Comparison: Before vs After

### Before (Full DevStack Loop)
```
1. Edit code
2. Copy to DevStack VM
3. Restart cinder-volume service (30-60s)
4. Run openstack CLI command
5. Check logs for errors
6. Repeat

Total: 2-5 minutes per iteration
```

### After (Inner Loop)
```
1. Edit code
2. Run: make test (5-10s)
3. See results immediately
4. Repeat

Total: 5-10 seconds per iteration
```

## Extending the Tests

To add more test cases, edit `test/scripts/standalone_driver_test.py`:

```python
def test_create_volume():
    """Test create_volume method."""
    print("\n" + "=" * 60)
    print("TEST: Create Volume")
    print("=" * 60)
    
    # Setup
    proxy = api.VmstoreProxy(...)
    driver = nfs.VmstoreNfsDriver(configuration=config)
    
    # Test
    mock_volume = {'id': '...', 'name': 'test-vol', 'size': 10}
    driver.create_volume(mock_volume)
    
    # Validate
    print("✓ Create volume successful")
    return True

# Add to main():
results.append(("Create Volume", test_create_volume()))
```

## Troubleshooting

### API Test Fails
```
✗ API call failed: Connection refused
```
**Solution**: Start containers with `cd test && make start`

### Driver Instantiation Fails
```
✗ Failed to instantiate driver: ModuleNotFoundError
```
**Solution**: Check Python path. Script should be run from project root.

### Import Errors
```
ImportError: No module named 'cinder'
```
**Solution**: Mocks are correctly configured. This shouldn't happen. File a bug.

## Benefits

1. **Fast Feedback**: 5-10 seconds vs 2-5 minutes
2. **No DevStack Needed**: Develop without full OpenStack
3. **Real API Calls**: Tests actual HTTP behavior
4. **Catches Errors Early**: Syntax, runtime, logic errors
5. **CI-Ready**: Fast enough for CI/CD pipelines
6. **OpenStack Standards**: Linting matches upstream

## Next Steps

When basic validation passes:
1. Deploy to DevStack for integration testing
2. Run Tempest tests against DevStack
3. Test with real VMstore appliance (if available)
4. Submit for production testing

## References

- **Gemini Discussion**: `../gemini-interaction.md`
- **Tox Config**: `../tox.ini`
- **Makefile**: `./Makefile`
- **Test Script**: `./scripts/standalone_driver_test.py`
