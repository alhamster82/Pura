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
    name: str

CHAINS: dict[str, ChainConfig] = {
    "mainnet": ChainConfig(1, DEFAULT_RPC_MAINNET, "Ethereum Mainnet"),
    "sepolia": ChainConfig(11155111, DEFAULT_RPC_SEPOLIA, "Sepolia"),
}

@dataclass
class PuraConfig:
    chain: str = "sepolia"
    rpc_url: Optional[str] = None
    contract_address: Optional[str] = None
    private_key: Optional[str] = None
    config_dir: str = DEFAULT_CONFIG_DIR
    leaves_file: str = DEFAULT_LEAVES_FILE
    merkle_file: str = DEFAULT_MERKLE_FILE
    tasks_file: str = DEFAULT_TASKS_FILE
    gas_limit_claim: int = 200_000
    gas_limit_batch: int = 500_000
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_rpc(self) -> str:
        return self.rpc_url or CHAINS.get(self.chain, CHAINS["sepolia"]).rpc_url

    @property
    def chain_id(self) -> int:
        return CHAINS.get(self.chain, CHAINS["sepolia"]).chain_id

    def config_path(self, name: str) -> str:
        return os.path.join(self.config_dir, name)

    def ensure_config_dir(self) -> None:
        Path(self.config_dir).mkdir(parents=True, exist_ok=True)

    def save(self, path: Optional[str] = None) -> None:
        self.ensure_config_dir()
        p = path or self.config_path(DEFAULT_CONFIG_FILE)
        data = {
            "chain": self.chain,
            "rpc_url": self.rpc_url,
            "contract_address": self.contract_address,
            "gas_limit_claim": self.gas_limit_claim,
            "gas_limit_batch": self.gas_limit_batch,
            **self.extra,
        }
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "PuraConfig":
        config_dir = path or os.path.join(DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_FILE)
        if os.path.isfile(config_dir):
            with open(config_dir, encoding="utf-8") as f:
                data = json.load(f)
            return cls(
                chain=data.get("chain", "sepolia"),
                rpc_url=data.get("rpc_url"),
                contract_address=data.get("contract_address"),
                private_key=data.get("private_key"),
                config_dir=os.path.dirname(config_dir) or DEFAULT_CONFIG_DIR,
                gas_limit_claim=data.get("gas_limit_claim", 200_000),
                gas_limit_batch=data.get("gas_limit_batch", 500_000),
                extra={k: v for k, v in data.items() if k not in ("chain", "rpc_url", "contract_address", "private_key", "gas_limit_claim", "gas_limit_batch")},
            )
        return cls(config_dir=os.path.dirname(config_dir) or DEFAULT_CONFIG_DIR)

# -----------------------------------------------------------------------------
# Keccak (for leaf hash) — use pycryptodome or web3 if available
# -----------------------------------------------------------------------------

def keccak256(data: bytes) -> bytes:
    if WEB3_AVAILABLE:
        return Web3.solidity_keccak(["bytes"], [data])
    try:
        from Crypto.Hash import keccak
