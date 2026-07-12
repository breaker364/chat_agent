"""
Checkers game Flask backend.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from checkers import CheckersGame

app = Flask(__name__, static_folder="../static", static_url_path="")
CORS(app)

game = CheckersGame()


def _load_game_config() -> dict:
    config_path = Path(__file__).resolve().parents[2] / "runtime_config.json"
    if not config_path.exists():
        return {}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return payload.get("checkers_game", {}) if isinstance(payload, dict) else {}


@app.route("/")
def index():
    """Serve the game frontend."""
    return send_from_directory("../static", "index.html")


@app.route("/api/game", methods=["GET"])
def get_game():
    """Return current game state."""
    return jsonify(game.to_dict())


@app.route("/api/select", methods=["POST"])
def select_piece():
    """Select a piece."""
    data = request.get_json()
    row, col = data.get("row"), data.get("col")
    if row is None or col is None:
        return jsonify({"error": "Missing row or col parameter"}), 400
    result = game.select_piece(row, col)
    return jsonify({**result, **game.to_dict()})


@app.route("/api/move", methods=["POST"])
def move_piece():
    """Move a piece."""
    data = request.get_json()
    from_row, from_col = data.get("from_row"), data.get("from_col")
    to_row, to_col = data.get("to_row"), data.get("to_col")
    if any(value is None for value in [from_row, from_col, to_row, to_col]):
        return jsonify({"error": "Missing move parameters"}), 400
    result = game.make_move(from_row, from_col, to_row, to_col)
    return jsonify({**result, **game.to_dict()})


@app.route("/api/reset", methods=["POST"])
def reset_game():
    """Reset the game."""
    game.reset()
    return jsonify({"success": True, **game.to_dict()})


@app.route("/api/ai", methods=["POST"])
def ai_turn():
    """Let the AI make one move."""
    result = game.ai_move()
    return jsonify({**result, **game.to_dict()})


if __name__ == "__main__":
    game_config = _load_game_config()
    host = game_config.get("backend_host")
    port = int(game_config.get("backend_port"))
    api_base_url = game_config.get("api_base_url")
    print("=" * 50)
    print("Checkers server started")
    print(f"Visit {api_base_url} to start")
    print("=" * 50)
    app.run(host=host, port=port, debug=True)
