#!/bin/bash
# Script to start the test environment containers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$(dirname "$SCRIPT_DIR")"

echo "🚀 Starting VMstore Test Environment..."
echo "==========================================="

# Check if docker-compose is available
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null 2>&1; then
    echo "❌ Error: docker-compose or 'docker compose' command not found"
    exit 1
fi

# Determine which command to use
if docker compose version &> /dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
else
    COMPOSE_CMD="docker-compose"
fi

# Navigate to test directory
cd "$TEST_DIR"

# Create .env from example if it doesn't exist
if [ ! -f .env ]; then
    echo "📝 Creating .env file from .env.example..."
    cp .env.example .env
    echo "✅ .env file created. You can customize it if needed."
fi

# Start containers
echo "🐳 Starting containers..."
$COMPOSE_CMD up -d

# Wait for services to be healthy
echo "⏳ Waiting for services to be ready..."
sleep 5

# Check if containers are running
echo ""
echo "📊 Container Status:"
$COMPOSE_CMD ps

echo ""
echo "✅ Test environment is ready!"
echo ""
echo "Services available:"
echo "  - NFS Server: localhost:2049 (mount path: /nfs/cinder)"
echo "  - WireMock API: http://localhost:8080"
echo ""
echo "To view logs: $COMPOSE_CMD logs -f"
echo "To stop: ./scripts/stop-test-env.sh"
echo ""
echo "🧪 You can now run tests with: ./scripts/run-standalone-test.sh"
