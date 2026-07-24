"""
commands/whiteboard.py - Read-only whiteboard (画板) commands.

Phase 1: list / meta / read / download (PNG).
Write commands (plantuml / mermaid / overwrite) live in whiteboard_write.py.
"""

import json
import os
import re
import sys

from ..whiteboard_api import (
    download_whiteboard_png,
    fetch_whiteboard_block,
    find_syntax_containers,
    list_whiteboards_in_docx,
    resolve_block_token,
)
from ..commands.doc import resolve_doc_token


def _first_present(mapping: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _extract_text(value) -> str:
    fragments = []

    def walk(obj):
        if isinstance(obj, dict):
            for key, nested in obj.items():
                lowered = str(key).lower()
                if lowered in {'text', 'content', 'plain', 'plaintext', 'title'} and isinstance(nested, str):
                    fragments.append(nested)
                else:
                    walk(nested)
        elif isinstance(obj, list):
            for nested in obj:
                walk(nested)

    walk(value)
    seen = set()
    unique = []
    for fragment in fragments:
        cleaned = fragment.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return '\n'.join(unique)


def _infer_node_type(info: dict) -> str:
    if 'mindMap' in info:
        return 'mind_map'
    if 'connectorV2' in info or 'connector' in info:
        return 'connector'
    if 'textV2' in info and 'compositeShape' in info:
        return 'shape_text'
    if 'textV2' in info:
        return 'text'
    if 'compositeShape' in info:
        return 'shape'
    if 'line' in info or 'lineV2' in info:
        return 'line'
    if 'image' in info or 'imageV2' in info:
        return 'image'
    return 'unknown'


def _normalize_node(node: dict) -> dict:
    info = node.get('info') or {}
    base = info.get('baseV2') or info.get('base') or {}
    text_info = info.get('textV2') or info.get('text') or {}
    children = node.get('children') or []
    normalized = {
        'id': node.get('id'),
        'type': _infer_node_type(info),
        'text': _extract_text(text_info),
        'geometry': {
            'x': _first_present(base, ('x', 'left')),
            'y': _first_present(base, ('y', 'top')),
            'width': _first_present(base, ('width', 'w')),
            'height': _first_present(base, ('height', 'h')),
            'rotation': _first_present(base, ('rotation', 'rotate')),
        },
        'style': {
            'theme': info.get('theme') or {},
            'opacity': info.get('opacity'),
            'flip': info.get('flip'),
            'shape': info.get('compositeShape'),
        },
        'relationships': {
            'children': [child.get('id') for child in children if isinstance(child, dict)],
            'mind_map': info.get('mindMap'),
            'connector': info.get('connectorV2') or info.get('connector'),
        },
        'raw_keys': sorted(info.keys()),
    }
    normalized['children'] = [_normalize_node(child) for child in children if isinstance(child, dict)]
    return normalized


def _collect_flat_nodes(nodes: list) -> list:
    flat = []

    def walk(items, parent_id=None):
        for node in items or []:
            if not isinstance(node, dict):
                continue
            normalized = _normalize_node(node)
            entry = {k: v for k, v in normalized.items() if k != 'children'}
            entry['parent_id'] = parent_id
            flat.append(entry)
            walk(node.get('children') or [], node.get('id'))

    walk(nodes)
    return flat


def _write_or_print(payload: dict, out_path: str = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_path:
        abs_path = os.path.abspath(out_path)
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(text)
        print(json.dumps({'success': True, 'path': abs_path, 'bytes': len(text)}, ensure_ascii=False, indent=2))
    else:
        print(text)


def _print_err(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _resolve_or_die(cookies, token_or_url: str, block_id_flag: str = None):
    """Returns block_token string, or exits after printing the error JSON."""
    res = resolve_block_token(cookies, token_or_url, block_id_flag)
    if res.get('error'):
        _print_err(res)
        sys.exit(1)
    return res['block_token']


def cmd_whiteboard_list(cookies, token_or_url: str):
    """List whiteboard blocks in a docx (input must be a docx/wiki URL)."""
    docx_token = resolve_doc_token(cookies, token_or_url)
    wbs = list_whiteboards_in_docx(cookies, docx_token)
    print(json.dumps({
        'docx_token': docx_token,
        'count': len(wbs),
        'whiteboards': wbs,
    }, ensure_ascii=False, indent=2))


def cmd_whiteboard_meta(cookies, token_or_url: str, block_id_flag: str = None):
    """Fetch lightweight whiteboard meta (version, theme, createTime)."""
    bt = _resolve_or_die(cookies, token_or_url, block_id_flag)
    data = fetch_whiteboard_block(cookies, bt)
    if data.get('error'):
        _print_err(data)
        sys.exit(1)
    meta = data.get('meta') or {}
    print(json.dumps({'block_token': bt, 'meta': meta}, ensure_ascii=False, indent=2))


def cmd_whiteboard_read(
    cookies,
    token_or_url: str,
    block_id_flag: str = None,
    fmt: str = 'raw',
    out_path: str = None,
    index: int = 0,
):
    """Read the whiteboard. fmt:
      - 'raw'  (default): full nodes JSON
      - 'ai'   : AI-friendly normalized JSON with flat node list and raw nodes
      - 'code': extract syntax sourceCode (PlantUML/Mermaid). Use --index to pick the N-th container.
    """
    bt = _resolve_or_die(cookies, token_or_url, block_id_flag)
    data = fetch_whiteboard_block(cookies, bt)
    if data.get('error'):
        _print_err(data)
        sys.exit(1)

    nodes = data.get('nodes') or []

    if fmt == 'raw':
        payload = {'block_token': bt, 'meta': data.get('meta'), 'nodes': nodes}
        _write_or_print(payload, out_path)
        return

    if fmt in ('ai', 'json'):
        payload = {
            'whiteboard': {
                'block_token': bt,
                'meta': data.get('meta') or {},
                'node_count': len(nodes),
            },
            'nodes': [_normalize_node(node) for node in nodes if isinstance(node, dict)],
            'flat_nodes': _collect_flat_nodes(nodes),
            'raw_nodes': nodes,
        }
        _write_or_print(payload, out_path)
        return

    if fmt == 'code':
        containers = find_syntax_containers(nodes)
        if not containers:
            _print_err({'error': 'no PlantUML/Mermaid container found', 'block_token': bt})
            sys.exit(1)
        if index < 0 or index >= len(containers):
            _print_err({
                'error': f'--index {index} out of range; found {len(containers)} container(s)',
                'available_indexes': list(range(len(containers))),
            })
            sys.exit(1)
        c = containers[index]
        if out_path:
            abs_path = os.path.abspath(out_path)
            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(c['source_code'])
            print(json.dumps({
                'success': True, 'block_token': bt, 'path': abs_path,
                'syntax_type': c['syntax_type'], 'container_id': c['id'],
                'total_containers': len(containers), 'index': index,
            }, indent=2))
        else:
            # When sending source to stdout, prefix one info line to stderr so it stays parseable
            print(f"[whiteboard] container {index+1}/{len(containers)} id={c['id']} syntax_type={c['syntax_type']}", file=sys.stderr)
            print(c['source_code'])
        return

    _print_err({'error': f'unknown --format value: {fmt}; expected raw|ai|json|code'})
    sys.exit(1)


def cmd_whiteboard_download(
    cookies,
    token_or_url: str,
    block_id_flag: str = None,
    out_path: str = None,
):
    """Download whiteboard as PNG (server-rendered)."""
    bt = _resolve_or_die(cookies, token_or_url, block_id_flag)
    res = download_whiteboard_png(cookies, bt)
    if res.get('status') != 200 or not res.get('buffer'):
        _print_err({'error': 'Failed to download PNG', 'status': res.get('status')})
        sys.exit(1)

    buf = res['buffer']
    # Magic-byte sniff: Feishu serves Content-Type=image/png but bytes are JPEG.
    if buf[:3] == b'\xff\xd8\xff':
        ext = 'jpg'
    elif buf[:4] == b'\x89PNG':
        ext = 'png'
    else:
        ext = 'png'  # default; server told us so

    from ..paths import resolve_output_path
    out_path = resolve_output_path(out_path, f'whiteboard_{bt}.{ext}')
    abs_path = os.path.abspath(out_path)
    with open(abs_path, 'wb') as f:
        f.write(buf)
    print(json.dumps({
        'success': True,
        'block_token': bt,
        'path': abs_path,
        'bytes': len(buf),
        'format': ext,
        'content_type': res.get('contentType'),
    }, indent=2))
