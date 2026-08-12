#!/usr/bin/env python3
"""Test script to verify backup system functionality"""
import os
import json
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import backup functions from bot
import sys
sys.path.insert(0, os.path.dirname(__file__))

print("=" * 60)
print("🔍 BACKUP SYSTEM TEST")
print("=" * 60)

# Check .env configuration
print("\n📋 Environment Variables:")
print(f"  BACKUP_FOLDER: {os.getenv('BACKUP_FOLDER', 'backups')}")
print(f"  BACKUP_INTERVAL_HOURS: {os.getenv('BACKUP_INTERVAL_HOURS', 24)}")
print(f"  KEEP_BACKUPS_DAYS: {os.getenv('KEEP_BACKUPS_DAYS', 30)}")

# Check tokens are loaded
telegram_token = os.getenv('TELEGRAM_TOKEN')
groq_key = os.getenv('GROQ_API_KEY')

print("\n🔐 Security Check:")
print(f"  ✓ TELEGRAM_TOKEN loaded: {len(telegram_token) > 0 if telegram_token else False}")
print(f"  ✓ GROQ_API_KEY loaded: {len(groq_key) > 0 if groq_key else False}")

# Check JSON files exist
print("\n📁 JSON Files to Backup:")
json_files = ["bot_data.json", "quests.json", "lang.json"]
for file in json_files:
    exists = os.path.exists(file)
    status = "✓" if exists else "✗"
    size = os.path.getsize(file) if exists else 0
    print(f"  {status} {file} ({size} bytes)")

# Check backup folder
backup_folder = os.getenv('BACKUP_FOLDER', 'backups')
print(f"\n💾 Backup Folder: {backup_folder}")
if Path(backup_folder).exists():
    backups = list(Path(backup_folder).glob("*.json"))
    print(f"  ✓ Folder exists")
    print(f"  📦 Existing backups: {len(backups)}")
    if backups:
        for backup in sorted(backups)[-5:]:  # Show last 5
            size = backup.stat().st_size
            mtime = datetime.fromtimestamp(backup.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"     - {backup.name} ({size} bytes) - {mtime}")
else:
    print(f"  ℹ Folder will be created on first backup")

print("\n✅ Test Complete!")
print("=" * 60)
