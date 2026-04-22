# VMstore Cinder Driver - Testing Guide

This directory should contain the test infrastructure for the VMstore Cinder NFS driver, including containerized dependencies and standalone testing capabilities for rapid development.

### Test Environment Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  Your Development Machine                │
│                                                           │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Driver Code (Python)                             │  │
│  │  - nfs.py                                          │  │
│  │  - api.py                                          │  │
│  │  - utils.py                                        │  │
│  └───────────┬───────────────────────┬─────────────────┘  │
│              │                       │                    │
│              │ Control Path          │ Data Path          │
│              │ (REST API)            │ (NFS)              │
│              ▼                       ▼                    │
│  ┌──────────────────────┐  ┌──────────────────────┐     │
│  │  WireMock Container  │  │  NFS Server Container │     │
│  │  Port: 8080          │  │  Port: 2049           │     │
│  │  Mock VMstore API    │  │  Serves /nfs/cinder   │     │
│  └──────────────────────┘  └──────────────────────┘     │
│                                                           │
└─────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Install Dependencies

```bash
# Install test dependencies
cd test
pip install -r requirements.txt
```

### 2. Start Test Environment

```bash
# Start NFS and WireMock containers
./scripts/start-test-env.sh
```

### 3. Run Standalone Test (Inner Loop)

```bash
# Quick validation of driver functionality
./scripts/run-standalone-test.sh
```

## Test Architecture

### Directory Structure

```
test/
├── docker-compose.yml          # Container orchestration
├── .env.example                # Environment configuration template
├── requirements.txt            # Python test dependencies
│
├── fixtures/                   # Test fixtures and mock data
│   └── fixtures.py             # Pytest fixtures
│
├── mocks/                      # Mock backend configurations
│   └── wiremock/
│       ├── __files/            # JSON response files
│       └── mappings/           # API endpoint mappings
│
├── scripts/                    # Management scripts
│   ├── start-test-env.sh       # Start containers
│   ├── stop-test-env.sh        # Stop containers
│   ├── reset-test-env.sh       # Clean restart
│   └── view-logs.sh            # View container logs
```

## Inner Loop Development

The "inner loop" refers to rapid development iteration without deploying a full OpenStack environment. This approach allows you to:

1. **Write code** → 2. **Test immediately** → 3. **Fix quickly** → Repeat

### Workflow

```bash
# 1. Make changes to driver code
vim ../nfs.py

# 2. Run standalone test to validate
./scripts/run-standalone-test.sh

# 3. View detailed logs if needed
./scripts/view-logs.sh wiremock

# 4. Reset environment if needed
./scripts/reset-test-env.sh
```

### What the Standalone Test Does

The standalone test (`standalone_test.py`) validates your test environment infrastructure:

- ✅ **WireMock API connectivity** - Confirms mock VMstore API is accessible
- ✅ **API endpoint testing** - Tests create volume, appliance info, etc.
- ✅ **NFS server connectivity** - Confirms storage backend is accessible
- ✅ **Driver syntax validation** - Ensures driver code has no syntax errors
- ✅ **No OpenStack required** - Works without installing OpenStack libraries

It should run the class code as proposed by gemini in the gemini-interaction.md file, but without the need for a full OpenStack deployment.

