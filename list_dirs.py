import os
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

print("--- D:\\NoobhekProject ---")
for item in os.listdir(r"D:\NoobhekProject"):
    print(f"  {item}")

print("\n--- D:\\NoobhekProject\\Node-Selector ---")
for item in os.listdir(r"D:\NoobhekProject\Node-Selector"):
    print(f"  {item}")

print("\n--- D:\\NoobhekProject\\Node-Selector\\code ---")
for item in os.listdir(r"D:\NoobhekProject\Node-Selector\code"):
    print(f"  {item}")

print("\n--- D:\\NoobhekProject\\Node-Selector\\code\\point_order_tool ---")
for item in os.listdir(r"D:\NoobhekProject\Node-Selector\code\point_order_tool"):
    print(f"  {item}")
