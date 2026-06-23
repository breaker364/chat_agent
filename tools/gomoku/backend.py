#!/usr/bin/env python3
"""
Gomoku (五子棋) Backend Server
Serves static frontend files and provides game API.
"""

import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Game logic
class GomokuGame:
    def __init__(self, size=15):
        self.size = size
        self.board = [[0] * size for _ in range(size)]
        self.current_player = 1  # 1 = black, 2 = white
        self.move_history = []
        self.winner = 0
        self.game_over = False

    def place_stone(self, row, col):
        if self.game_over:
            return {"success": False, "message": "Game over"}
        if not (0 <= row < self.size and 0 <= col < self.size):
            return {"success": False, "message": "Out of bounds"}
        if self.board[row][col] != 0:
            return {"success": False, "message": "Already occupied"}

        self.board[row][col] = self.current_player
        self.move_history.append((row, col, self.current_player))

        if self.check_win(row, col, self.current_player):
            self.winner = self.current_player
            self.game_over = True
            return {"success": True, "winner": self.current_player, "message": f"{'Black' if self.current_player == 1 else 'White'} wins!"}

        if len(self.move_history) == self.size * self.size:
            self.game_over = True
            return {"success": True, "winner": 0, "message": "Draw!"}

        self.current_player = 3 - self.current_player
        return {"success": True, "winner": 0, "message": "OK"}

    def check_win(self, row, col, player):
        directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
        for dr, dc in directions:
            count = 1
            r, c = row + dr, col + dc
            while 0 <= r < self.size and 0 <= c < self.size and self.board[r][c] == player:
                count += 1
                r += dr
                c += dc
            r, c = row - dr, col - dc
            while 0 <= r < self.size and 0 <= c < self.size and self.board[r][c] == player:
                count += 1
                r -= dr
                c -= dc
            if count >= 5:
                return True
        return False

    def get_state(self):
        return {
            "board": self.board,
            "current_player": self.current_player,
            "winner": self.winner,
            "game_over": self.game_over,
            "move_history": self.move_history,
            "size": self.size
        }

    def reset(self):
        self.__init__(self.size)

    def undo(self):
        if not self.move_history:
            return {"success": False, "message": "No moves to undo"}
        row, col, player = self.move_history.pop()
        self.board[row][col] = 0
        self.current_player = player
        self.winner = 0
        self.game_over = False
        return {"success": True, "message": "Undone"}


game = GomokuGame()

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/state":
            self._send_json(game.get_state())
            return
        elif path == "/api/reset":
            game.reset()
            self._send_json({"success": True, "message": "Reset"})
            return

        file_path = path.lstrip("/")
        if file_path == "" or file_path == "index.html":
            file_path = "index.html"

        full_path = os.path.join(FRONTEND_DIR, file_path)
        if os.path.isfile(full_path):
            ext = os.path.splitext(full_path)[1]
            content_types = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".ico": "image/x-icon",
            }
            ctype = content_types.get(ext, "application/octet-stream")
            try:
                with open(full_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self._send_error(500, f"Read failed: {str(e)}")
        else:
            full_path = os.path.join(FRONTEND_DIR, "index.html")
            if os.path.isfile(full_path):
                with open(full_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(content)
            else:
                self._send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        data = json.loads(body) if body else {}

        if path == "/api/place":
            row = data.get("row")
            col = data.get("col")
            if row is None or col is None:
                self._send_json({"success": False, "message": "Missing row/col"})
                return
            result = game.place_stone(int(row), int(col))
            self._send_json(result)
            return
        elif path == "/api/undo":
            result = game.undo()
            self._send_json(result)
            return
        elif path == "/api/reset":
            game.reset()
            self._send_json({"success": True, "message": "Reset"})
            return

        self._send_error(404, "API not found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _send_json(self, data):
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content)

    def _send_error(self, code, message):
        self.send_response(code)
        content = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        try:
            msg = format % args
            print(f"[{self.address_string()}] {msg}")
        except:
            pass


def main():
    global FRONTEND_DIR
    script_dir = os.path.dirname(os.path.abspath(__file__))
    FRONTEND_DIR = os.path.join(script_dir, "frontend")

    if not os.path.isdir(FRONTEND_DIR):
        os.makedirs(FRONTEND_DIR, exist_ok=True)

    port = 8888
    server = HTTPServer(("0.0.0.0", port), RequestHandler)
    print("[Gomoku Server] Started on http://localhost:%d" % port)
    print("[Gomoku Server] Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Gomoku Server] Stopped")
        server.server_close()


if __name__ == "__main__":
    main()
