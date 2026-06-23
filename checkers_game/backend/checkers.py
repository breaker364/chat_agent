"""
跳棋（国际跳棋 / Checkers）游戏逻辑引擎
"""
import json

class CheckersGame:
    EMPTY = 0
    RED = 1
    BLACK = 2
    RED_KING = 3
    BLACK_KING = 4

    def __init__(self):
        self.reset()

    def reset(self):
        """初始化棋盘：红方在下，黑方在上"""
        self.board = [[self.EMPTY] * 8 for _ in range(8)]
        # 黑方在顶部（row 0-2）
        for r in range(3):
            for c in range(8):
                if (r + c) % 2 == 1:
                    self.board[r][c] = self.BLACK
        # 红方在底部（row 5-7）
        for r in range(5, 8):
            for c in range(8):
                if (r + c) % 2 == 1:
                    self.board[r][c] = self.RED
        self.current_player = self.RED  # 红方先走
        self.game_over = False
        self.winner = None
        self.selected = None
        self.valid_moves = []
        self.valid_jumps = []
        self.must_jump = False
        self.jumping_piece = None  # 连跳中的棋子
        self.red_count = 12
        self.black_count = 12

    def to_dict(self):
        """序列化为字典（用于API返回）"""
        return {
            'board': [row[:] for row in self.board],
            'current_player': self.current_player,
            'game_over': self.game_over,
            'winner': self.winner,
            'selected': list(self.selected) if self.selected else None,
            'valid_moves': [list(m) for m in self.valid_moves],
            'valid_jumps': [list(j) for j in self.valid_jumps],
            'jumping_piece': list(self.jumping_piece) if self.jumping_piece else None,
            'red_count': self.red_count,
            'black_count': self.black_count,
        }

    @staticmethod
    def is_valid_position(r, c):
        return 0 <= r < 8 and 0 <= c < 8

    @staticmethod
    def is_playable(r, c):
        """是否可落子的黑格（仅黑格可放棋子）"""
        return (r + c) % 2 == 1

    @staticmethod
    def is_red(piece):
        return piece == 1 or piece == 3

    @staticmethod
    def is_black(piece):
        return piece == 2 or piece == 4

    @staticmethod
    def is_king(piece):
        return piece == 3 or piece == 4

    def get_opponent(self, player):
        return self.BLACK if player == self.RED else self.RED

    def _get_jumps(self, r, c):
        """获取位置 (r,c) 的所有跳跃目标"""
        piece = self.board[r][c]
        if piece == self.EMPTY:
            return []

        is_red_piece = self.is_red(piece)
        is_black_piece = self.is_black(piece)
        is_king_piece = self.is_king(piece)

        # 方向：红方/王可向上，黑方/王可向下
        directions = []
        if is_red_piece or is_king_piece:
            directions.extend([(-1, -1), (-1, 1)])
        if is_black_piece or is_king_piece:
            directions.extend([(1, -1), (1, 1)])

        jumps = []
        for dr, dc in directions:
            mr, mc = r + dr, c + dc  # 中间格（对方的棋子）
            lr, lc = r + 2 * dr, c + 2 * dc  # 目标格
            if not (self.is_valid_position(lr, lc) and self.is_playable(lr, lc)):
                continue
            if not self.is_valid_position(mr, mc):
                continue
            if self.board[lr][lc] != self.EMPTY:
                continue
            mid = self.board[mr][mc]
            if mid == self.EMPTY:
                continue
            # 红方跳黑方，黑方跳红方
            if is_red_piece and self.is_black(mid):
                jumps.append((lr, lc, mr, mc))
            elif is_black_piece and self.is_red(mid):
                jumps.append((lr, lc, mr, mc))
        return jumps

    def _get_moves(self, r, c):
        """获取位置 (r,c) 的所有普通移动目标"""
        piece = self.board[r][c]
        if piece == self.EMPTY:
            return []

        is_red_piece = self.is_red(piece)
        is_black_piece = self.is_black(piece)
        is_king_piece = self.is_king(piece)

        directions = []
        if is_red_piece or is_king_piece:
            directions.extend([(-1, -1), (-1, 1)])
        if is_black_piece or is_king_piece:
            directions.extend([(1, -1), (1, 1)])

        moves = []
        for dr, dc in directions:
            nr, nc = r + dr, c + dc
            if self.is_valid_position(nr, nc) and self.is_playable(nr, nc):
                if self.board[nr][nc] == self.EMPTY:
                    moves.append((nr, nc))
        return moves

    def get_all_jumps(self, player=None):
        """获取某方所有可能的跳跃"""
        if player is None:
            player = self.current_player
        jumps = []
        for r in range(8):
            for c in range(8):
                piece = self.board[r][c]
                if piece == self.EMPTY:
                    continue
                if player == self.RED and self.is_red(piece):
                    jumps_from_piece = self._get_jumps(r, c)
                    jumps.extend([(r, c, j[0], j[1]) for j in jumps_from_piece])
                elif player == self.BLACK and self.is_black(piece):
                    jumps_from_piece = self._get_jumps(r, c)
                    jumps.extend([(r, c, j[0], j[1]) for j in jumps_from_piece])
        return jumps

    def get_all_moves(self, player=None):
        """获取某方所有可能的普通移动"""
        if player is None:
            player = self.current_player
        moves = []
        for r in range(8):
            for c in range(8):
                piece = self.board[r][c]
                if piece == self.EMPTY:
                    continue
                if player == self.RED and self.is_red(piece):
                    moves_from_piece = self._get_moves(r, c)
                    moves.extend([(r, c, m[0], m[1]) for m in moves_from_piece])
                elif player == self.BLACK and self.is_black(piece):
                    moves_from_piece = self._get_moves(r, c)
                    moves.extend([(r, c, m[0], m[1]) for m in moves_from_piece])
        return moves

    def _has_any_move(self, player):
        """某方是否有任何合法行动（移动或跳跃）"""
        for r in range(8):
            for c in range(8):
                piece = self.board[r][c]
                if piece == self.EMPTY:
                    continue
                is_player = (player == self.RED and self.is_red(piece)) or \
                            (player == self.BLACK and self.is_black(piece))
                if is_player:
                    if self._get_jumps(r, c) or self._get_moves(r, c):
                        return True
        return False

    def select_piece(self, r, c):
        """
        选择棋子，返回可选的目标位置
        返回: dict {success, error, valid_moves, valid_jumps, selected}
        """
        if self.game_over:
            return {'error': '游戏已结束'}

        piece = self.board[r][c]
        if piece == self.EMPTY:
            return {'error': '该位置没有棋子'}

        is_current = (self.current_player == self.RED and self.is_red(piece)) or \
                     (self.current_player == self.BLACK and self.is_black(piece))
        if not is_current:
            return {'error': '不是你的棋子'}
        
        # 连跳模式：只能继续使用同一枚棋子
        if self.jumping_piece is not None and (r, c) != self.jumping_piece:
            return {'error': '必须用同一枚棋子继续连跳'}
        
        # 检查是否有强制跳跃
        all_jumps = self.get_all_jumps(self.current_player)
        if self.jumping_piece is None and all_jumps:
            # 强制跳跃模式：只显示有跳跃的棋子
            piece_jumps = self._get_jumps(r, c)
            if not piece_jumps:
                return {'error': '必须进行跳跃吃子！'}
            self.selected = (r, c)
            self.valid_moves = []
            self.valid_jumps = [(j[0], j[1]) for j in piece_jumps]
            return {
                'success': True,
                'selected': [r, c],
                'valid_moves': [],
                'valid_jumps': self.valid_jumps,
                'jumping_piece': list(self.jumping_piece) if self.jumping_piece else None
            }
        
        # 连跳模式：检查是否有更多跳跃
        if self.jumping_piece is not None:
            piece_jumps = self._get_jumps(r, c)
            if piece_jumps:
                self.selected = (r, c)
                self.valid_moves = []
                self.valid_jumps = [(j[0], j[1]) for j in piece_jumps]
                return {
                    'success': True,
                    'selected': [r, c],
                    'valid_moves': [],
                    'valid_jumps': self.valid_jumps,
                    'jumping_piece': list(self.jumping_piece)
                }
        
        # 普通移动
        moves = self._get_moves(r, c)
        self.selected = (r, c)
        self.valid_moves = moves
        self.valid_jumps = []
        return {
            'success': True,
            'selected': [r, c],
            'valid_moves': [list(m) for m in moves],
            'valid_jumps': [],
            'jumping_piece': None
        }

    def make_move(self, from_r, from_c, to_r, to_c):
        """
        执行移动
        返回: dict {success, error, game_over, winner, multi_jump, valid_jumps, message}
        """
        if self.game_over:
            return {'error': '游戏已结束'}
        
        piece = self.board[from_r][from_c]
        if piece == self.EMPTY:
            return {'error': '源位置没有棋子'}
        
        # 判断是否为跳跃（距离为2）
        is_jump = abs(to_r - from_r) == 2
        
        if is_jump:
            # ---- 跳跃吃子 ----
            jumps = self._get_jumps(from_r, from_c)
            jump_dests = [(j[0], j[1]) for j in jumps]
            if (to_r, to_c) not in jump_dests:
                return {'error': '无效的跳跃'}
            
            mid_r = (from_r + to_r) // 2
            mid_c = (from_c + to_c) // 2
            jumped_piece = self.board[mid_r][mid_c]
            
            # 执行跳跃
            self.board[to_r][to_c] = piece
            self.board[from_r][from_c] = self.EMPTY
            self.board[mid_r][mid_c] = self.EMPTY
            
            # 更新棋子计数
            if jumped_piece == self.RED or jumped_piece == self.RED_KING:
                self.red_count -= 1
            else:
                self.black_count -= 1
            
            # 升王
            promoted = False
            if piece == self.RED and to_r == 0:
                self.board[to_r][to_c] = self.RED_KING
                promoted = True
            elif piece == self.BLACK and to_r == 7:
                self.board[to_r][to_c] = self.BLACK_KING
                promoted = True
            
            # 连跳检测（升王后不可继续跳）
            if not promoted:
                more_jumps = self._get_jumps(to_r, to_c)
                if more_jumps:
                    self.jumping_piece = (to_r, to_c)
                    self.selected = None
                    self.valid_moves = []
                    self.valid_jumps = [(j[0], j[1]) for j in more_jumps]
                    return {
                        'success': True,
                        'message': '继续连跳！',
                        'multi_jump': True,
                        'valid_jumps': self.valid_jumps,
                        'jumping_piece': [to_r, to_c],
                        'game_over': False,
                        'winner': None
                    }
            
            # 切换玩家
            self.current_player = self.get_opponent(self.current_player)
            self.selected = None
            self.valid_moves = []
            self.valid_jumps = []
            self.jumping_piece = None
            
            # 检查胜负
            if self._check_win():
                pass
            
            return {
                'success': True,
                'message': '跳跃完成',
                'multi_jump': False,
                'valid_jumps': [],
                'jumping_piece': None,
                'game_over': self.game_over,
                'winner': self.winner
            }
        
        else:
            # ---- 普通移动 ----
            # 检查是否有强制跳跃
            if self.jumping_piece is None:
                all_jumps = self.get_all_jumps(self.current_player)
                if all_jumps:
                    return {'error': '必须进行跳跃吃子！'}
            
            moves = self._get_moves(from_r, from_c)
            if (to_r, to_c) not in moves:
                return {'error': '无效的移动'}
            
            # 执行移动
            self.board[to_r][to_c] = piece
            self.board[from_r][from_c] = self.EMPTY
            
            # 升王
            if piece == self.RED and to_r == 0:
                self.board[to_r][to_c] = self.RED_KING
            elif piece == self.BLACK and to_r == 7:
                self.board[to_r][to_c] = self.BLACK_KING
            
            # 切换玩家
            self.current_player = self.get_opponent(self.current_player)
            
            # 检查胜负
            self._check_win()
            
            self.selected = None
            self.valid_moves = []
            self.valid_jumps = []
            self.jumping_piece = None
            
            return {
                'success': True,
                'message': '移动完成',
                'multi_jump': False,
                'valid_jumps': [],
                'jumping_piece': None,
                'game_over': self.game_over,
                'winner': self.winner
            }

    def _check_win(self):
        """
        检查当前玩家（切换后轮到的一方）是否还有合法移动。
        如果对手（刚刚走完的一方）吃光了对方棋子，或者对方的棋子全被堵死，
        则游戏结束，刚刚完成走棋的一方获胜。
        """
        if not self._has_any_move(self.current_player):
            self.game_over = True
            # winner 是刚刚走完的一方，即 current_player 的对手
            self.winner = self.get_opponent(self.current_player)
            return True
        return False

    def ai_move(self):
        """简易AI：优先跳跃，否则随机移动"""
        import random
        jumps = self.get_all_jumps(self.current_player)
        if jumps:
            move = random.choice(jumps)
            result = self.make_move(move[0], move[1], move[2], move[3])
            # 处理连跳
            while result.get('multi_jump'):
                piece_jumps = self._get_jumps(self.jumping_piece[0], self.jumping_piece[1])
                if piece_jumps:
                    j = random.choice(piece_jumps)
                    result = self.make_move(self.jumping_piece[0], self.jumping_piece[1], j[0], j[1])
                else:
                    break
            return result
        moves = self.get_all_moves(self.current_player)
        if moves:
            move = random.choice(moves)
            return self.make_move(move[0], move[1], move[2], move[3])
        self._check_win()
        return {'error': '无可用移动', 'game_over': self.game_over, 'winner': self.winner}
