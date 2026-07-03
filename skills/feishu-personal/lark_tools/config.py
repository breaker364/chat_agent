"""
config.py - Shared constants for lark_cli
"""

import os

# scripts/lib/config.py → scripts/lib → scripts → project root (skill root)
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)

# Cross-platform user data directory (~/.lark_cli/)
DATA_DIR = os.path.join(os.path.expanduser('~'), '.lark_cli')

GATEWAY_HOST = 'internal-api-lark-api.feishu.cn'
DOC_HOST = 'nio.feishu.cn'
BITABLE_HOST = 'www.feishu.cn'
DOC_IMAGE_HOST = 'internal-api-drive-stream.feishu.cn'
# docx image upload control-flow host (prepare/blocks/finish).
# Separate from DOC_IMAGE_HOST which is the data-flow host (merge_block).
UPLOAD_HOST = 'internal-api-space.feishu.cn'
