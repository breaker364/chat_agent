import requests, json, re, os

BASE = "http://localhost:8000"

# Read the current skill definition
r = requests.get(f"{BASE}/skills")
data = r.json()
skills_list = data.get("skills", [])

# Check what was generated
tmp_dir = "tmp"
output_path = os.path.join(tmp_dir, "architecture_diagram.html")
with open(output_path, 'r', encoding='utf-8') as f:
    content = f.read()

print(f"File size: {len(content)} bytes")
print("=== Full content ===")
print(content)
print("=== END ===")
