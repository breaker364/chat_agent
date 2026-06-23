"""
跳棋游戏 Flask 后端服务器
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from checkers import CheckersGame

app = Flask(__name__, static_folder='../static', static_url_path='')
CORS(app)

# 全局游戏实例
game = CheckersGame()


@app.route('/')
def index():
    """提供前端页面"""
    return send_from_directory('../static', 'index.html')


@app.route('/api/game', methods=['GET'])
def get_game():
    """获取当前游戏状态"""
    return jsonify(game.to_dict())


@app.route('/api/select', methods=['POST'])
def select_piece():
    """选择棋子"""
    data = request.get_json()
    r, c = data.get('row'), data.get('col')
    if r is None or c is None:
        return jsonify({'error': '缺少 row 或 col 参数'}), 400
    result = game.select_piece(r, c)
    return jsonify({**result, **game.to_dict()})


@app.route('/api/move', methods=['POST'])
def move_piece():
    """移动棋子"""
    data = request.get_json()
    from_r, from_c = data.get('from_row'), data.get('from_col')
    to_r, to_c = data.get('to_row'), data.get('to_col')
    if any(v is None for v in [from_r, from_c, to_r, to_c]):
        return jsonify({'error': '缺少移动参数'}), 400
    result = game.make_move(from_r, from_c, to_r, to_c)
    return jsonify({**result, **game.to_dict()})


@app.route('/api/reset', methods=['POST'])
def reset_game():
    """重置游戏"""
    game.reset()
    return jsonify({'success': True, **game.to_dict()})


@app.route('/api/ai', methods=['POST'])
def ai_turn():
    """AI 走一步"""
    from threading import Timer
    result = game.ai_move()
    return jsonify({**result, **game.to_dict()})


if __name__ == '__main__':
    print("=" * 50)
    print("  跳棋游戏服务器启动！")
    print("  访问 http://localhost:5000 开始游戏")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000, debug=True)
