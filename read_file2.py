import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

with open(r"D:\NoobhekProject\Node-Selector\code\point_order_tool\app.py", "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

# Save to workspace
with open("app.py", "w", encoding="utf-8") as out:
    out.write(content)

print(f"File size: {len(content)} chars, {content.count(chr(10))+1} lines")
print("Saved to workspace: app.py")
