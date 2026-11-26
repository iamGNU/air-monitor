"""Minimal LoRa physical layer in plain Python (no numpy): encode bytes into chirps and decode chirps back into
bytes, matched to the v0.1 radio settings (SX1281: SF7, BW 1625 kHz, CR 4/5, explicit header, CRC on,
sync word 0x12, 12-symbol preamble). Used to transmit test packets from a HackRF and to check captures.

Coding steps follow the open LoRa reverse-engineering work (Tapparel et al., gr-lora_sdr):
whitening -> header + CRC -> Hamming (header block 4/8) -> diagonal interleaving -> Gray -> chirp shift.

Usage:
  python3 lora_phy.py decode CAPTURE.iq [FS]          int8 IQ from hackrf_transfer -r (default FS 6.5 MS/s)
  python3 lora_phy.py encode OUT.iq "text" [COUNT] [GAP_MS]   (text up to 11 chars; HackRF tuned to 2438 MHz)
"""
import cmath
import math
import sys

SF, BW, CR, SYNC, PREAMBLE = 7, 1_625_000, 1, 0x12, 12
# Filler nibbles the SX1281 puts after the CRC in the last block (measured from its own packets, 20-byte
# payload, 16-11-2026). Receivers ignore them; copying them makes our symbols identical to the SX1281's.
PAD = [10, 3, 8, 13, 6]
N = 1 << SF


def configure(sf=7, bw=1_625_000, cr=1):
    """Use other radio settings (cr: 1-4 for 4/5-4/8). Rebinds the module constants used by every function."""
    global SF, BW, CR, N, DOWN, UP
    SF, BW, CR, N = sf, bw, cr, 1 << sf
    DOWN = [cmath.exp(-1j * math.pi * (u * u / N - u)) for u in range(N)]
    UP = [z.conjugate() for z in DOWN]


# --------------------------------------------------------------------------------------------- bit tools
def whitening_seq(n):
    s, out = 0xFF, []
    for _ in range(n):
        out.append(s)
        fb = ((s >> 7) ^ (s >> 5) ^ (s >> 4) ^ (s >> 3)) & 1
        s = ((s << 1) | fb) & 0xFF
    return out


def gray(x):
    return x ^ (x >> 1)


def gray_inv(g):
    x = 0
    while g:
        x ^= g
        g >>= 1
    return x


def crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def payload_crc(pl):
    c = crc16(pl[:-2]) if len(pl) >= 2 else 0
    return c ^ pl[-1] ^ (pl[-2] << 8)


def header_nibbles(length, cr=None, has_crc=1):
    cr = CR if cr is None else cr
    n0, n1, n2 = length >> 4, length & 0xF, (cr << 1) | has_crc
    b = lambda v, k: (v >> k) & 1
    c4 = b(n0, 3) ^ b(n0, 2) ^ b(n0, 1) ^ b(n0, 0)
    c3 = b(n0, 3) ^ b(n1, 3) ^ b(n1, 2) ^ b(n1, 1) ^ b(n2, 0)
    c2 = b(n0, 2) ^ b(n1, 3) ^ b(n1, 0) ^ b(n2, 3) ^ b(n2, 1)
    c1 = b(n0, 1) ^ b(n1, 2) ^ b(n1, 0) ^ b(n2, 2) ^ b(n2, 1) ^ b(n2, 0)
    c0 = b(n0, 0) ^ b(n1, 1) ^ b(n2, 3) ^ b(n2, 2) ^ b(n2, 1) ^ b(n2, 0)
    return [n0, n1, n2, c4, (c3 << 3) | (c2 << 2) | (c1 << 1) | c0]


def hamming_enc(nib, cr):
    d = [(nib >> 3) & 1, (nib >> 2) & 1, (nib >> 1) & 1, nib & 1]          # MSB first, as gr-lora_sdr
    if cr == 1:
        p4 = d[0] ^ d[1] ^ d[2] ^ d[3]
        return d[3] << 4 | d[2] << 3 | d[1] << 2 | d[0] << 1 | p4
    p0, p1, p2, p3 = d[3] ^ d[2] ^ d[1], d[2] ^ d[1] ^ d[0], d[3] ^ d[2] ^ d[0], d[3] ^ d[1] ^ d[0]
    return (d[3] << 7 | d[2] << 6 | d[1] << 5 | d[0] << 4 | p0 << 3 | p1 << 2 | p2 << 1 | p3) >> (4 - cr)


def hamming_data(cw, cr):
    """data nibble from a codeword (cw bits MSB first: nibble LSB first, then parity). No correction."""
    bits = cw >> cr                                    # top 4 bits = b0 b1 b2 b3
    return ((bits >> 3) & 1) | ((bits >> 2) & 1) << 1 | ((bits >> 1) & 1) << 2 | (bits & 1) << 3


def bits_msb(v, n):
    return [(v >> (n - 1 - k)) & 1 for k in range(n)]


def from_bits(b):
    v = 0
    for x in b:
        v = (v << 1) | x
    return v


