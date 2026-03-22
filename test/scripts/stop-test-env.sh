#!/bin/bash
# Script to stop the test environment containers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$(dirname "$SCRIPT_DIR")"

echo "🛑 Stopping VMstore Test Environment..."

# Determine which command to use
if docker compose version &> /dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
else
    COMPOSE_CMD="docker-compose"
fi

cd "$TEST_DIR"

# Stop and remove containers
$COMPOSE_CMD down

echo "✅ Test environment stopped."
echo ""
echo "To remove volumes as well: $COMPOSE_CMD down -v"
