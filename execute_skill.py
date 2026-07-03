import json
import os
import re
from pathlib import Path

import requests

_config_path = Path(__file__).resolve().parent / "runtime_config.json"
_config = json.loads(_config_path.read_text(encoding="utf-8")) if _config_path.exists() else {}
BASE = os.environ.get("CHAT_AGENT_API_BASE_URL") or _config["app"]["api_base_url"]

# Step 1: Get skills
r = requests.get(f"{BASE}/skills")
print("Skills API status:", r.status_code)
if r.status_code == 200:
    data = r.json()
    print(f"Data type: {type(data)}")
    print(f"Data: {json.dumps(data, ensure_ascii=False, indent=2)[:1000]}")
    
    # Find architecture diagram skill
    skill_name = None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and 'architecture' in item.get('name','').lower():
                skill_name = item['name']
                break
            elif isinstance(item, str) and 'architecture' in item.lower():
                skill_name = item
                break
        if not skill_name and data:
            skill_name = data[0] if isinstance(data[0], str) else data[0].get('name','')
    elif isinstance(data, dict):
        for k, v in data.items():
            if 'architecture' in k.lower():
                skill_name = k
                break
        if not skill_name:
            skill_name = next(iter(data.keys()))
    
    print(f"\nSkill to execute: {skill_name}")
    
    if skill_name:
        payload = {"name": skill_name, "params": {}}
        
        print(f"Executing skill: {skill_name}")
        r2 = requests.post(f"{BASE}/skills/execute", json=payload, timeout=180)
        print(f"Execute status: {r2.status_code}")
        
        if r2.status_code == 200:
            result = r2.json()
            content = result.get('result', '')
            print(f"Result length: {len(content)} chars")
            print(f"First 300 chars: {content[:300]}")
            print(f"Last 100 chars: {content[-100:]}")
            
            # Look for HTML/SVG
            has_svg = '<svg' in content
            has_html = '<html' in content.lower()
            print(f"Contains <svg>: {has_svg}")
            print(f"Contains <html>: {has_html}")
            
            # Try to extract HTML
            html_match = re.search(r'```(?:html)?\s*\n(.*?)(?:```|\Z)', content, re.DOTALL)
            if html_match:
                html_content = html_match.group(1).strip()
            else:
                html_content = content
            
            # Save to tmp
            tmp_dir = "tmp"
            os.makedirs(tmp_dir, exist_ok=True)
            output_path = os.path.join(tmp_dir, "architecture_diagram.html")
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            print(f"\nSaved to {output_path}")
            print(f"File size: {len(html_content)} bytes")
            print(f"Full path: {os.path.abspath(output_path)}")
        else:
            print(f"Error: {r2.text[:1000]}")
    else:
        print("No skill found!")
else:
    print(f"Error: {r.text[:500]}")
