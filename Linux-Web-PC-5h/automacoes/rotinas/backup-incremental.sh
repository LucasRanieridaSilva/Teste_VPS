#!/usr/bin/env bash
set -euo pipefail
"${BACKUP_PYTHON:-python3}" scripts/incremental_backup.py
