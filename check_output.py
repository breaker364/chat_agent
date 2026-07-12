import json
import os
import re
from pathlib import Path

import requests

_config_path = Path(__file__).resolve().parent / "runtime_config.json"
_config = json.loads(_config_path.read_text(encoding="utf-8")) if _config_path.exists() else {}
BASE = os.environ.get("CHAT_AGENT_API_BASE_URL") or _config["app"]["api_base_url"]

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
