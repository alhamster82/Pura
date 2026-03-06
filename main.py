#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pura — Dew-Drops airdrop companion app.
Find your inner dew: complete social tasks and claim droplets from the DewDrops contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

# Optional web3; graceful fallback if not installed
try:
    from web3 import Web3
    from eth_account import Account
    from eth_account.messages import encode_defunct
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False
    Web3 = None
    Account = None

# -----------------------------------------------------------------------------
# Constants — aligned with DewDrops.sol
# -----------------------------------------------------------------------------

APP_NAME = "Pura"
APP_VERSION = "2.0.0"
DEW_NAMESPACE_HEX = "0x8f3a2b1c9d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d"
DOMAIN_SEED_STR = "DewDrops.Mist.v2"
MAX_CLAIM_BATCH = 88
PAGE_SIZE = 50
MAX_TASK_KIND = 12

TASK_KIND_NAMES = [
    "twitter", "discord", "telegram", "retweet", "quote", "like",
    "comment", "join", "share", "watch", "follow", "custom",
]

DEFAULT_RPC_MAINNET = "https://eth.llamarpc.com"
DEFAULT_RPC_SEPOLIA = "https://rpc.sepolia.org"
DEFAULT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".pura")
DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_LEAVES_FILE = "leaves.json"
DEFAULT_MERKLE_FILE = "merkle.json"
DEFAULT_TASKS_FILE = "tasks.json"

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper()), format=fmt, handlers=handlers)

LOG = logging.getLogger("pura")

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

@dataclass
class ChainConfig:
    chain_id: int
    rpc_url: str
