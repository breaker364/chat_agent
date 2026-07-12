"""
crypto.py - Image decryption helpers for Feishu chat images.

Feishu encrypts ALL chat images with AES-256-GCM (cipherType=1) or AES-256-CBC (cipherType=2).
The key and nonce are embedded in the message protobuf payload.
"""

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .proto import extract_raw_field, extract_raw_path, decode_varint


def extract_image_crypto(msg_buffer: bytes):
    """
    Extract image crypto params from a message payload buffer (for type-5 image messages).
    Structure: msg.f5.f2.f3 = { f1=cipherType, f2={ f1=secretKey(32B), f2=secretNonce(12B) } }
    """
    crypto_container = extract_raw_path(msg_buffer, [5, 2, 3])
    if not crypto_container:
        return None

    cipher_type = extract_raw_field(crypto_container, 1, 0)  # varint
    key_nonce_container = extract_raw_field(crypto_container, 2, 2)  # length-delimited
    if not key_nonce_container:
        return None

    secret_key = extract_raw_field(key_nonce_container, 1, 2)    # 32 bytes
    secret_nonce = extract_raw_field(key_nonce_container, 2, 2)  # 12 bytes

    if not secret_key or not secret_nonce:
        return None
    return {
        'cipherType': int(cipher_type) if cipher_type else 1,
        'secretKey': secret_key,
        'secretNonce': secret_nonce,
    }


def extract_embedded_image_crypto(msg_buffer: bytes, target_image_key: str):
    """
    Extract image crypto from embedded images in rich text messages (type 2).
    Structure: msg.f5.f6.f3.f1[].f2.f3.f17.f3 = crypto container
    """
    rich_text = extract_raw_path(msg_buffer, [5, 6, 3])
    if not rich_text:
        return None

    # Iterate over repeated f1 elements
    data = bytes(rich_text)
    pos = 0
    try:
        while pos < len(data):
            tag, pos = decode_varint(data, pos)
            fn = tag >> 3
            wt = tag & 7
            if fn == 1 and wt == 2:
                length, pos = decode_varint(data, pos)
                element_buf = bytes(data[pos:pos + length])
                pos += length

                # element.f2.f3.f17 = image info
                image_info = extract_raw_path(element_buf, [2, 3, 17])
                if image_info:
                    img_key = extract_raw_field(image_info, 1, 2)
                    if img_key and img_key.decode('utf-8', errors='replace') == target_image_key:
                        crypto_container = extract_raw_field(image_info, 3, 2)
                        if crypto_container:
                            cipher_type = extract_raw_field(crypto_container, 1, 0)
                            key_nonce_container = extract_raw_field(crypto_container, 2, 2)
                            if key_nonce_container:
                                secret_key = extract_raw_field(key_nonce_container, 1, 2)
                                secret_nonce = extract_raw_field(key_nonce_container, 2, 2)
                                if secret_key and secret_nonce:
                                    return {
                                        'cipherType': int(cipher_type) if cipher_type else 1,
                                        'secretKey': secret_key,
                                        'secretNonce': secret_nonce,
                                    }
            else:
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


def decrypt_image_buffer(encrypted_buf: bytes, secret_key: bytes, secret_nonce: bytes, cipher_type: int = 1) -> bytes:
    """Decrypt an image buffer using AES-256-GCM (type 1) or AES-256-CBC (type 2)."""
    if cipher_type == 1:
        # AES-256-GCM: Python's AESGCM expects ciphertext+tag (tag appended at end)
        # The JS code splits tag off the end; AESGCM.decrypt handles the same format
        aesgcm = AESGCM(bytes(secret_key))
        return aesgcm.decrypt(bytes(secret_nonce), bytes(encrypted_buf), None)
    elif cipher_type == 2:
        # AES-256-CBC
        cipher = Cipher(algorithms.AES(bytes(secret_key)), modes.CBC(bytes(secret_nonce)))
        decryptor = cipher.decryptor()
        return decryptor.update(bytes(encrypted_buf)) + decryptor.finalize()
    raise ValueError(f'Unsupported cipher type: {cipher_type}')
