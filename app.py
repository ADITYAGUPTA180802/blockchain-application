from __future__ import annotations
import hashlib
import json
from time import time
from uuid import uuid4
from urllib.parse import urlparse
import sqlite3
import requests
from flask import Flask, jsonify, request, send_from_directory

# ---------------- SQLite Persistence ----------------
DB_PATH = "blockchain.db"

def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    con = db(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS blocks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idx INTEGER, ts REAL, proof INTEGER, previous_hash TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS txs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        block_idx INTEGER, sender TEXT, recipient TEXT, amount REAL
    )""")
    con.commit(); con.close()

def save_block(block: dict):
    con = db(); cur = con.cursor()
    cur.execute("INSERT INTO blocks(idx, ts, proof, previous_hash) VALUES(?,?,?,?)",
                (block["index"], block["timestamp"], block["proof"], block["previous_hash"]))
    for t in block["transactions"]:
        cur.execute("INSERT INTO txs(block_idx, sender, recipient, amount) VALUES(?,?,?,?)",
                    (block["index"], t["sender"], t["recipient"], t["amount"]))
    con.commit(); con.close()

def load_chain_from_db() -> list[dict]:
    con = db(); cur = con.cursor()
    cur.execute("SELECT idx, ts, proof, previous_hash FROM blocks ORDER BY idx ASC")
    rows = cur.fetchall()
    chain: list[dict] = []
    for idx, ts, proof, prev in rows:
        cur.execute("SELECT sender, recipient, amount FROM txs WHERE block_idx=? ORDER BY id ASC", (idx,))
        txs = [{"sender": s, "recipient": r, "amount": a} for (s, r, a) in cur.fetchall()]
        chain.append({"index": idx, "timestamp": ts, "transactions": txs, "proof": proof, "previous_hash": prev})
    con.close()
    return chain

# ---------------- Flask ----------------
app = Flask(__name__)
node_identifier = str(uuid4()).replace("-", "")

# ---------------- Blockchain ----------------
class Blockchain:
    def __init__(self):
        self.current_transactions: list[dict] = []
        self.chain: list[dict] = []
        self.nodes: set[str] = set()
        # NOTE: we DO NOT create the genesis block here.
        # Startup will decide: load from DB or create genesis once.

    def new_block(self, proof: int, previous_hash: str | None = None) -> dict:
        block = {
            "index": len(self.chain) + 1,
            "timestamp": time(),
            "transactions": self.current_transactions,
            "proof": proof,
            "previous_hash": previous_hash or self.hash(self.chain[-1]),
        }
        self.current_transactions = []
        self.chain.append(block)
        save_block(block)  # persist every block
        return block

    def new_transaction(self, sender: str, recipient: str, amount: float) -> int:
        self.current_transactions.append({"sender": sender, "recipient": recipient, "amount": amount})
        return (self.last_block["index"] + 1) if self.chain else 1

    @property
    def last_block(self) -> dict:
        return self.chain[-1]

    @staticmethod
    def hash(block: dict) -> str:
        block_string = json.dumps(block, sort_keys=True).encode()
        return hashlib.sha256(block_string).hexdigest()

    def proof_of_work(self, last_proof: int) -> int:
        proof = 0
        while not self.valid_proof(last_proof, proof):
            proof += 1
        return proof

    @staticmethod
    def valid_proof(last_proof: int, proof: int) -> bool:
        guess = f"{last_proof}{proof}".encode()
        guess_hash = hashlib.sha256(guess).hexdigest()
        return guess_hash.startswith("0000")

    # (Consensus & nodes: unchanged)
    def register_node(self, address: str) -> None:
        parsed = urlparse(address)
        if parsed.scheme and parsed.netloc:
            self.nodes.add(f"{parsed.scheme}://{parsed.netloc}")
        else:
            self.nodes.add(f"http://{address}")

    def valid_chain(self, chain: list[dict]) -> bool:
        if not chain:
            return False
        last_block = chain[0]
        for block in chain[1:]:
            if block["previous_hash"] != self.hash(last_block):
                return False
            if not self.valid_proof(last_block["proof"], block["proof"]):
                return False
            last_block = block
        return True

    def resolve_conflicts(self) -> bool:
        neighbors = self.nodes
        new_chain: list[dict] | None = None
        max_length = len(self.chain)
        for node in neighbors:
            try:
                response = requests.get(f"{node}/chain", timeout=5)
            except requests.RequestException:
                continue
            if response.status_code != 200:
                continue
            data = response.json()
            length = data.get("length")
            chain = data.get("chain")
            if length and chain and length > max_length and self.valid_chain(chain):
                max_length = length
                new_chain = chain
        if new_chain:
            self.chain = new_chain
            return True
        return False

# -------- Startup order: init DB, load or create genesis ------
init_db()
blockchain = Blockchain()
_db_chain = load_chain_from_db()
if _db_chain:
    # Use persisted chain; no new genesis is created
    blockchain.chain = _db_chain
else:
    # First run: create and persist genesis block
    blockchain.new_block(proof=100, previous_hash="1")

# ---------------- Routes ----------------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/transactions/new", methods=["POST"])
def new_transaction_route():
    values = request.get_json(force=True, silent=True) or {}
    required = ["sender", "recipient", "amount"]
    if not all(k in values for k in required):
        return jsonify({"error": "Missing values. Required: sender, recipient, amount"}), 400

    blockchain.new_transaction(values["sender"], values["recipient"], float(values["amount"]))
    last_proof = blockchain.last_block["proof"]
    proof = blockchain.proof_of_work(last_proof)
    blockchain.new_transaction(sender="0", recipient=node_identifier, amount=1)
    block = blockchain.new_block(proof)

    return jsonify({
        "message": "Transaction stored and block mined",
        "index": block["index"],
        "transactions": block["transactions"],
        "proof": block["proof"],
        "previous_hash": block["previous_hash"],
        "miner": node_identifier
    }), 201

@app.route("/mine", methods=["GET"])
def mine_route():
    last_proof = blockchain.last_block["proof"]
    proof = blockchain.proof_of_work(last_proof)
    blockchain.new_transaction(sender="0", recipient=node_identifier, amount=1)
    block = blockchain.new_block(proof)
    return jsonify({
        "message": "New Block Forged",
        "index": block["index"],
        "transactions": block["transactions"],
        "proof": block["proof"],
        "previous_hash": block["previous_hash"],
        "miner": node_identifier
    }), 200

@app.route("/chain", methods=["GET"])
def full_chain():
    return jsonify({"chain": blockchain.chain, "length": len(blockchain.chain)}), 200

@app.route("/nodes/register", methods=["POST"])
def register_nodes():
    values = request.get_json(force=True, silent=True) or {}
    nodes = values.get("nodes")
    if nodes is None or not isinstance(nodes, list) or not nodes:
        return jsonify({"error": "Please supply a non-empty list of node URLs in 'nodes'."}), 400
    for node in nodes:
        blockchain.register_node(node)
    return jsonify({"message": "New nodes have been added", "total_nodes": list(blockchain.nodes)}), 201

@app.route("/nodes/resolve", methods=["GET"])
def consensus():
    replaced = blockchain.resolve_conflicts()
    if replaced:
        return jsonify({"message": "Our chain was replaced", "new_chain": blockchain.chain}), 200
    else:
        return jsonify({"message": "Our chain is authoritative", "chain": blockchain.chain}), 200

# ---------------- Run ----------------
if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    app.run(host="0.0.0.0", port=port, debug=True)
