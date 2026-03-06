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
        k = keccak.new(digest_bits=256)
        k.update(data)
        return k.digest()
    except ImportError:
        return hashlib.sha256(data).digest()

def bytes32_to_hex(b: bytes) -> str:
    return "0x" + b.hex() if len(b) <= 32 else "0x" + b[:32].hex()

def hex_to_bytes32(s: str) -> bytes:
    if s.startswith("0x"):
        s = s[2:]
    return bytes.fromhex(s.zfill(64))[:32]

def address_to_bytes(addr: str) -> bytes:
    if addr.startswith("0x"):
        addr = addr[2:]
    return bytes.fromhex(addr.zfill(40))

# -----------------------------------------------------------------------------
# Leaf and Merkle (DewDrops domain)
# -----------------------------------------------------------------------------

DOMAIN_SEED_BYTES = keccak256(DOMAIN_SEED_STR.encode()) if not WEB3_AVAILABLE else None

def build_leaf(participant: str, proof_nonce: str, task_id: str) -> bytes:
    """Build leaf hash: keccak256(abi.encodePacked(participant, proofNonce, taskId, DOMAIN_SEED))."""
    participant_b = address_to_bytes(participant)
    proof_nonce_b = hex_to_bytes32(proof_nonce) if isinstance(proof_nonce, str) else (proof_nonce if len(proof_nonce) == 32 else proof_nonce[:32])
    task_id_b = hex_to_bytes32(task_id) if isinstance(task_id, str) else (task_id if len(task_id) == 32 else task_id[:32])
    if WEB3_AVAILABLE:
        domain_seed = Web3.solidity_keccak(["string"], [DOMAIN_SEED_STR])
    else:
        domain_seed = hashlib.sha256(DOMAIN_SEED_STR.encode()).digest()
    payload = participant_b + proof_nonce_b + task_id_b + domain_seed
    return keccak256(payload)

def build_leaf_hex(participant: str, proof_nonce: str, task_id: str) -> str:
    return "0x" + build_leaf(participant, proof_nonce, task_id).hex()

# -----------------------------------------------------------------------------
# Merkle tree build and proof
# -----------------------------------------------------------------------------

def merkle_parent(left: bytes, right: bytes) -> bytes:
    if left < right:
        return keccak256(left + right)
    return keccak256(right + left)

def build_merkle_tree(leaves: list[bytes]) -> list[list[bytes]]:
    if not leaves:
        return []
    tree: list[list[bytes]] = [list(leaves)]
    layer = list(leaves)
    while len(layer) > 1:
        next_layer: list[bytes] = []
        for i in range(0, len(layer), 2):
            if i + 1 < len(layer):
                next_layer.append(merkle_parent(layer[i], layer[i + 1]))
            else:
                next_layer.append(layer[i])
        tree.append(next_layer)
        layer = next_layer
    return tree

def get_merkle_proof(leaves: list[bytes], index: int) -> list[bytes]:
    tree = build_merkle_tree(leaves)
    if index < 0 or index >= len(leaves):
        return []
    proof: list[bytes] = []
    idx = index
    for level in range(len(tree) - 1):
        layer = tree[level]
        if idx % 2 == 0:
            sibling = idx + 1
        else:
