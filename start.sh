#!/bin/bash
# Start the FastAPI app in the background
# Using port 8848 to match the strategy name ;)
echo "Starting 8848 Strategy Analyzer on port 8848..."
nohup uv run uvicorn app:app --host 0.0.0.0 --port 8848 > app.log 2>&1 &
echo "Application is running in background. PID: $!"
echo "Access it at http://localhost:8848"
