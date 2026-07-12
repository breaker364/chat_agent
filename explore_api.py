import json
import os
from pathlib import Path

import requests

_config_path = Path(__file__).resolve().parent / "runtime_config.json"
_config = json.loads(_config_path.read_text(encoding="utf-8")) if _config_path.exists() else {}
BASE = os.environ.get("CHAT_AGENT_API_BASE_URL") or _config["app"]["api_base_url"]

# Try different API paths
paths_to_try = [
    "/skills",
    "/api/skills", 
    "/api/v1/skills",
    "/execute",
    "/skill",
]

print("=== Exploring API ===")
for path in paths_to_try:
    try:
        r = requests.get(f"{BASE}{path}", timeout=5)
        print(f"GET {path}: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"  Data: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}")
    except Exception as e:
        print(f"GET {path}: Error - {e}")

# Check the actual skills data from the GET /skills response
r = requests.get(f"{BASE}/skills", timeout=5)
if r.status_code == 200:
    data = r.json()
    skills_list = data.get("skills", [])
    print(f"\n=== Skills count: {len(skills_list)} ===")
    for s in skills_list:
        print(f"  - {s.get('name')}: params={s.get('params', [])}")
        prompt = s.get('prompt_template', '')
        print(f"    prompt length: {len(prompt)}")
    
    # Find architecture skill
    arch_skill = None
    for s in skills_list:
        if 'architecture' in s['name'].lower():
            arch_skill = s
            break
    
    if arch_skill:
        print(f"\n=== Found: {arch_skill['name']} ===")
        # Try POST to different paths
        post_paths = ["/skills/execute", "/execute", "/skill/execute"]
        for pp in post_paths:
            try:
                payload = {"name": arch_skill['name'], "params": {}}
                r2 = requests.post(f"{BASE}{pp}", json=payload, timeout=10)
                print(f"POST {pp}: {r2.status_code}")
                if r2.status_code == 200:
                    print(f"  Success! Result: {json.dumps(r2.json(), ensure_ascii=False)[:300]}")
                    break
                else:
                    print(f"  Response: {r2.text[:200]}")
            except Exception as e:
                print(f"POST {pp}: Error - {e}")
else:
    print(f"GET /skills failed: {r.status_code}")
