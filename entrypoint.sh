#!/bin/bash
set -e

echo "Starting Zerikai Memory MCP Server in SSE Mode on port 8200..."
exec python main.py --sse
