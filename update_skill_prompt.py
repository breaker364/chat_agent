import requests, json, re, os

BASE = "http://localhost:8000"

# Read current skill
r = requests.get(f"{BASE}/skills")
data = r.json()
skills_list = data.get("skills", [])

arch_skill = None
for s in skills_list:
    if 'architecture' in s['name'].lower():
        arch_skill = s
        break

if arch_skill:
    # Check current prompt template length
    prompt = arch_skill.get('prompt_template', '')
    print(f"Current prompt template length: {len(prompt)} chars")
    
    # Let's read the skills JSON file directly and update it
    import os
    skills_file = os.path.join(os.getcwd(), "skills", "architecture-diagram-generator.json")
    print(f"Skills file: {skills_file}")
    
    with open(skills_file, 'r', encoding='utf-8') as f:
        skill_data = json.load(f)
    
    print(f"Current prompt_template length: {len(skill_data['prompt_template'])}")
    
    # Create a more concise prompt that fits within token limits
    concise_prompt = """You are an architecture diagram generator. Based on the system description below, generate a COMPLETE self-contained HTML file with inline SVG graphics.

## Output
Output ONLY ```html ... ``` with a complete HTML file. The file MUST contain:
1. A dark-themed SVG architecture diagram showing all system components
2. Export toolbar with Copy/Download PNG/Download PDF buttons
3. Proper component boxes with labels, arrows between components
4. Complete, working HTML that opens in a browser

## Color Scheme
Background: #020617, Card: #0f172a, Border: #1e293b
- Frontend: fill rgba(8,51,68,0.4) stroke #22d3ee
- Backend: fill rgba(6,78,59,0.4) stroke #34d399  
- Database: fill rgba(76,29,149,0.4) stroke #a78bfa
- Cloud/AWS: fill rgba(120,53,15,0.3) stroke #fbbf24
- Security: fill rgba(136,19,55,0.4) stroke #fb7185
- Message Bus: fill rgba(251,146,60,0.3) stroke #fb923c
- External: fill rgba(30,41,59,0.5) stroke #94a3b8

## Requirements
- Use JetBrains Mono font from Google Fonts CDN
- Component boxes: rounded rects (rx=6), 1.5px stroke, semi-transparent fill
- Draw opaque rect (fill #0f172a) behind each component to mask arrows
- Connection arrows between related components using SVG markers
- Export toolbar uses html2canvas and jspdf from CDN
- Include 3 summary cards below the diagram
- CRITICAL: The SVG MUST be complete - do not cut off style or elements
- Keep the design simple but polished with proper spacing

## System Description
{system_description}

Generate a {diagram_type} diagram. Output ONLY the HTML code block."""
    
    skill_data['prompt_template'] = concise_prompt
    skill_data['params_schema'] = {
        "type": "object",
        "properties": {
            "system_description": {
                "type": "string",
                "description": "Natural language description of the system architecture"
            },
            "diagram_type": {
                "type": "string",
                "enum": ["architecture", "flowchart", "sequence", "component"],
                "default": "architecture",
                "description": "Type of diagram to generate"
            }
        },
        "required": ["system_description"]
    }
    
    with open(skills_file, 'w', encoding='utf-8') as f:
        json.dump(skill_data, f, ensure_ascii=False, indent=2)
    
    print(f"Updated prompt_template length: {len(concise_prompt)}")
    print("Skill file updated!")

    # Also update the built-in registry in skills.py
    skills_py_path = os.path.join(os.getcwd(), "backend", "skills.py")
    print(f"\nNeed to update: {skills_py_path}")
