from __future__ import annotations
import hashlib
import json
from time import time
from uuid import uuid4
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, send_from_directory

# ---------------- Flask app must exist before using @app.route ----------------
app = Flask(__name__)

# Unique id for this node (used as the mining reward recipient)
node_identifier = str(uuid4()).replace("-", "")

# ----------------------------- Blockchain ------------------------------------
class Blockchain:
    def __init__(self):
        self.current_transactions: list[dict] = []   # pending txns
        self.chain: list[dict] = []                  # list of blocks
        self.nodes: set[str] = set()                 # peer node URLs

        # Genesis block
        self.new_block(proof=100, previous_hash="1")

    # -------- Blocks & Transactions --------
    def new_block(self, proof: int, previous_hash: str | None = None) -> dict:
        """
        Create a new block and add it to the chain.
        """
        block = {
            "index": len(self.chain) + 1,
            "timestamp": time(),
            "transactions": self.current_transactions,
            "proof": proof,
            "previous_hash": previous_hash or self.hash(self.chain[-1]),
        }
        # reset pending transactions
        self.current_transactions = []
        self.chain.append(block)
        return block

    def new_transaction(self, sender: str, recipient: str, amount: float) -> int:
        """
        Add a new transaction to the list of pending transactions.
        Returns the index of the block that will hold this transaction.
        """
        self.current_transactions.append({
            "sender": sender,
            "recipient": recipient,
            "amount": amount,
        })
        return self.last_block["index"] + 1

    @property
    def last_block(self) -> dict:
        return self.chain[-1]

    @staticmethod
    def hash(block: dict) -> str:
        """
        Create a SHA-256 hash of a block (keys sorted for consistency).
        """
        block_string = json.dumps(block, sort_keys=True).encode()
        return hashlib.sha256(block_string).hexdigest()

    # -------- Proof of Work --------
    def proof_of_work(self, last_proof: int) -> int:
        """
        Simple PoW:
        Find a number 'proof' such that sha256(f"{last_proof}{proof}") begins with '0000'.
        """
        proof = 0
        while not self.valid_proof(last_proof, proof):
            proof += 1
        return proof

    @staticmethod
    def valid_proof(last_proof: int, proof: int) -> bool:
        guess = f"{last_proof}{proof}".encode()
        guess_hash = hashlib.sha256(guess).hexdigest()
        return guess_hash.startswith("0000")  # difficulty

    # -------- Networking & Consensus --------
    def register_node(self, address: str) -> None:
        """
        Add a new node by URL. Accepts 'http://host:port' or 'host:port'.
        """
        parsed = urlparse(address)
        if parsed.scheme and parsed.netloc:
            self.nodes.add(f"{parsed.scheme}://{parsed.netloc}")
        else:
            self.nodes.add(f"http://{address}")

    def valid_chain(self, chain: list[dict]) -> bool:
        """
        A chain is valid if:
        - each block's previous_hash matches the SHA-256 of the previous block
        - each block's proof satisfies PoW with previous block's proof
        """
        if not chain:
            return False

        last_block = chain[0]
        current_index = 1

        while current_index < len(chain):
            block = chain[current_index]

            # previous hash must match
            if block["previous_hash"] != self.hash(last_block):
                return False

            # proof must be valid
            if not self.valid_proof(last_block["proof"], block["proof"]):
                return False

            last_block = block
            current_index += 1

        return True

    def resolve_conflicts(self) -> bool:
        """
        Consensus: replace our chain if a neighbor has a longer valid one.
        Returns True if our chain was replaced.
        """
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


# Global blockchain instance
blockchain = Blockchain()

# --------------------------------- Routes ------------------------------------
@app.route("/")
def home():
    # Optional: simple viewer page if you created index.html
    return send_from_directory(".", "index.html")

@app.route("/transactions/new", methods=["POST"])
def new_transaction_route():
    values = request.get_json(force=True, silent=True) or {}
    required = ["sender", "recipient", "amount"]
    if not all(k in values for k in required):
        return jsonify({"error": "Missing values. Required: sender, recipient, amount"}), 400

    # 1) Add the transaction
    blockchain.new_transaction(values["sender"], values["recipient"], values["amount"])

    # 2) Auto-mine: run PoW, add reward, create block
    last_proof = blockchain.last_block["proof"]
    proof = blockchain.proof_of_work(last_proof)

    # reward the miner
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

    # reward the miner
    blockchain.new_transaction(sender="0", recipient=node_identifier, amount=1)

    block = blockchain.new_block(proof)
    return jsonify({
        "message": "New Block Forged",
        "index": block["index"],
        "transactions": block["transactions"],
        "proof": block["proof"],
        "previous_hash": block["previous_hash"],
        "miner": node_identifier,
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

# --------------------------------- Run ---------------------------------------
if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    app.run(host="0.0.0.0", port=port, debug=True)
