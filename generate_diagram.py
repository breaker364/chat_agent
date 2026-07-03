import json
import os
import re
from pathlib import Path

import requests

_config_path = Path(__file__).resolve().parent / "runtime_config.json"
_config = json.loads(_config_path.read_text(encoding="utf-8")) if _config_path.exists() else {}
BASE = os.environ.get("CHAT_AGENT_API_BASE_URL") or _config["app"]["api_base_url"]

# System description of this agent project
system_desc = """
System Architecture of an AI Chat Agent

## Frontend Layer (React + Vite)
- React 18 SPA with component-based UI
- SkillPopup: `/skill` slash-command popup for selecting and executing skills
- ChatArea: Message display with markdown rendering
- Sidebar: Session management (create, rename, delete)
- Build tool: Vite with ES modules

## Backend Layer (Python FastAPI)
- FastAPI server on port 8000 with SSE streaming and REST APIs
- Routes: /chat (sync+stream), /sessions (CRUD), /skills (install/execute), /subagents
- Agent orchestration: LangGraph-based agent with streaming event pipeline
- Session management: JSON file-based session store with progress tracking
- Skill system: File-based skill definitions (.json), prompt template filling, LLM execution
- Subagent system: Background subagent tasks with event bus notifications
- MCP integration: Tool-based connectors to external services

## External Integrations (MCP Tools)
- McDonald's China: Orders, coupons, stores, meals, account (20+ tools)
- 12306 Train API: Station codes, ticket queries, interline tickets
- Web Search: Tavily/Bing adapters for internet search
- File Operations: Local file read/write/delete tools
- Python Execution: Sandboxed Python script runner

## Data Flow
- User -> Frontend (React) -> HTTP/SSE -> Backend (FastAPI)
- Backend -> LangGraph Agent -> MCP Tools -> External Services
- Agent events streamed via SSE to frontend in real-time
- Sessions stored as JSON files on disk

## Key Design Patterns
- Event-driven SSE streaming for real-time UI updates
- Lazy-loaded agent singleton with async lock
- Session-scoped environment variables for tool isolation
- Subagent mailbox pattern for cross-agent communication
"""

r = requests.get(f"{BASE}/skills")
if r.status_code == 200:
    data = r.json()
    skills_list = data.get("skills", [])
    print(f"Found {len(skills_list)} skills")
    
    arch_skill = None
    for s in skills_list:
        if 'architecture' in s['name'].lower():
            arch_skill = s
            break
    
    if arch_skill:
        print(f"Executing skill: {arch_skill['name']}")
        
        payload = {
            "params": {
                "system_description": system_desc.strip(),
                "diagram_type": "architecture"
            }
        }
        
        # The correct endpoint is POST /skills/{name}/execute
        r2 = requests.post(f"{BASE}/skills/{arch_skill['name']}/execute", json=payload, timeout=180)
        print(f"Execute status: {r2.status_code}")
        
        if r2.status_code == 200:
            result = r2.json()
            content = result.get('result', '')
            print(f"Result length: {len(content)} chars")
            print(f"First 300 chars: {content[:300]}")
            print(f"Last 100 chars: {content[-100:]}")
            
            has_svg = '<svg' in content
            has_html = '<html' in content.lower()
            has_doctype = '<!DOCTYPE' in content.upper()
            print(f"Contains <svg>: {has_svg}")
            print(f"Contains <html>: {has_html}")
            print(f"Contains DOCTYPE: {has_doctype}")
            
            # Extract HTML from code block
            html_match = re.search(r'```html\s*\n(.*?)(?:```|\Z)', content, re.DOTALL)
            if html_match:
                html_content = html_match.group(1).strip()
            else:
                html_content = content
            
            # Save to tmp dir
            tmp_dir = "tmp"
            os.makedirs(tmp_dir, exist_ok=True)
            output_path = os.path.join(tmp_dir, "architecture_diagram.html")
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            full_path = os.path.abspath(output_path)
            file_size = len(html_content)
            print(f"\nSaved to: {full_path}")
            print(f"File size: {file_size} bytes")
            
            # Also save a backup copy
            output_path2 = os.path.join(tmp_dir, "agent_architecture.html")
            with open(output_path2, 'w', encoding='utf-8') as f:
                f.write(html_content)
            print(f"Backup: {os.path.abspath(output_path2)}")
        else:
            print(f"Error: {r2.status_code}")
            print(r2.text[:1000])
    else:
        print("Architecture skill not found in installed skills!")
else:
    print(f"Error: {r.status_code}")
