"""
proto.py - Protobuf encoder/decoder (no .proto schema needed)

Mirrors the JS protobufjs-based implementation in lib/proto.js.
"""

import struct


# ==========================================
# Varint helpers
# ==========================================

def encode_varint(n: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
    n = n & 0xFFFFFFFFFFFFFFFF  # treat as uint64
    if n == 0:
        return b'\x00'
    result = bytearray()
    while n > 0:
        bits = n & 0x7F
        n >>= 7
        if n > 0:
            bits |= 0x80
        result.append(bits)
    return bytes(result)


def decode_varint(data: bytes, pos: int):
    """Decode a varint from data starting at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


# ==========================================
# Protobuf encoder (supports repeated fields via lists)
# ==========================================

def encode_message(fields: dict) -> bytes:
    """
    Encode a dict of {field_number: value} to protobuf bytes.
    Field numbers can be int or str (will be converted to int).
    Values:
      - int / bool -> varint (wire type 0)
      - float      -> double (wire type 1)
      - str        -> UTF-8 string (wire type 2)
      - bytes      -> raw bytes (wire type 2)
      - dict       -> nested message (wire type 2)
      - list       -> repeated field (each item written separately)
      - None       -> skipped
    """
    buf = bytearray()
    for k, value in fields.items():
        num = int(k)
        if value is None:
            continue
        if isinstance(value, list):
            for item in value:
                _write_field(buf, num, item)
        else:
            _write_field(buf, num, value)
    return bytes(buf)


def _write_field(buf: bytearray, num: int, value) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        buf.extend(encode_varint((num << 3) | 0))
        buf.extend(encode_varint(1 if value else 0))
    elif isinstance(value, int):
        buf.extend(encode_varint((num << 3) | 0))
        buf.extend(encode_varint(value & 0xFFFFFFFFFFFFFFFF))
    elif isinstance(value, float):
        buf.extend(encode_varint((num << 3) | 1))
        buf.extend(struct.pack('<d', value))
    elif isinstance(value, str):
        encoded = value.encode('utf-8')
        buf.extend(encode_varint((num << 3) | 2))
        buf.extend(encode_varint(len(encoded)))
        buf.extend(encoded)
    elif isinstance(value, (bytes, bytearray)):
        buf.extend(encode_varint((num << 3) | 2))
        buf.extend(encode_varint(len(value)))
        buf.extend(value)
    elif isinstance(value, dict):
        nested = encode_message(value)
        buf.extend(encode_varint((num << 3) | 2))
        buf.extend(encode_varint(len(nested)))
        buf.extend(nested)


# ==========================================
# Generic protobuf decoder
# ==========================================

def generic_decode(buffer, depth: int = 0):
    """
    Decode a protobuf buffer to a dict {'fN': value} without a schema.
    - Wire type 0 (varint)  -> str (matching JS int64.toString())
    - Wire type 1 (64-bit)  -> float
    - Wire type 2 (len-del) -> str if printable, nested dict if parseable, else '<bytes:N>'
    - Wire type 5 (32-bit)  -> float
    """
    if depth > 10 or not buffer:
        return None
    data = bytes(buffer)
    result = {}
    pos = 0
    try:
        while pos < len(data):
            tag, pos = decode_varint(data, pos)
            field_number = tag >> 3
            wire_type = tag & 7
            key = f'f{field_number}'

            if wire_type == 0:
                value, pos = decode_varint(data, pos)
                value = str(value)  # match JS: int64.toString()
            elif wire_type == 1:
                if pos + 8 > len(data):
                    break
                value = struct.unpack_from('<d', data, pos)[0]
                pos += 8
            elif wire_type == 2:
                length, pos = decode_varint(data, pos)
                if pos + length > len(data):
                    break
                raw_bytes = data[pos:pos + length]
                pos += length

                try:
                    text = raw_bytes.decode('utf-8')
                    is_printable = bool(text) and all(
                        '\x20' <= c <= '\x7e' or '\u4e00' <= c <= '\u9fff'
                        or '\u0080' <= c <= '\uffff' or c in '\n\r\t'
                        for c in text
                    )
                except (UnicodeDecodeError, ValueError):
                    text = None
                    is_printable = False

                if is_printable and length < 2000:
                    value = text
                else:
                    nested = None
                    try:
                        nested = generic_decode(raw_bytes, depth + 1)
                    except Exception:
                        pass
                    if nested and nested:
                        value = nested
                    elif is_printable:
                        value = text
                    else:
                        value = f'<bytes:{length}>'
            elif wire_type == 5:
                if pos + 4 > len(data):
                    break
                value = struct.unpack_from('<f', data, pos)[0]
                pos += 4
            else:
                # Unknown wire type — stop parsing
                break

            if key in result:
                existing = result[key]
                if not isinstance(existing, list):
                    result[key] = [existing]
                result[key].append(value)
            else:
                result[key] = value
    except Exception:
        pass
    return result


# ==========================================
# Raw protobuf field navigator (preserves binary bytes)
# ==========================================

def extract_raw_field(buffer, field_number: int, want_wire_type=None):
    """
    Extract a specific field from a protobuf buffer, returning raw bytes for
    wire type 2 (length-delimited). Returns None if not found.
    """
    if not buffer:
        return None
    data = bytes(buffer)
    pos = 0
    try:
        while pos < len(data):
            tag, pos = decode_varint(data, pos)
            fn = tag >> 3
            wt = tag & 7

            if fn == field_number and (want_wire_type is None or wt == want_wire_type):
                if wt == 2:
                    length, pos = decode_varint(data, pos)
                    return bytes(data[pos:pos + length])
                elif wt == 0:
                    value, pos = decode_varint(data, pos)
                    return str(value)
                elif wt == 1:
                    value = struct.unpack_from('<d', data, pos)[0]
                    pos += 8
                    return value
                elif wt == 5:
                    value = struct.unpack_from('<f', data, pos)[0]
                    pos += 4
                    return value

            # Skip this field
            if wt == 0:
                _, pos = decode_varint(data, pos)
            elif wt == 1:
                pos += 8
            elif wt == 2:
                length, pos = decode_varint(data, pos)
                pos += length
            elif wt == 5:
                pos += 4
            else:
                break
    except Exception:
        pass
    return None


def extract_raw_path(buffer, field_path: list):
    """Navigate nested protobuf: extract_raw_path(buf, [5, 2, 3]) -> field5.field2.field3"""
    current = buffer
    for fn in field_path:
        current = extract_raw_field(current, fn, 2)  # wire type 2 = length-delimited
        if not current:
            return None
    return current


# ==========================================
# Packet encode/decode (known schema)
# Packet fields: 1=sid(int64), 2=payload_type(int32), 3=cmd(int32),
#                4=status(int32), 5=payload(bytes), 6=cid(string)
# ==========================================

def encode_packet(payload_type: int, cmd: int, payload: bytes, cid: str) -> bytes:
    fields = {}
    if payload_type:
        fields[2] = payload_type
    fields[3] = cmd
    if payload:
        fields[5] = payload
    if cid:
        fields[6] = cid
    return encode_message(fields)


def decode_packet(buffer) -> dict:
    """Decode the outer Packet structure, returning payload as raw bytes."""
    data = bytes(buffer)
    pos = 0
    packet = {}
    try:
        while pos < len(data):
            tag, pos = decode_varint(data, pos)
            fn = tag >> 3
            wt = tag & 7
            if wt == 0:
                value, pos = decode_varint(data, pos)
                packet[fn] = value
            elif wt == 2:
                length, pos = decode_varint(data, pos)
                raw = data[pos:pos + length]
                pos += length
                if fn == 5:
                    packet[5] = bytes(raw)  # payload: keep as raw bytes
                elif fn == 6:
                    packet[6] = raw.decode('utf-8', errors='replace')  # cid
                else:
                    packet[fn] = bytes(raw)
            elif wt == 1:
                pos += 8
            elif wt == 5:
                pos += 4
            else:
                break
    except Exception:
        pass
    return {
        'sid': packet.get(1, 0),
        'payload_type': packet.get(2, 0),
        'cmd': packet.get(3, 0),
        'status': packet.get(4, 0),
        'payload': packet.get(5, b''),
        'cid': packet.get(6, ''),
    }
