"""A small QR code encoder (byte mode, error correction level M, versions 1-6: up to 106 bytes), enough for a
pairing link. It follows ISO/IEC 18004 the way Project Nayuki's reference encoder lays it out, and draws an SVG."""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

# version: (error-correction codewords per block, blocks); level M
_ECC_M = {1: (10, 1), 2: (16, 1), 3: (26, 1), 4: (18, 2), 5: (24, 2), 6: (16, 4)}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_FORMAT_M = 0   # level M's two format bits


def _mul(x: int, y: int) -> int:
    """Multiply in GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1."""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def rs_divisor(degree: int) -> List[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _mul(root, 0x02)
    return result


def rs_remainder(data: Sequence[int], divisor: Sequence[int]) -> List[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _mul(coef, factor)
    return result


def format_bits(mask: int, level: int = _FORMAT_M) -> int:
    """The 15 format bits: level and mask, BCH-protected, XORed with 101010000010010."""
    data = level << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


def _capacity(version: int) -> int:
    """Data codewords at level M."""
    ec, blocks = _ECC_M[version]
    return _total(version) // 8 - ec * blocks


def _total(version: int) -> int:
    """Raw data modules (bits) available for codewords, versions 1-6."""
    size = version * 4 + 17
    result = size * size - 3 * 64 - 2 * (size - 16) - 31   # finders with separators, timing, format, dark module
    if version >= 2:
        result -= 25   # one alignment pattern (versions 2-6 have exactly one)
    return result


def _codewords(data: bytes, version: int) -> List[int]:
    cap = _capacity(version)
    bits: List[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(n)))
    put(0b0100, 4)
    put(len(data), 8)
    for b in data:
        put(b, 8)
    put(0, min(4, cap * 8 - len(bits)))
    put(0, (-len(bits)) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < cap:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    ec_len, n_blocks = _ECC_M[version]
    per = cap // n_blocks
    divisor = rs_divisor(ec_len)
    blocks = [words[i * per:(i + 1) * per] for i in range(n_blocks)]
    eccs = [rs_remainder(b, divisor) for b in blocks]
    out = [b[i] for i in range(per) for b in blocks] + [e[i] for i in range(ec_len) for e in eccs]
    return out


class QR:
    def __init__(self, text: str):
        data = text.encode("utf-8")
        version = next((v for v in _ECC_M if len(data) + 2 <= _capacity(v)), None)
        if version is None:
            raise ValueError("too long for a QR code here (%d bytes; at most %d)" % (len(data), _capacity(6) - 2))
        self.version = version
        self.size = version * 4 + 17
        n = self.size
        self.modules = [[False] * n for _ in range(n)]
        self.function = [[False] * n for _ in range(n)]
        self._draw_functions()
        self._draw_codewords(_codewords(data, version))
        best: Optional[Tuple[int, int]] = None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format(mask)
            score = self._penalty()
            if best is None or score < best[0]:
                best = (score, mask)
            self._apply_mask(mask)   # undo
        self.mask = best[1]
        self._apply_mask(self.mask)
        self._draw_format(self.mask)

    # ------------------------------------------------------------------ layout

    def _set(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.function[y][x] = True

    def _draw_functions(self) -> None:
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _ALIGN[self.version]
        for i, cy in enumerate(pos):
            for j, cx in enumerate(pos):
                if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)   # reserve the format areas

    def _draw_format(self, mask: int) -> None:
        bits = format_bits(mask)
        n = self.size

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1
        for i in range(6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(8):
            self._set(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, bit(i))
        self._set(8, n - 8, True)   # the dark module

    def _draw_codewords(self, words: List[int]) -> None:
        n = self.size
        i = 0
        total = len(words) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.function[y][x] and i < total:
                        self.modules[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    @staticmethod
    def mask_bit(mask: int, x: int, y: int) -> bool:
        return [(x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0, (x // 3 + y // 2) % 2 == 0,
                x * y % 2 + x * y % 3 == 0, (x * y % 2 + x * y % 3) % 2 == 0,
                ((x + y) % 2 + x * y % 3) % 2 == 0][mask]

    def _apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and self.mask_bit(mask, x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty(self) -> int:
        """ISO 18004's four rules: runs, 2x2 blocks, finder look-alikes, dark/light balance."""
        n = self.size
        m = self.modules
        score = 0
        lines = [m[y] for y in range(n)] + [[m[y][x] for y in range(n)] for x in range(n)]
        finder_like = ([True, False, True, True, True, False, True, False, False, False, False],
                       [False, False, False, False, True, False, True, True, True, False, True])
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run, prev = 1, v
            if run >= 5:
                score += 3 + run - 5
            for i in range(n - 10):
                if list(line[i:i + 11]) in finder_like:
                    score += 40
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(v for row in m for v in row)
        score += abs(dark * 20 - n * n * 10) // (n * n) * 10
        return score

    # ------------------------------------------------------------------ output

    def svg(self, scale: int = 6, border: int = 4) -> str:
        n = self.size + 2 * border
        path = "".join("M%d,%dh1v1h-1z" % (x + border, y + border)
                       for y in range(self.size) for x in range(self.size) if self.modules[y][x])
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
                'shape-rendering="crispEdges" role="img" aria-label="QR code"><rect width="100%%" height="100%%" '
                'fill="#fff"/><path d="%s" fill="#000"/></svg>' % (n, n, n * scale, n * scale, path))
