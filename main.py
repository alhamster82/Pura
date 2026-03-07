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
            sibling = idx - 1
        if sibling < len(layer):
            proof.append(layer[sibling])
        idx //= 2
    return proof

def get_merkle_root(leaves: list[bytes]) -> bytes:
    tree = build_merkle_tree(leaves)
    if not tree:
        return bytes(32)
    return tree[-1][0]

def proof_to_hex_list(proof: list[bytes]) -> list[str]:
    return ["0x" + p.hex() for p in proof]

# -----------------------------------------------------------------------------
# Contract ABI (minimal for Pura usage)
# -----------------------------------------------------------------------------

DEW_DROPS_ABI = [
    {"inputs": [{"name": "taskId", "type": "bytes32"}, {"name": "proofNonce", "type": "bytes32"}, {"name": "merkleProof", "type": "bytes32[]"}], "name": "claimDroplet", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "taskIds", "type": "bytes32[]"}, {"name": "proofNonces", "type": "bytes32[]"}, {"name": "merkleProofs", "type": "bytes32[][]"}], "name": "claimDropletBatch", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "taskId", "type": "bytes32"}], "name": "getTask", "outputs": [{"name": "taskKind", "type": "uint8"}, {"name": "rewardPerClaim", "type": "uint256"}, {"name": "endBlock", "type": "uint256"}, {"name": "merkleRoot", "type": "bytes32"}, {"name": "poolBalance", "type": "uint256"}, {"name": "disabled", "type": "bool"}, {"name": "totalClaimed", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "taskId", "type": "bytes32"}, {"name": "proofNonce", "type": "bytes32"}], "name": "hasFulfilled", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "taskId", "type": "bytes32"}], "name": "isTaskActive", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "offset", "type": "uint256"}, {"name": "limit", "type": "uint256"}], "name": "getTaskIdsPaginated", "outputs": [{"name": "out", "type": "bytes32[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "participant", "type": "address"}, {"name": "proofNonce", "type": "bytes32"}, {"name": "taskId", "type": "bytes32"}], "name": "computeLeaf", "outputs": [{"name": "", "type": "bytes32"}], "stateMutability": "pure", "type": "function"},
    {"inputs": [{"name": "taskId", "type": "bytes32"}], "name": "getVestedAmount", "outputs": [{"name": "claimable", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "taskId", "type": "bytes32"}, {"name": "proofNonce", "type": "bytes32"}, {"name": "merkleProof", "type": "bytes32[]"}], "name": "claimDropletVested", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "taskId", "type": "bytes32"}], "name": "claimVested", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [], "name": "paused", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "contractBalance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "taskCount", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "index", "type": "uint256"}], "name": "taskIdAt", "outputs": [{"name": "", "type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "userTotalClaimed", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "globalTotalClaimed", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

# -----------------------------------------------------------------------------
# Contract client (requires web3)
# -----------------------------------------------------------------------------

class DewDropsClient:
    def __init__(self, rpc_url: str, contract_address: str, private_key: Optional[str] = None):
        if not WEB3_AVAILABLE:
            raise RuntimeError("web3 and eth_account are required for contract interaction. pip install web3 eth-account")
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.contract_address = Web3.to_checksum_address(contract_address)
        self.contract = self.w3.eth.contract(address=self.contract_address, abi=DEW_DROPS_ABI)
        self.private_key = private_key
        self.account = Account.from_key(private_key) if private_key else None

    def is_connected(self) -> bool:
        return self.w3.is_connected()

    def get_task(self, task_id: str) -> tuple[int, int, int, str, int, bool, int]:
        task_id_b32 = hex_to_bytes32(task_id) if len(task_id) == 66 else task_id
        if isinstance(task_id_b32, bytes):
            task_id_hex = "0x" + task_id_b32.hex()
        else:
            task_id_hex = task_id
        return self.contract.functions.getTask(task_id_hex).call()

    def has_fulfilled(self, task_id: str, proof_nonce: str) -> bool:
        tid = hex_to_bytes32(task_id) if len(task_id) == 66 else task_id
        pn = hex_to_bytes32(proof_nonce) if len(proof_nonce) == 66 else proof_nonce
        return self.contract.functions.hasFulfilled("0x" + tid.hex() if isinstance(tid, bytes) else tid, "0x" + pn.hex() if isinstance(pn, bytes) else pn).call()

    def is_task_active(self, task_id: str) -> bool:
        tid = task_id if task_id.startswith("0x") and len(task_id) == 66 else "0x" + hex_to_bytes32(task_id).hex()
        return self.contract.functions.isTaskActive(tid).call()

    def paused(self) -> bool:
        return self.contract.functions.paused().call()

    def task_count(self) -> int:
        return self.contract.functions.taskCount().call()

    def task_id_at(self, index: int) -> str:
        return self.contract.functions.taskIdAt(index).call()

    def get_task_ids_paginated(self, offset: int, limit: int) -> list[str]:
        return self.contract.functions.getTaskIdsPaginated(offset, limit).call()

    def user_total_claimed(self, address: str) -> int:
        return self.contract.functions.userTotalClaimed(Web3.to_checksum_address(address)).call()

    def global_total_claimed(self) -> int:
        return self.contract.functions.globalTotalClaimed().call()

    def contract_balance(self) -> int:
        return self.contract.functions.contractBalance().call()

    def get_vested_amount(self, task_id: str, address: str) -> int:
        tid = task_id if task_id.startswith("0x") and len(task_id) == 66 else "0x" + hex_to_bytes32(task_id).hex()
        return self.contract.functions.getVestedAmount(tid, Web3.to_checksum_address(address)).call()

    def claim_droplet(self, task_id: str, proof_nonce: str, merkle_proof: list[str]) -> str:
        if not self.account:
            raise ValueError("Private key required for claiming")
        tid = task_id if task_id.startswith("0x") and len(task_id) == 66 else "0x" + hex_to_bytes32(task_id).hex()
        pn = proof_nonce if proof_nonce.startswith("0x") and len(proof_nonce) == 66 else "0x" + hex_to_bytes32(proof_nonce).hex()
        tx = self.contract.functions.claimDroplet(tid, pn, merkle_proof).build_transaction({
            "from": self.account.address,
            "gas": 200_000,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self.account.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def claim_droplet_batch(self, task_ids: list[str], proof_nonces: list[str], merkle_proofs: list[list[str]]) -> str:
        if not self.account:
            raise ValueError("Private key required for claiming")
        tid_hex = [t if t.startswith("0x") and len(t) == 66 else "0x" + hex_to_bytes32(t).hex() for t in task_ids]
        pn_hex = [p if p.startswith("0x") and len(p) == 66 else "0x" + hex_to_bytes32(p).hex() for p in proof_nonces]
        tx = self.contract.functions.claimDropletBatch(tid_hex, pn_hex, merkle_proofs).build_transaction({
            "from": self.account.address,
            "gas": 500_000,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self.account.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def claim_vested(self, task_id: str) -> str:
        if not self.account:
            raise ValueError("Private key required")
        tid = task_id if task_id.startswith("0x") and len(task_id) == 66 else "0x" + hex_to_bytes32(task_id).hex()
        tx = self.contract.functions.claimVested(tid).build_transaction({
            "from": self.account.address,
            "gas": 150_000,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self.account.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

# -----------------------------------------------------------------------------
# Leaves / eligibility list handling
# -----------------------------------------------------------------------------

def load_leaves(path: str) -> list[dict[str, str]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return data.get("leaves", data.get("entries", []))

def save_leaves(path: str, leaves: list[dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"leaves": leaves}, f, indent=2)

def load_tasks(path: str) -> list[dict[str, Any]]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("tasks", [])

def save_tasks(path: str, tasks: list[dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, indent=2)

# -----------------------------------------------------------------------------
# CLI: list tasks
# -----------------------------------------------------------------------------

def cmd_list_tasks(config: PuraConfig, limit: int = 20) -> None:
    if not WEB3_AVAILABLE or not config.contract_address:
        LOG.warning("Set contract_address in config and install web3 to list tasks")
        return
    client = DewDropsClient(config.effective_rpc, config.contract_address)
    if not client.is_connected():
        LOG.error("RPC not connected")
        return
    if client.paused():
        LOG.warning("Contract is paused")
    n = client.task_count()
    LOG.info("Total tasks: %s", n)
    for i in range(min(n, limit)):
        tid = client.task_id_at(i)
        try:
            kind, reward, end_block, root, pool, disabled, claimed = client.get_task(tid)
            active = client.is_task_active(tid)
            name = TASK_KIND_NAMES[kind] if kind < len(TASK_KIND_NAMES) else "custom"
            LOG.info("  [%s] %s reward=%s end=%s pool=%s active=%s disabled=%s", tid[:18], name, reward, end_block, pool, active, disabled)
        except Exception as e:
            LOG.debug("task %s: %s", tid, e)

# -----------------------------------------------------------------------------
# CLI: build merkle and save
# -----------------------------------------------------------------------------

def cmd_build_merkle(config: PuraConfig, task_id: str, leaves_path: Optional[str] = None) -> None:
    path = leaves_path or config.config_path(config.leaves_file)
    leaves_data = load_leaves(path)
    if not leaves_data:
        LOG.error("No leaves in %s", path)
        return
    task_id_hex = task_id if task_id.startswith("0x") and len(task_id) == 66 else "0x" + hex_to_bytes32(task_id).hex()
    leaves: list[bytes] = []
    for entry in leaves_data:
        addr = entry.get("address", entry.get("participant", ""))
        nonce = entry.get("proofNonce", entry.get("nonce", "0x" + "0" * 64))
        leaf = build_leaf(addr, nonce, task_id_hex)
        leaves.append(leaf)
    root = get_merkle_root(leaves)
    out_path = config.config_path(config.merkle_file)
    config.ensure_config_dir()
    output = {
        "taskId": task_id_hex,
        "merkleRoot": "0x" + root.hex(),
        "numLeaves": len(leaves),
        "proofs": [],
    }
    for i, entry in enumerate(leaves_data):
        proof = get_merkle_proof(leaves, i)
        output["proofs"].append({
            "address": entry.get("address", entry.get("participant", "")),
            "proofNonce": entry.get("proofNonce", entry.get("nonce", "0x" + "0" * 64)),
            "merkleProof": proof_to_hex_list(proof),
        })
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    LOG.info("Merkle root %s saved to %s (%s leaves)", output["merkleRoot"], out_path, len(leaves))

# -----------------------------------------------------------------------------
# CLI: claim single
# -----------------------------------------------------------------------------

def cmd_claim(config: PuraConfig, task_id: str, proof_nonce: str, merkle_path: Optional[str] = None) -> None:
    if not config.private_key or not config.contract_address:
        LOG.error("Set private_key and contract_address in config")
        return
    merkle_path = merkle_path or config.config_path(config.merkle_file)
    if not os.path.isfile(merkle_path):
        LOG.error("Merkle file not found: %s. Run build-merkle first.", merkle_path)
        return
    with open(merkle_path, encoding="utf-8") as f:
        data = json.load(f)
    proofs = data.get("proofs", [])
    task_id_hex = task_id if task_id.startswith("0x") and len(task_id) == 66 else "0x" + hex_to_bytes32(task_id).hex()
    pn_hex = proof_nonce if proof_nonce.startswith("0x") and len(proof_nonce) == 66 else "0x" + hex_to_bytes32(proof_nonce).hex()
    if not WEB3_AVAILABLE:
        LOG.error("web3 required for claim")
        return
    client = DewDropsClient(config.effective_rpc, config.contract_address, config.private_key)
    if client.paused():
        LOG.error("Contract is paused")
        return
    for p in proofs:
        if p.get("proofNonce", p.get("nonce")) == pn_hex and p.get("address", p.get("participant")) == client.account.address:
            tx_hash = client.claim_droplet(task_id_hex, pn_hex, p.get("merkleProof", []))
            LOG.info("Claim tx: %s", tx_hash)
            return
    LOG.error("No proof found for this address and proofNonce in %s", merkle_path)

# -----------------------------------------------------------------------------
# CLI: claim vested
# -----------------------------------------------------------------------------

def cmd_claim_vested(config: PuraConfig, task_id: str) -> None:
    if not config.private_key or not config.contract_address:
        LOG.error("Set private_key and contract_address in config")
        return
    if not WEB3_AVAILABLE:
        LOG.error("web3 required")
        return
    client = DewDropsClient(config.effective_rpc, config.contract_address, config.private_key)
    amount = client.get_vested_amount(task_id, client.account.address)
    if amount == 0:
        LOG.warning("No vested amount to claim for this task")
        return
    tx_hash = client.claim_vested(task_id)
    LOG.info("Claim vested tx: %s", tx_hash)

# -----------------------------------------------------------------------------
# CLI: info (user stats, global stats)
# -----------------------------------------------------------------------------

def cmd_info(config: PuraConfig, address: Optional[str] = None) -> None:
    if not config.contract_address or not WEB3_AVAILABLE:
        LOG.warning("contract_address and web3 required")
        return
    client = DewDropsClient(config.effective_rpc, config.contract_address)
    if not client.is_connected():
        LOG.error("RPC not connected")
        return
    global_claimed = client.global_total_claimed()
    balance = client.contract_balance()
    task_count = client.task_count()
    LOG.info("Contract balance: %s wei | Global claimed: %s wei | Tasks: %s", balance, global_claimed, task_count)
    addr = address or (client.account.address if config.private_key and client.account else None)
    if addr:
        if not client.account and not address:
            client = DewDropsClient(config.effective_rpc, config.contract_address)
        user_claimed = client.user_total_claimed(addr)
        LOG.info("User %s total claimed: %s wei", addr[:16], user_claimed)

# -----------------------------------------------------------------------------
# CLI: init config
# -----------------------------------------------------------------------------

def cmd_init_config(config: PuraConfig, chain: str = "sepolia", contract: Optional[str] = None, rpc: Optional[str] = None) -> None:
    config.chain = chain
    if contract:
        config.contract_address = contract
    if rpc:
        config.rpc_url = rpc
    config.save()
    LOG.info("Config saved to %s", config.config_path(DEFAULT_CONFIG_FILE))

# -----------------------------------------------------------------------------
# Validation helpers
# -----------------------------------------------------------------------------

def validate_address(addr: str) -> bool:
    if not addr or len(addr) < 40:
        return False
    if addr.startswith("0x"):
        addr = addr[2:]
    return len(addr) == 40 and all(c in "0123456789abcdefABCDEF" for c in addr)

def validate_bytes32(s: str) -> bool:
    if s.startswith("0x"):
        s = s[2:]
    return len(s) == 64 and all(c in "0123456789abcdefABCDEF" for c in s)

def validate_task_kind(kind: int) -> bool:
    return 0 <= kind <= MAX_TASK_KIND

# -----------------------------------------------------------------------------
# Task kind utilities
# -----------------------------------------------------------------------------

def task_kind_to_name(kind: int) -> str:
    return TASK_KIND_NAMES[kind] if 0 <= kind < len(TASK_KIND_NAMES) else "custom"

def task_name_to_kind(name: str) -> int:
    name = name.lower().strip()
    for i, n in enumerate(TASK_KIND_NAMES):
        if n == name:
            return i
    return len(TASK_KIND_NAMES) - 1  # custom

def list_task_kinds() -> list[tuple[int, str]]:
    return list(enumerate(TASK_KIND_NAMES))

# -----------------------------------------------------------------------------
# Wei / Ether formatting
# -----------------------------------------------------------------------------

def wei_to_ether(wei: int) -> float:
    return wei / 1e18

def ether_to_wei(ether: float) -> int:
    return int(ether * 1e18)

def format_wei(wei: int) -> str:
    if wei >= 1e18:
        return f"{wei_to_ether(wei):.4f} ETH"
    return f"{wei} wei"

# -----------------------------------------------------------------------------
# Sample leaves generator (for testing)
# -----------------------------------------------------------------------------

def generate_sample_leaves(task_id: str, addresses: list[str], nonce_prefix: str = "0x") -> list[dict[str, str]]:
    out = []
    for i, addr in enumerate(addresses):
        nonce = f"{nonce_prefix}{i:064x}" if len(nonce_prefix) <= 2 else nonce_prefix + f"{i:064x}"
        out.append({"address": addr, "proofNonce": nonce})
    return out

# -----------------------------------------------------------------------------
# Export merkle root and proofs to contract-ready format
# -----------------------------------------------------------------------------

def export_merkle_for_guardian(leaves_path: str, task_id: str, output_path: Optional[str] = None) -> dict[str, Any]:
    leaves_data = load_leaves(leaves_path)
    task_id_hex = task_id if task_id.startswith("0x") and len(task_id) == 66 else "0x" + hex_to_bytes32(task_id).hex()
    leaves = [build_leaf(e.get("address", e.get("participant", "")), e.get("proofNonce", e.get("nonce", "0x" + "0" * 64)), task_id_hex) for e in leaves_data]
    root = get_merkle_root(leaves)
    proofs = []
    for i, entry in enumerate(leaves_data):
        proof = get_merkle_proof(leaves, i)
        proofs.append({
            "address": entry.get("address", entry.get("participant", "")),
            "proofNonce": entry.get("proofNonce", entry.get("nonce", "0x" + "0" * 64)),
            "merkleProof": proof_to_hex_list(proof),
        })
    result = {"taskId": task_id_hex, "merkleRoot": "0x" + root.hex(), "numLeaves": len(leaves), "proofs": proofs}
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    return result

# -----------------------------------------------------------------------------
# Check eligibility from leaves file
# -----------------------------------------------------------------------------

def check_eligibility(leaves_path: str, task_id: str, address: str, proof_nonce: Optional[str] = None) -> Optional[dict[str, Any]]:
    leaves_data = load_leaves(leaves_path)
    task_id_hex = task_id if task_id.startswith("0x") and len(task_id) == 66 else "0x" + hex_to_bytes32(task_id).hex()
    for i, entry in enumerate(leaves_data):
        addr = entry.get("address", entry.get("participant", ""))
        if addr.lower() != address.lower():
            continue
        nonce = entry.get("proofNonce", entry.get("nonce", "0x" + "0" * 64))
        if proof_nonce and nonce != proof_nonce:
            continue
        leaves = [build_leaf(e.get("address", e.get("participant", "")), e.get("proofNonce", e.get("nonce", "0x" + "0" * 64)), task_id_hex) for e in leaves_data]
        proof = get_merkle_proof(leaves, i)
        return {"address": addr, "proofNonce": nonce, "merkleProof": proof_to_hex_list(proof), "index": i}
    return None

# -----------------------------------------------------------------------------
# Batch claim from merkle file (all eligible for one address)
# -----------------------------------------------------------------------------

def get_all_proofs_for_address(merkle_path: str, address: str) -> list[dict[str, Any]]:
    with open(merkle_path, encoding="utf-8") as f:
        data = json.load(f)
    proofs = data.get("proofs", [])
    task_id = data.get("taskId", "")
    return [p for p in proofs if p.get("address", p.get("participant", "")).lower() == address.lower()]

# -----------------------------------------------------------------------------
# CLI: check-eligibility
# -----------------------------------------------------------------------------

def cmd_check_eligibility(config: PuraConfig, task_id: str, address: str, leaves_path: Optional[str] = None) -> None:
    path = leaves_path or config.config_path(config.leaves_file)
    if not os.path.isfile(path):
        LOG.error("Leaves file not found: %s", path)
        return
    result = check_eligibility(path, task_id, address)
    if result:
        LOG.info("Eligible: address=%s proofNonce=%s proofLen=%s", address, result["proofNonce"], len(result["merkleProof"]))
    else:
        LOG.info("Not found in leaves for this task")

# -----------------------------------------------------------------------------
# CLI: export-merkle
# -----------------------------------------------------------------------------

def cmd_export_merkle(config: PuraConfig, task_id: str, leaves_path: Optional[str] = None, output: Optional[str] = None) -> None:
    path = leaves_path or config.config_path(config.leaves_file)
    out = output or config.config_path(config.merkle_file)
    if not os.path.isfile(path):
        LOG.error("Leaves file not found: %s", path)
        return
    export_merkle_for_guardian(path, task_id, out)
    LOG.info("Exported merkle to %s", out)

