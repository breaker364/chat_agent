"""
commands/whiteboard_write.py - Whiteboard (画板) write commands.

Phase 2: create (empty whiteboard inside a docx), plantuml, mermaid.
Read commands live in whiteboard.py.
"""

import json
import os
import sys

from ..whiteboard_api import fetch_whiteboard_block, resolve_block_token
from ..whiteboard_ot import (
    SYNTAX_PLANTUML, SYNTAX_MERMAID,
    build_user_change_envelope, create_whiteboard, fetch_current_user_id,
    fetch_ws_ticket, new_member_id, new_page_id,
    op_create, op_delete, parse_syntax, submit_user_change,
)
from ..commands.doc import resolve_doc_token


# ---------------------------------------------------------------------------
# Source-input helpers
# ---------------------------------------------------------------------------

def _read_source(text_arg: str = None, source_path: str = None) -> str:
    """Resolve source code from a path, '-' (stdin), or inline text.

    `source_path` precedence:
      - '-' → read stdin
      - real file path → read file
      - None → fall through to text_arg
    """
    if source_path == '-':
        return sys.stdin.read()
    if source_path:
        if not os.path.isfile(source_path):
            raise FileNotFoundError(f'source file not found: {source_path}')
        with open(source_path, 'r', encoding='utf-8') as f:
            return f.read()
    if text_arg is not None:
        return text_arg
    raise ValueError('no source provided (pass <file>, --source <file|->, or --text)')


def _print_err_exit(payload: dict, code: int = 1):
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(code)


def _compute_bbox(top_nodes: list) -> dict:
    """Compute a baseV2 bounding box around parse_syntax's top-level shapes.

    Used to populate the wrapper container's baseV2. The server uses this for
    initial layout; small inaccuracies are tolerated.
    """
    xs, ys, x2s, y2s = [], [], [], []
    for n in top_nodes:
        b = ((n or {}).get('info') or {}).get('baseV2') or {}
        x = b.get('x'); y = b.get('y'); w = b.get('width'); h = b.get('height')
        if None in (x, y, w, h):
            continue
        xs.append(x); ys.append(y)
        x2s.append(x + w); y2s.append(y + h)
    if not xs:
        # Reasonable default if parse_syntax yielded no positioned shapes
        return {'x': 0, 'y': 0, 'width': 800, 'height': 600}
    return {
        'x': min(xs),
        'y': min(ys),
        'width': max(x2s) - min(xs),
        'height': max(y2s) - min(ys),
    }


# ---------------------------------------------------------------------------
# create — empty whiteboard inside a docx
# ---------------------------------------------------------------------------

