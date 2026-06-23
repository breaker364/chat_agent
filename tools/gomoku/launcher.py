#!/usr/bin/env python3
"""
Launcher: starts the Gomoku backend server and opens the browser.
"""
import os
import subprocess
import sys
import time
import webbrowser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_PATH = os.path.join(SCRIPT_DIR, "backend.py")
FRONTEND_URL = "http://localhost:8888"

def main():
    print("🎮 五子棋游戏启动中...")
    print(f"   后端路径: {BACKEND_PATH}")
    print(f"   前端地址: {FRONTEND_URL}")
    print()
    
    # Start backend server in a new process
    if sys.platform == "win32":
        # Hide the console window for the subprocess
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        process = subprocess.Popen(
            [sys.executable, BACKEND_PATH],
            cwd=SCRIPT_DIR,
            startupinfo=startupinfo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
    else:
        process = subprocess.Popen(
            [sys.executable, BACKEND_PATH],
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
    
    # Wait a moment for the server to start
    time.sleep(1)
    
    # Check if process is still running
    if process.poll() is not None:
        print("❌ 服务器启动失败!")
        stdout, stderr = process.communicate()
        if stdout:
            print(f"   stdout: {stdout.decode('utf-8', errors='replace')}")
        if stderr:
            print(f"   stderr: {stderr.decode('utf-8', errors='replace')}")
        sys.exit(1)
    
    # Open browser
    print("📂 正在打开浏览器...")
    webbrowser.open(FRONTEND_URL)
    
    print(f"\n✅ 服务器已启动! 访问 http://localhost:8888 开始游戏")
    print("   按 Ctrl+C 停止服务器\n")
    
    try:
        # Keep the launcher running so user can Ctrl+C
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n⏹ 正在停止服务器...")
        process.terminate()
        process.wait()
        print("服务器已停止。")

if __name__ == "__main__":
    main()
