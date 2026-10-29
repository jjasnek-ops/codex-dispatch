#!/usr/bin/env python3
"""Tiny interactive worker used only by dispatch health tests."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


if os.environ.get("FAKE_CODEX_UPDATE_ONCE"):
    marker = Path(os.environ["FAKE_CODEX_UPDATE_ONCE"])
    if not marker.exists():
        marker.write_text("updated\n")
        print("Updating Codex via npm install -g @openai/codex...", flush=True)
        print("Update ran successfully! Please restart Codex.", flush=True)
        raise SystemExit(0)

print("OpenAI Codex", flush=True)
print("Tip: fake worker ready", flush=True)
if os.environ.get("FAKE_CODEX_CAPACITY"):
    print("Selected model is at capacity. Please try a different model.", flush=True)

for line in sys.stdin:
    if line.strip():
        print("fake prompt accepted", flush=True)
        print("• Working (1s • esc to interrupt)", flush=True)
    time.sleep(0.1)