# --------------------------------------------------------------------------------------------- encode
def encode_symbols(payload):
    payload = bytes(payload)
    ws = whitening_seq(len(payload))
    nibs = header_nibbles(len(payload))
    for i, byte in enumerate(payload):
        w = byte ^ ws[i]
        nibs += [w & 0xF, w >> 4]
    c = payload_crc(payload)
    nibs += [c & 0xF, (c >> 4) & 0xF, (c >> 8) & 0xF, (c >> 12) & 0xF]

    syms, pos, first = [], 0, True
    while pos < len(nibs):
        sf_app = SF - 2 if first else SF
        cr_app = 4 if first else CR
        cw_len = 4 + cr_app
        block = nibs[pos:pos + sf_app]
        block += (PAD * 3)[:sf_app - len(block)]
        pos += sf_app
        cw = [bits_msb(hamming_enc(n, cr_app), cw_len) for n in block]
        for i in range(cw_len):
            ib = [cw[(i - j - 1) % sf_app][i] for j in range(sf_app)]
            if first:
                ib += [sum(ib) % 2, 0]                     # reduced rate: parity bit and a zero
            v = from_bits(ib)
            syms.append((gray_inv(v) + 1) % N)
        first = False
    return syms


def modulate(symbols, fs, sfd_shift=0.0):
    os_ = fs / BW
    out, phase = [], 0.0

    def chirp(k, up=True, frac=1.0):
        nonlocal phase
        for n in range(int(round(N * os_ * frac))):
            u = n / os_
            f = (((k + u) % N) / N - 0.5) * BW if up else (0.5 - ((u + sfd_shift) % N) / N) * BW
            phase += 2 * math.pi * f / fs
            out.append(cmath.exp(1j * phase))

    for _ in range(PREAMBLE):
        chirp(0)
    chirp((SYNC >> 4) * 8)
    chirp((SYNC & 0xF) * 8)
    chirp(0, up=False); chirp(0, up=False); chirp(0, up=False, frac=0.25)
    for s in symbols:
        chirp(s)
    return out


