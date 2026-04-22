#!/bin/bash
# Script to reset the test environment (clean volumes and restart)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$(dirname "$SCRIPT_DIR")"

echo "🔄 Resetting VMstore Test Environment..."

# Determine which command to use
if docker compose version &> /dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
else
    COMPOSE_CMD="docker-compose"
fi

cd "$TEST_DIR"

# Stop and remove everything including volumes
echo "🗑️  Removing containers and volumes..."
$COMPOSE_CMD down -v

# Start fresh
echo "🚀 Starting clean environment..."
$COMPOSE_CMD up -d

echo "⏳ Waiting for services to be ready..."
sleep 5

echo "✅ Test environment reset complete!"
