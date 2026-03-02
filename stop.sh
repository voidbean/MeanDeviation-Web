#!/bin/bash
# Stop the application running on port 8848
PID=$(lsof -t -i:8848)
if [ -z "$PID" ]; then
    echo "No application running on port 8848."
else
    echo "Stopping application (PID $PID)..."
    kill $PID
    echo "Stopped."
fi
