/**
 * 跳棋前端游戏逻辑
 */
const API_BASE = window.CHECKERS_API_BASE || window.location.origin;

let gameState = null;
let selectedPiece = null;  // {row, col}
let boardEl = document.getElementById('board');
let turnText = document.getElementById('turn-text');
let messageEl = document.getElementById('message');
let redCountEl = document.getElementById('red-count');
let blackCountEl = document.getElementById('black-count');
let gameOverModal = document.getElementById('game-over-modal');
let winnerText = document.getElementById('winner-text');

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    fetchGameState();
    document.getElementById('reset-btn').addEventListener('click', resetGame);
    document.getElementById('ai-btn').addEventListener('click', aiTurn);
    document.getElementById('modal-reset-btn').addEventListener('click', resetGame);
});

// 获取游戏状态
async function fetchGameState() {
    try {
        const res = await fetch(`${API_BASE}/api/game`);
        gameState = await res.json();
        renderBoard();
        updateUI();
    } catch (err) {
        messageEl.textContent = '无法连接到服务器，请确保后端已启动';
        messageEl.style.color = '#e74c3c';
    }
}

// 渲染棋盘
function renderBoard() {
    boardEl.innerHTML = '';
    for (let r = 0; r < 8; r++) {
        for (let c = 0; c < 8; c++) {
            const cell = document.createElement('div');
            cell.className = `cell ${(r + c) % 2 === 0 ? 'light' : 'dark'}`;
            cell.dataset.row = r;
            cell.dataset.col = c;

            // 如果是黑格，支持点击
            if ((r + c) % 2 === 1) {
                cell.addEventListener('click', () => onCellClick(r, c));
            }

            // 画棋子
            const piece = gameState.board[r][c];
            if (piece !== 0) {
                const pieceEl = document.createElement('div');
                pieceEl.className = 'piece';
                if (piece === 1) pieceEl.classList.add('red');
                else if (piece === 2) pieceEl.classList.add('black');
                else if (piece === 3) pieceEl.classList.add('red-king');
                else if (piece === 4) pieceEl.classList.add('black-king');

                // 高亮选中的棋子
                if (gameState.selected && gameState.selected[0] === r && gameState.selected[1] === c) {
                    pieceEl.classList.add('selected');
                }

                cell.appendChild(pieceEl);
            }

            // 显示有效移动标识
            if (gameState.valid_moves) {
                for (const move of gameState.valid_moves) {
                    if (move[0] === r && move[1] === c) {
                        const indicator = document.createElement('div');
                        indicator.className = 'valid-move-indicator';
                        cell.appendChild(indicator);
                    }
                }
            }

            // 显示有效跳跃标识
            if (gameState.valid_jumps) {
                for (const jump of gameState.valid_jumps) {
                    if (jump[0] === r && jump[1] === c) {
                        const indicator = document.createElement('div');
                        indicator.className = 'valid-jump-indicator';
                        cell.appendChild(indicator);
                    }
                }
            }

            boardEl.appendChild(cell);
        }
    }
}

// 点击格子
async function onCellClick(row, col) {
    if (gameState.game_over) {
        messageEl.textContent = '游戏已结束，请重新开始';
        return;
    }

    const piece = gameState.board[row][col];

    // 情况1: 点击了己方棋子 -> 选择
    if (isOwnPiece(piece)) {
        await selectPiece(row, col);
        return;
    }

    // 情况2: 点击了有效移动目标 -> 移动
    if (selectedPiece && isValidTarget(row, col)) {
        await makeMove(selectedPiece.row, selectedPiece.col, row, col);
        return;
    }

    // 情况3: 点击了有效跳跃目标 -> 跳跃
    if (selectedPiece && isValidJumpTarget(row, col)) {
        await makeMove(selectedPiece.row, selectedPiece.col, row, col);
        return;
    }

    // 情况4: 点击了空黑格但不是有效目标
    if (piece === 0 && (row + col) % 2 === 1) {
        messageEl.textContent = '无效的移动，请先选中你的棋子';
        setTimeout(() => { messageEl.textContent = ''; }, 1500);
    }
}

// 判断是否为当前玩家的棋子
function isOwnPiece(piece) {
    if (piece === 0) return false;
    const isRed = piece === 1 || piece === 3;
    const isBlack = piece === 2 || piece === 4;
    return (gameState.current_player === 1 && isRed) ||
           (gameState.current_player === 2 && isBlack);
}

