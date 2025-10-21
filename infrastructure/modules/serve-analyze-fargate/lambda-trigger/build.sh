#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🔨 Building Lambda trigger package..."

if ! command -v npm &> /dev/null; then
    echo "❌ Error: npm is not installed"
    exit 1
fi

if [ ! -f "package.json" ]; then
    echo "❌ Error: package.json not found"
    exit 1
fi

echo "📦 Installing dependencies..."
npm ci --production=false

echo "🏗️  Compiling TypeScript..."
npm run build

echo "✅ Lambda package built successfully: lambda-trigger.zip"
echo "📊 Package size: $(du -h lambda-trigger.zip | cut -f1)"
