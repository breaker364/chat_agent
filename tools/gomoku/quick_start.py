"""Quick start: launch backend in background and open browser."""
import os, subprocess, sys, time, webbrowser

script_dir = os.path.dirname(os.path.abspath(__file__))
backend = os.path.join(script_dir, "backend.py")

# Hide console window on Windows
si = None
if sys.platform == "win32":
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

proc = subprocess.Popen(
    [sys.executable, backend],
    cwd=script_dir,
    startupinfo=si,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL
)

time.sleep(1.5)

if proc.poll() is None:
    webbrowser.open("http://localhost:8888")
    print("Server started on http://localhost:8888")
    print(f"PID: {proc.pid}")
    # Keep running briefly to confirm
    time.sleep(2)
    print("Server is running. Browser should open now.")
else:
    print("Server failed to start!")
    sys.exit(1)
