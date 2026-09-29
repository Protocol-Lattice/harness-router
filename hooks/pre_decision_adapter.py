#!/usr/bin/env python3
"""CLI adapter for harnesses that expose a user-turn/pre-prompt hook."""
import json,sys
from pathlib import Path
from hooks_pre_decision_impl import main_adapter
if __name__=="__main__": raise SystemExit(main_adapter())
