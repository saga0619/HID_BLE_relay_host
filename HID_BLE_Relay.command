#!/bin/bash
# Double-click launcher for HID BLE Relay on macOS.
# A Terminal window opens alongside the app to show BLE / camera logs;
# closing it quits the app.
cd "$(dirname "$0")"
exec /usr/bin/env python3 main.py
