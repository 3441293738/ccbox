"""Bounded clipboard data protocol; no shell commands or destination paths."""
import struct

MAX_IMAGE = 12 * 1024 * 1024
MAX_TEXT = 1024 * 1024
PROTOCOL = b'CCBOX-CLIPBOARD/1\n'
TYPES = {
    'image': ('image/png', MAX_IMAGE),
    'text': ('text/plain', MAX_TEXT),
}


def validate(kind, payload):
    if kind not in TYPES or not 0 < len(payload) <= TYPES[kind][1]:
        raise ValueError('invalid clipboard size or type')
    if kind == 'text':
        text = payload.decode('utf-8', errors='strict')
        if '\0' in text:
            raise ValueError('NUL is not supported by the X11 clipboard')
        return 'UTF8_STRING'
    if (len(payload) < 33 or payload[:8] != b'\x89PNG\r\n\x1a\n'
            or payload[8:16] != b'\0\0\0\rIHDR'):
        raise ValueError('invalid PNG header')
    width, height = struct.unpack('>II', payload[16:24])
    if not 1 <= width <= 4096 or not 1 <= height <= 4096:
        raise ValueError('PNG dimensions exceed limit')
    return 'image/png'