# --------------------------------------------------------------------------------------------- decode
def fft(x):
    n = len(x)
    if n == 1:
        return x
    ev, od = fft(x[0::2]), fft(x[1::2])
    t = [cmath.exp(-2j * math.pi * k / n) * od[k] for k in range(n // 2)]
    return [ev[k] + t[k] for k in range(n // 2)] + [ev[k] - t[k] for k in range(n // 2)]


DOWN = [cmath.exp(-1j * math.pi * (u * u / N - u)) for u in range(N)]     # conj of a base upchirp
UP = [z.conjugate() for z in DOWN]


def peak(win, ref):
    spec = fft([a * b for a, b in zip(win, ref)])
    mags = [abs(z) for z in spec]
    k = max(range(N), key=mags.__getitem__)
    return k, mags[k]


def read_iq(path, fs):
    """int8 IQ file -> list of complex samples at the chip rate (boxcar average over the oversampling)."""
    import array
    raw = array.array("b", open(path, "rb").read())
    os_ = int(round(fs / BW))
    I, Q = raw[0::2], raw[1::2]
    # conjugate: in HackRF IQ the SX1281's standard-IQ LoRa chirps run downwards (seen on the bench, 16-11-2026)
    return [complex(sum(I[i:i + os_]), -sum(Q[i:i + os_])) / os_ for i in range(0, len(I) - os_, os_)]


def fpeak(win, ref):
    """dechirped FFT peak with parabolic interpolation: (fractional bin, magnitude)"""
    spec = fft([u * v for u, v in zip(win, ref)])
    m = [abs(z) for z in spec]
    k = max(range(N), key=m.__getitem__)
    a, b, c = m[(k - 1) % N], m[k], m[(k + 1) % N]
    d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else 0.0
    return (k + d) % N, b


def wrap(v):
    return (v + N / 2) % N - N / 2


def decode_symbols(x, start):
    """x at chip rate, start = index inside the preamble. Finds the sync word, then picks the symbol timing
    that concentrates the most energy in the payload chirps, removes the frequency offset measured on the
    aligned preamble, and returns (sync, symbols)."""
    g = start
    p0, _ = fpeak(x[g:g + N], DOWN)
    k = g
    while True:                                      # walk the preamble until the sync word appears
        b, _ = fpeak(x[k:k + N], DOWN)
        if abs(wrap(b - p0)) > 1.5:
            break
        k += N
        if k > g + 20 * N:
            return None, None
    sfd = 2 * N + int(2.25 * N)

    def energy(t, nsym=16):
        j0 = k + t + sfd
        return sum(fpeak(x[j0 + i * N:j0 + (i + 1) * N], DOWN)[1] for i in range(nsym))

    t = max(range(-N // 2, N // 2, 4), key=energy)
    t = max(range(t - 3, t + 4), key=energy)
    ks = k + t                                        # aligned start of the first sync symbol
    pre = [fpeak(x[j:j + N], DOWN)[0] for j in range(ks - 6 * N, ks - N, N)]
    c = (pre[0] + sum(wrap(v - pre[0]) for v in pre) / len(pre)) % N

    def sym(j):
        v, _ = fpeak(x[j:j + N], DOWN)
        return int(round(wrap(v - c))) % N

    sync = (sym(ks), sym(ks + N))
    pay, syms = ks + sfd, []
    while pay + N <= len(x) and len(syms) < 300:
        syms.append(sym(pay))
        pay += N
    return sync, syms


def decode_payload(syms):
    vals, i, nibs, first = [], 0, [], True
    while i < len(syms):
        sf_app = SF - 2 if first else SF
        cr_app = 4 if first else CR
        cw_len = 4 + cr_app
        blk = syms[i:i + cw_len]
        if len(blk) < cw_len:
            break
        i += cw_len
        inter = []
        for v in blk:
            y = gray((v - 1) % N)
            inter.append(bits_msb(y >> 2 if first else y, sf_app))
        cw = [[0] * cw_len for _ in range(sf_app)]
        for a in range(cw_len):
            for j in range(sf_app):
                cw[(a - j - 1) % sf_app][a] = inter[a][j]
        nibs += [hamming_data(from_bits(c), cr_app) for c in cw]
        if first:
            length = (nibs[0] << 4) | nibs[1]
            hdr_ok = header_nibbles(length, nibs[2] >> 1, nibs[2] & 1) == nibs[:5]
            need = 5 + 2 * length + (4 if nibs[2] & 1 else 0)
        first = False
        if len(nibs) >= need:
            break
    pl = nibs[5:5 + 2 * length]
    ws = whitening_seq(length)
    data = bytes(((pl[2 * k] | (pl[2 * k + 1] << 4)) ^ ws[k]) for k in range(length))
    cn = nibs[5 + 2 * length:5 + 2 * length + 4]
    crc_rx = cn[0] | cn[1] << 4 | cn[2] << 8 | cn[3] << 12 if len(cn) == 4 else None
    return length, hdr_ok, data, crc_rx == payload_crc(data)


def find_packets(x):
    pw = [sum(abs(z) ** 2 for z in x[k:k + N]) / N for k in range(0, len(x) - N, N)]
    floor = sorted(pw)[len(pw) // 2]
    starts, k = [], 0
    while k < len(pw):
        if pw[k] > 10 * floor and all(pw[j] > 10 * floor for j in range(k, min(k + 8, len(pw)))):
            starts.append((k + 1) * N)
            k += 70
        else:
            k += 1
    return starts


# --------------------------------------------------------------------------------------------- cli
if __name__ == "__main__":
    if sys.argv[1] == "decode":
        fs = float(sys.argv[3]) if len(sys.argv) > 3 else 6.5e6
        x = read_iq(sys.argv[2], fs)
        starts = find_packets(x)
        print(f"{len(starts)} candidate packets")
        for st in starts:
            sync, syms = decode_symbols(x, st)
            if syms is None:
                print(f"  @{st / BW * 1e3:7.1f} ms: no preamble lock"); continue
            try:
                length, hdr_ok, data, crc_ok = decode_payload(syms)
                print(f"  @{st / BW * 1e3:7.1f} ms: sync {sync}, len {length}, header {'ok' if hdr_ok else 'BAD'}, "
                      f"crc {'ok' if crc_ok else 'BAD'}: {data!r}")
            except Exception as e:                      # noqa: BLE001
                print(f"  @{st / BW * 1e3:7.1f} ms: sync {sync}, decode failed ({e}); first symbols {syms[:12]}")
    elif sys.argv[1] == "encode":
        # The packets are shifted up by OFFSET in the file, so the HackRF is tuned OFFSET below the channel:
        # this keeps the HackRF's own carrier leak (at its tuned frequency) out of the LoRa channel. Without
        # it the SX1281 detected preambles but never a header (bench test 16-11-2026).
        out, text = sys.argv[2], sys.argv[3]
        count = int(sys.argv[4]) if len(sys.argv) > 4 else 10
        gap = float(sys.argv[5]) if len(sys.argv) > 5 else 150
        fs, offset = 8e6, 2e6
        buf, ph = bytearray(2 * int(fs * 0.05)), 0.0
        for n in range(count):
            pl = (text + f" #{n:06d}").encode()[:19] + b"\0"
            for z in modulate(encode_symbols(pl), fs):
                z = z.conjugate() * cmath.exp(1j * ph)        # conjugated: see read_iq
                ph = (ph + 2 * math.pi * offset / fs) % (2 * math.pi)
                buf += bytes([int(round(z.real * 90)) & 0xFF, int(round(z.imag * 90)) & 0xFF])
            pad = int(fs * gap / 1000)
            buf += bytes(2 * pad)
            ph = (ph + 2 * math.pi * offset / fs * pad) % (2 * math.pi)
        open(out, "wb").write(buf)
        print(f"wrote {out}: {count} packets, {gap} ms gaps, {len(buf) / 2 / fs:.2f} s at 8 MS/s")
        print(f"send with: hackrf_transfer -t {out} -f 2438000000 -s 8000000 -a 0 -x 30")