def cmd_whiteboard_create(cookies, docx_token_or_url: str):
    """Create an empty whiteboard appended to the given docx.

    Output: {block_id, block_token, docx_token, url}
    """
    docx_token = resolve_doc_token(cookies, docx_token_or_url)
    try:
        res = create_whiteboard(cookies, docx_token)
    except Exception as e:
        _print_err_exit({'error': f'create failed: {e}'})

    out = {
        'success': True,
        'docx_token': docx_token,
        'block_id': res['block_id'],
        'block_token': res['block_token'],
        'docx_url': f'https://nio.feishu.cn/docx/{docx_token}',
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# plantuml / mermaid — render syntax into whiteboard
# ---------------------------------------------------------------------------

def _render_and_write(
    cookies,
    block_token: str,
    code: str,
    syntax_type: int,
    overwrite: bool,
    dry_run: bool,
) -> dict:
    """Shared core for plantuml/mermaid commands.

    Returns a result dict. On dry_run, includes 'dry_run': True.
    """
    # Always parse first so dry-run can report what would happen
    new_nodes = parse_syntax(cookies, block_token, code, syntax_type)
    if not new_nodes:
        raise RuntimeError('parse_syntax returned 0 nodes (check source code for syntax errors)')

    # If --overwrite, walk the current tree and emit nodeDelete ops in DFS
    # post-order (leaves first, then parents). Sibling order is reversed so
    # earlier indices stay valid as later siblings are removed.
    delete_ops = []
    existing_total = 0
    if overwrite:
        block = fetch_whiteboard_block(cookies, block_token)
        if block.get('error'):
            raise RuntimeError(f'fetch before overwrite failed: {block}')
        existing_nodes = block.get('nodes') or []

        def _collect_deletes(siblings, parent_path):
            nonlocal existing_total
            # Iterate right-to-left so indices into `siblings` stay valid on server
            for i in range(len(siblings) - 1, -1, -1):
                n = siblings[i] or {}
                nid = n.get('id') or ''
                children = n.get('children') or []
                # children first (post-order)
                if children:
                    _collect_deletes(children, parent_path + [nid])
                if nid:
                    delete_ops.append(op_delete(nid, path=list(parent_path), index=i))
                    existing_total += 1

        _collect_deletes(existing_nodes, [])

    # parse_syntax returns only the rendered shapes — we wrap them in a
    # synthetic `syntaxProps` container (id `t1:1`, `type: 31`) so the picture
    # is editable as PlantUML/Mermaid afterwards. Without this wrapper the
    # server panics (nil dereference) because it expects every shape to live
    # under such a container when posted via /whiteboard/user_change.
    wrapper_id = 't1:1'
    bbox = _compute_bbox(new_nodes)
    wrapper_data = {
        'baseV2': bbox,
        'title': '',
        'theme': {'fillColorCode': 0, 'borderStyleCode': 0},
        'extraTextInfo': {
            'info1': {'themeCapability': {'fillColorCode': 0}},
            'info2': {'themeCapability': {'fillColorCode': 0}},
        },
        'syntaxProps': {
            'sourceCode': code,
            'syntaxType': syntax_type,
            'diagramType': 0,  # 0 = auto-detect (matches official CLI's parse_mode/diagram_type)
            'styleType': 0,
            'type': 31,
        },
    }

    create_ops = [op_create(wrapper_data, wrapper_id, path=[], index=0)]

    # Flatten parse_syntax tree as descendants of the wrapper. Each node →
    # one nodeCreate op; `path` is the chain of parent ids. Server rejects
    # nested `data.children`, so we must emit children as separate ops.
    def _walk(nodes, parent_path):
        for i, n in enumerate(nodes or []):
            nid = n.get('id') or ''
            info = n.get('info') or {}
            create_ops.append(op_create(dict(info), nid, path=list(parent_path), index=i))
            children = n.get('children') or []
            if children:
                _walk(children, parent_path + [nid])

    _walk(new_nodes, [wrapper_id])

    summary = {
        'block_token': block_token,
        'syntax_type': syntax_type,
        'new_nodes': len(new_nodes),
        'deleted_nodes': len(delete_ops),
        'total_ops': len(delete_ops) + len(create_ops),
    }

    if dry_run:
        summary['dry_run'] = True
        summary['message'] = (
            f'[dry-run] would parse {len(new_nodes)} new top-level shape(s) '
            f'(plus syntaxProps wrapper)'
            + (f', delete {existing_total} existing node(s) recursively' if overwrite else '')
            + '. Re-run without --dry-run to commit.'
        )
        return summary

    # Fetch shared auth context once
    user_ticket = fetch_ws_ticket(cookies)
    user_id = fetch_current_user_id(cookies)
    page_id = new_page_id()
    member_id = new_member_id()

    def _read_base_seq() -> int:
        block_meta = fetch_whiteboard_block(cookies, block_token)
        if block_meta.get('error'):
            raise RuntimeError(f'fetch baseSeq failed: {block_meta}')
        return (block_meta.get('meta') or {}).get('appliedVersion') or 0

    def _submit(ops_batch: list) -> dict:
        base_seq = _read_base_seq()
        env = build_user_change_envelope(
            block_token=block_token,
            ops=ops_batch,
            member_id=member_id,
            user_ticket=user_ticket,
            user_id=user_id,
            page_id=page_id,
            base_seq=base_seq,
        )
        return submit_user_change(cookies, env, member_id)

    # Submit in two passes so the new-node ids (t1:1, r1:*, ...) don't collide
    # with existing ones during the same OT batch. Empirically the server
    # rejects (4002000 "invalid option") when delete + recreate use the same
    # prefix in one commit; splitting cleanly avoids it.
    last_resp = None
    if delete_ops:
        last_resp = _submit(delete_ops)
    if create_ops:
        last_resp = _submit(create_ops)

    summary['success'] = True
    summary['seq'] = (last_resp or {}).get('seq')
    summary['edit_time'] = (last_resp or {}).get('editTime')
    return summary


def _cmd_syntax_write(
    cookies,
    token_or_url: str,
    block_id_flag: str,
    source_path: str,
    text_arg: str,
    overwrite: bool,
    dry_run: bool,
    syntax_type: int,
    syntax_label: str,
):
    # Resolve block_token
    resolved = resolve_block_token(cookies, token_or_url, block_id_flag)
    if resolved.get('error'):
        _print_err_exit(resolved)
    block_token = resolved['block_token']

    # Resolve source
    try:
        code = _read_source(text_arg=text_arg, source_path=source_path)
    except (FileNotFoundError, ValueError) as e:
        _print_err_exit({'error': str(e)})
    if not code.strip():
        _print_err_exit({'error': f'empty {syntax_label} source'})

    try:
        result = _render_and_write(
            cookies, block_token, code, syntax_type, overwrite=overwrite, dry_run=dry_run,
        )
    except Exception as e:
        _print_err_exit({'error': f'{syntax_label} write failed: {e}'})

    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_whiteboard_plantuml(
    cookies, token_or_url: str, block_id_flag: str = None,
    source_path: str = None, text_arg: str = None,
    overwrite: bool = False, dry_run: bool = False,
):
    _cmd_syntax_write(
        cookies, token_or_url, block_id_flag, source_path, text_arg,
        overwrite, dry_run, SYNTAX_PLANTUML, 'plantuml',
    )


def cmd_whiteboard_mermaid(
    cookies, token_or_url: str, block_id_flag: str = None,
    source_path: str = None, text_arg: str = None,
    overwrite: bool = False, dry_run: bool = False,
):
    _cmd_syntax_write(
        cookies, token_or_url, block_id_flag, source_path, text_arg,
        overwrite, dry_run, SYNTAX_MERMAID, 'mermaid',
    )
