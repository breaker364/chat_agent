#!/usr/bin/env python3
"""跳棋游戏逻辑自测脚本"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checkers import CheckersGame

def test_initialization():
    """测试棋盘初始化"""
    game = CheckersGame()
    assert game.current_player == game.RED, "红方先走"
    assert game.red_count == 12, f"红方应有12枚棋子，实际{game.red_count}"
    assert game.black_count == 12, f"黑方应有12枚棋子，实际{game.black_count}"
    assert not game.game_over, "初始状态不应游戏结束"
    
    for r in range(3):
        for c in range(8):
            if (r + c) % 2 == 1:
                assert game.board[r][c] == game.BLACK, f"顶部黑格[{r}][{c}]应为黑棋"
            else:
                assert game.board[r][c] == game.EMPTY, f"白格[{r}][{c}]应为空"
    for r in range(5, 8):
        for c in range(8):
            if (r + c) % 2 == 1:
                assert game.board[r][c] == game.RED, f"底部黑格[{r}][{c}]应为红棋"
            else:
                assert game.board[r][c] == game.EMPTY, f"白格[{r}][{c}]应为空"
    
    print("[PASS] 初始化测试")
    return game

def test_basic_moves():
    """测试基本移动"""
    game = CheckersGame()
    
    # 选择红方一棋子 (5,2) 是黑格有红棋
    result = game.select_piece(5, 2)
    assert result['success'], f"选择棋子失败: {result.get('error')}"
    assert len(result['valid_moves']) > 0, "应有可移动目标"
    assert [4, 1] in result['valid_moves'] or (4, 1) in result['valid_moves'], f"应可移动到(4,1)，实际有: {result['valid_moves']}"
    assert [4, 3] in result['valid_moves'] or (4, 3) in result['valid_moves'], f"应可移动到(4,3)，实际有: {result['valid_moves']}"
    
    # 执行移动
    result = game.make_move(5, 2, 4, 1)
    assert result['success'], f"移动失败: {result.get('error')}"
    assert game.board[4][1] == game.RED, f"目标格应为红棋，实际{game.board[4][1]}"
    assert game.board[5][2] == game.EMPTY, f"原格应为空"
    assert game.current_player == game.BLACK, "移动后应切换到黑方"
    
    print("[PASS] 基本移动测试")
    return game

def test_jump():
    """测试跳跃吃子"""
    game = CheckersGame()
    # 清空棋盘，设置测试场景
    for r in range(8):
        for c in range(8):
            game.board[r][c] = game.EMPTY
    
    game.board[5][0] = game.RED
    game.board[4][1] = game.BLACK
    game.current_player = game.RED
    game.red_count = 1
    game.black_count = 1
    
    result = game.select_piece(5, 0)
    assert result['success'], f"选择棋子失败: {result.get('error')}"
    assert len(result['valid_moves']) == 0, "有跳跃时不应有普通移动"
    assert [3, 2] in result['valid_jumps'] or (3, 2) in result['valid_jumps'], f"应可跳到(3,2)，实际有: {result['valid_jumps']}"
    
    result = game.make_move(5, 0, 3, 2)
    assert result['success'], f"跳跃失败: {result.get('error')}"
    assert game.board[3][2] == game.RED, "目标格应为红棋"
    assert game.board[5][0] == game.EMPTY, "原格应为空"
    assert game.board[4][1] == game.EMPTY, "被吃棋子应消失"
    assert game.black_count == 0, f"黑方应无棋子，实际{game.black_count}"
    
    print("[PASS] 跳跃吃子测试")
    return game

def test_king_promotion():
    """测试升王"""
    game = CheckersGame()
    for r in range(8):
        for c in range(8):
            game.board[r][c] = game.EMPTY
    
    game.board[1][0] = game.RED
    game.board[6][7] = game.BLACK  # 加一个黑棋防止自动胜利
    game.current_player = game.RED
    game.red_count = 1
    game.black_count = 1
    
    result = game.make_move(1, 0, 0, 1)
    assert result['success'], f"升王移动失败: {result.get('error')}"
    assert game.board[0][1] == game.RED_KING, f"应升王为RED_KING(3)，实际{game.board[0][1]}"
    
    # 测试王可逆向移动
    game.current_player = game.RED
    result = game.select_piece(0, 1)
    assert result['success'], f"选择王失败: {result.get('error')}"
    
    can_move_down = [1, 0] in result['valid_moves'] or (1, 0) in result['valid_moves']
    can_move_down2 = [1, 2] in result['valid_moves'] or (1, 2) in result['valid_moves']
    assert can_move_down or can_move_down2, f"王应能向下移动，实际: {result['valid_moves']}"
    
    print("[PASS] 升王测试")
    return game

def test_forced_jump():
    """测试强制跳跃（有跳跃时必须跳，不能走子）"""
    game = CheckersGame()
    for r in range(8):
        for c in range(8):
            game.board[r][c] = game.EMPTY
    
    # 布局：红方两枚棋子，黑方一枚
    # (5,0) 可以跳吃 (4,1) -> (3,2)
    # (5,4) 周围没有黑棋，不能跳
    game.board[5][0] = game.RED
    game.board[5][4] = game.RED
    game.board[4][1] = game.BLACK
    game.current_player = game.RED
    game.red_count = 2
    game.black_count = 1
    
    # 选择不能跳跃的棋子(5,4)应被拒绝 - 必须跳跃吃子
    result = game.select_piece(5, 4)
    has_error = result.get('error') and '必须' in result['error']
    assert has_error, f"应提示必须跳跃，实际: {result}"
    
    # 选择可跳跃的棋子(5,0)应成功
    result = game.select_piece(5, 0)
    assert result['success'], f"选择可跳棋子失败: {result.get('error')}"
    assert [3, 2] in result['valid_jumps'] or (3, 2) in result['valid_jumps'], f"应有跳跃目标(3,2)，实际: {result['valid_jumps']}"
    
    print("[PASS] 强制跳跃测试")
    return game

def test_win_condition():
    """测试胜负判定——吃光对方最后棋子获胜"""
    game = CheckersGame()
    for r in range(8):
        for c in range(8):
            game.board[r][c] = game.EMPTY
    
    game.board[5][0] = game.RED
    game.board[4][1] = game.BLACK
    game.current_player = game.RED
    game.red_count = 1
    game.black_count = 1
    
    result = game.make_move(5, 0, 3, 2)
    assert result['success'], f"吃子失败: {result.get('error')}"
    assert game.game_over, "游戏应结束"
    assert game.winner == game.RED, f"红方应获胜，实际赢家: {game.winner}"
    
    print("[PASS] 胜负判定测试")
    return game

def test_ai():
    """测试AI是否正常走棋"""
    game = CheckersGame()
    # 让红方走一步
    result = game.make_move(5, 2, 4, 1)
    assert result['success'], f"红方移动失败: {result.get('error')}"
    
    # AI走棋（黑方）
    result = game.ai_move()
    has_moved = result.get('success', False) or result.get('game_over', False)
    assert has_moved, f"AI走棋失败: {result.get('error', '未知错误')}"
    
    print("[PASS] AI测试")
    return game

if __name__ == '__main__':
    print("=" * 50)
    print("  跳棋游戏逻辑 - 自测程序")
    print("=" * 50)
    print()
    
    tests = [
        ("初始化测试", test_initialization),
        ("基本移动测试", test_basic_moves),
        ("跳跃吃子测试", test_jump),
        ("升王测试", test_king_promotion),
        ("强制跳跃测试", test_forced_jump),
        ("胜负判定测试", test_win_condition),
        ("AI测试", test_ai),
    ]
    
    passed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"[失败] {name}: {e}")
        except Exception as e:
            print(f"[异常] {name}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    
    print()
    print("=" * 50)
    print(f"  测试结果: {passed}/{len(tests)} 通过")
    if passed == len(tests):
        print("  所有测试通过！游戏逻辑正确!")
    else:
        print(f"  有 {len(tests) - passed} 个测试失败")
    print("=" * 50)
    
    sys.exit(0 if passed == len(tests) else 1)
