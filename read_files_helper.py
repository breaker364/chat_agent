import os
import sys
import io

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

files = [
    r"D:\NoobhekProject\Node-Selector\extract_nodes.py",
    r"D:\NoobhekProject\Node-Selector\code\point_order_tool\app.py",
]

for fpath in files:
    print(f"\n{'='*80}")
    print(f"FILE: {fpath}")
    print(f"{'='*80}")
    if os.path.exists(fpath):
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        print(content)
    else:
        print("[FILE NOT FOUND]")
    print(f"{'='*80}")
    print(f"END OF FILE: {fpath}")
    print(f"{'='*80}\n")

# Also list the directories
print("\n--- Listing D:\\NoobhekProject ---")
for item in os.listdir(r"D:\NoobhekProject"):
    print(f"  {item}")

print("\n--- Listing D:\\NoobhekProject\\Node-Selector ---")
for item in os.listdir(r"D:\NoobhekProject\Node-Selector"):
    print(f"  {item}")

print("\n--- Listing D:\\NoobhekProject\\Node-Selector\\code ---")
for item in os.listdir(r"D:\NoobhekProject\Node-Selector\code"):
    print(f"  {item}")

print("\n--- Listing D:\\NoobhekProject\\Node-Selector\\code\\point_order_tool ---")
for item in os.listdir(r"D:\NoobhekProject\Node-Selector\code\point_order_tool"):
    print(f"  {item}")
