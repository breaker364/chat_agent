"""
sheet_decoder.py — low-level protobuf scanner for Lark spreadsheet cell blocks.

Distinct from proto.generic_decode in two ways:
  1. Wire type 2 (length-delimited) bytes are kept RAW. The caller must
     re-invoke decode_proto_raw on them to descend into nested messages.
     generic_decode tries to interpret bytes as utf-8 / nested-dict
     automatically, which mangles the binary index_map / cell_meta that
     sheet cell blocks use.
  2. Varints (wire type 0) and fixed-width fields (1, 5) return Python ints,
     not strings or floats. generic_decode stringifies varints to match
     the JS bundle's int64.toString() output, which is wrong for our use.

Used by:
  - commands/sheet.py::_parse_cell_block — standalone spreadsheet read
  - commands/doc.py::fetch_sheet_markdown_table — embedded sheets in docx
"""

import struct

from .proto import decode_varint


def decode_proto_raw(buf) -> dict:
    """Decode a protobuf buffer into {fN: value} without a schema.

    Behavior:
      - wire type 0 (varint)  → int
      - wire type 1 (64-bit)  → int (little-endian uint64)
      - wire type 2 (len-del) → bytes (raw, NOT decoded as utf-8)
      - wire type 5 (32-bit)  → int (little-endian uint32)
      - unknown wire type     → stop parsing, return what's accumulated
      - repeated field        → values collected into a list

    Always returns a dict. Malformed input doesn't raise; the function
    swallows decode errors and returns whatever has been parsed so far —
    callers downstream rely on that resilience when scanning cell blocks
    that may have stray trailing bytes.
    """
    result = {}
    pos = 0
    data = bytes(buf)
    try:
        while pos < len(data):
            tag, pos = decode_varint(data, pos)
            fn = tag >> 3
            wt = tag & 7
            key = f'f{fn}'
            if wt == 0:
                val, pos = decode_varint(data, pos)
            elif wt == 1:
                val = struct.unpack_from('<Q', data, pos)[0]
                pos += 8
            elif wt == 2:
                length, pos = decode_varint(data, pos)
                val = data[pos:pos + length]
                pos += length
            elif wt == 5:
                val = struct.unpack_from('<I', data, pos)[0]
                pos += 4
            else:
                break
            if key not in result:
                result[key] = val
            elif isinstance(result[key], list):
                result[key].append(val)
            else:
                result[key] = [result[key], val]
    except Exception:
        pass
    return result