// 判断是否为有效移动目标
function isValidTarget(row, col) {
    if (!gameState.valid_moves) return false;
    return gameState.valid_moves.some(m => m[0] === row && m[1] === col);
}

// 判断是否为有效跳跃目标
function isValidJumpTarget(row, col) {
    if (!gameState.valid_jumps) return false;
    return gameState.valid_jumps.some(j => j[0] === row && j[1] === col);
}

// 选择棋子
async function selectPiece(row, col) {
    try {
        const res = await fetch(`${API_BASE}/api/select`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ row, col })
        });
        const data = await res.json();

        if (data.error) {
            messageEl.textContent = data.error;
            setTimeout(() => { 
                if (messageEl.textContent === data.error) messageEl.textContent = ''; 
            }, 1500);
            return;
        }

        selectedPiece = { row, col };
        gameState = data;
        renderBoard();
        updateUI();
    } catch (err) {
        messageEl.textContent = '请求失败，请检查连接';
    }
}

// 执行移动
async function makeMove(fromRow, fromCol, toRow, toCol) {
    try {
        const res = await fetch(`${API_BASE}/api/move`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                from_row: fromRow,
                from_col: fromCol,
                to_row: toRow,
                to_col: toCol
            })
        });
        const data = await res.json();

        if (data.error) {
            messageEl.textContent = data.error;
            setTimeout(() => { 
                if (messageEl.textContent === data.error) messageEl.textContent = ''; 
            }, 1500);
            return;
        }

        selectedPiece = null;
        gameState = data;
        renderBoard();
        updateUI();

        // 连跳提示
        if (data.multi_jump) {
            messageEl.textContent = '继续连跳！点击目标位置继续吃子';
            // 自动选中连跳棋子
            if (data.jumping_piece) {
                selectedPiece = { row: data.jumping_piece[0], col: data.jumping_piece[1] };
            }
        } else {
            messageEl.textContent = data.message || '';
            setTimeout(() => { messageEl.textContent = ''; }, 1000);
        }

        // 游戏结束
        if (data.game_over) {
            showGameOver(data.winner);
        }
    } catch (err) {
        messageEl.textContent = '请求失败，请检查连接';
    }
}

// 更新UI
function updateUI() {
    // 更新回合显示
    const isRedTurn = gameState.current_player === 1;
    const turnIndicator = document.getElementById('current-turn');
    turnText.textContent = isRedTurn ? '红方回合' : '黑方回合';
    turnIndicator.className = 'turn-indicator';
    turnIndicator.style.borderLeft = `4px solid ${isRedTurn ? '#e74c3c' : '#2c3e50'}`;

    // 更新棋子计数
    redCountEl.textContent = gameState.red_count;
    blackCountEl.textContent = gameState.black_count;

    // 更新强制跳跃提示
    if (gameState.valid_jumps && gameState.valid_jumps.length > 0 && !selectedPiece) {
        messageEl.textContent = '有可跳吃的棋子！点击你的棋子进行跳跃';
    }
}

// 显示游戏结束弹窗
function showGameOver(winner) {
    const isRed = winner === 1;
    winnerText.textContent = isRed ? '🎉 红方获胜！' : '🎉 黑方获胜！';
    gameOverModal.classList.remove('hidden');
}

// 重置游戏
async function resetGame() {
    try {
        const res = await fetch(`${API_BASE}/api/reset`, {
            method: 'POST'
        });
        const data = await res.json();
        selectedPiece = null;
        gameState = data;
        gameOverModal.classList.add('hidden');
        renderBoard();
        updateUI();
        messageEl.textContent = '游戏已重置';
        setTimeout(() => { messageEl.textContent = ''; }, 1500);
    } catch (err) {
        messageEl.textContent = '重置失败';
    }
}

// AI走一步
async function aiTurn() {
    if (gameState.game_over) {
        messageEl.textContent = '游戏已结束';
        return;
    }
    if (gameState.current_player !== 2) {
        messageEl.textContent = '当前是红方回合，请先走棋';
        setTimeout(() => { messageEl.textContent = ''; }, 1500);
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/api/ai`, {
            method: 'POST'
        });
        const data = await res.json();
        selectedPiece = null;
        gameState = data;
        renderBoard();
        updateUI();
        messageEl.textContent = 'AI 走了一步';

        if (data.game_over) {
            showGameOver(data.winner);
        }

        setTimeout(() => { 
            if (messageEl.textContent === 'AI 走了一步') messageEl.textContent = ''; 
        }, 1500);
    } catch (err) {
        messageEl.textContent = 'AI 请求失败';
    }
}
