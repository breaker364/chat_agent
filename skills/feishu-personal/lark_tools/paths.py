"""
paths.py — shared `--out` resolution for all download commands.

Each download subcommand (minutes/doc/sheet/bitable/whiteboard/img/file)
computes its own default filename (server-provided title, content-disposition
header, magic-byte ext sniff, etc. — never uniform). What IS uniform is how
we interpret the user's `--out` argument once that default is in hand. This
module owns just that step.

Semantics mirror `cp` / `wget -O`:

    --out absent or empty   → return default_filename (cwd-relative)
    --out ends with `/`     → treat as directory, join default filename
    --out is existing dir   → same
    --out anything else     → treat as full file path

Side effects: `mkdir -p` on the target directory (or the file's parent).
`~` and `$VAR` are expanded so callers don't have to pre-process.
"""

import os


def resolve_output_path(out_arg, default_filename: str) -> str:
    """Resolve `--out` to a final on-disk file path.

    See module docstring for the cp-like decision table. Returns the path
    the caller should pass to `open()`; auto-creates any missing parent
    directories (or the target itself, if dir-shaped).
    """
    if not out_arg:
        return default_filename
    out_arg = os.path.expanduser(os.path.expandvars(out_arg))
    if out_arg.endswith(('/', os.sep)) or os.path.isdir(out_arg):
        os.makedirs(out_arg, exist_ok=True)
        return os.path.join(out_arg, default_filename)
    parent = os.path.dirname(out_arg)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return out_arg
