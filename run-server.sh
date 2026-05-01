#!/bin/bash
# Self-healing watchdog for the Prismatica dev server.
# Lives in the project so it survives /tmp wipes on reboot.
# Restarts python3 serve.py whenever it dies. Exits the loop only on SIGTERM/SIGINT.

cd "$(dirname "$0")"
trap 'echo "watchdog exiting"; exit 0' TERM INT
while true; do
  echo "[$(date +%T)] starting serve.py"
  python3 serve.py
  echo "[$(date +%T)] serve.py exited with $? — restarting in 1s"
  sleep 1
done
