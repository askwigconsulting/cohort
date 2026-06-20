---
name: secret-scan
kind: hook
scope: global
description: Scan writes for secrets before they hit disk.
targets: [claude]
event: pre_write
action: python3 "$HOME/.cohort/bin/secret_scan.py"
---
Before any Write/Edit, scan the payload for credential-shaped strings.
