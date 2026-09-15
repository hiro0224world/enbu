"""
VRM に埋まっているサムネイル画像を、1x1 の透明 PNG に置き換えて軽くする。

サムネイルは VRM を一覧表示するソフトが使うもので、ゲーム中は一度も表示されない。
画像の要素ごと削除すると images / textures の番号がずれて他の参照が壊れるため、
番号はそのままにして中身だけを最小の画像に差し替える。

**2026-09-15 修正**: 以前は 1x1 PNG のバイト列を手で書いていて、IDAT の CRC が誤り、
終端（IEND）も欠けていた。ブラウザは見逃すので炎舞は動いていたが、Blender（libpng）が
「IDAT: CRC error」で読めなかった。いまは PNG を zlib で組み立て、
**書き出す前に全 PNG の CRC と終端を検査し、1 枚でも壊れていたら書かない**。

使い方:
    python scripts/strip_vrm_thumbnail.py public/models/*.vrm
"""

import json
import os
import struct
import sys
import zlib

PNG_SIG = b'\x89PNG\r\n\x1a\n'


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


def tiny_png() -> bytes:
    """1x1 の透明 PNG を正しい CRC で組み立てる（手書きのバイト列は使わない）"""
    ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)  # 幅1 高さ1 8bit RGBA
    raw = b'\x00' + b'\x00\x00\x00\x00'                  # フィルタ0 + 透明の1画素
    return PNG_SIG + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', zlib.compress(raw)) + _chunk(b'IEND', b'')


def check_png(b: bytes) -> str:
    """PNG の署名・全チャンクの CRC・終端を検査する。問題がなければ 'ok'"""
    if len(b) < 8 or b[:8] != PNG_SIG:
        return 'not png'
    i, problems = 8, []
    while True:
        if i + 12 > len(b):
            problems.append('truncated (no IEND)')
            break
        ln = struct.unpack('>I', b[i:i + 4])[0]
        kind = b[i + 4:i + 8]
        if i + 12 + ln > len(b):
            problems.append(f'chunk {kind!r} overruns end')
            break
        crc = struct.unpack('>I', b[i + 8 + ln:i + 12 + ln])[0]
        if zlib.crc32(kind + b[i + 8:i + 8 + ln]) & 0xffffffff != crc:
            problems.append(f'CRC NG in {kind.decode(errors="replace")}')
        i += 12 + ln
        if kind == b'IEND':
            break
    return 'ok' if not problems else '; '.join(problems)


def read_glb(path):
    with open(path, 'rb') as f:
        data = f.read()
    magic, _version, length = struct.unpack_from('<III', data, 0)
    assert magic == 0x46546C67, 'glb ではない'
    off, js, bin_ = 12, None, b''
    while off < length:
        clen, ctype = struct.unpack_from('<II', data, off)
        chunk = data[off + 8: off + 8 + clen]
        if ctype == 0x4E4F534A:
            js = json.loads(chunk.decode('utf-8'))
        elif ctype == 0x004E4942:
            bin_ = chunk
        off += 8 + clen + ((4 - clen % 4) % 4)
    return js, bin_


def build_glb(js, bin_) -> bytes:
    jb = json.dumps(js, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    jb += b' ' * ((4 - len(jb) % 4) % 4)
    bb = bin_ + b'\x00' * ((4 - len(bin_) % 4) % 4)
    total = 12 + 8 + len(jb) + (8 + len(bb) if bb else 0)
    out = struct.pack('<III', 0x46546C67, 2, total) + struct.pack('<II', len(jb), 0x4E4F534A) + jb
    if bb:
        out += struct.pack('<II', len(bb), 0x004E4942) + bb
    return out


def verify_all_pngs(glb: bytes, label: str):
    """書き出す直前のゲート。1 枚でも壊れていたら例外にしてファイルを作らせない"""
    clen = struct.unpack_from('<I', glb, 12)[0]
    js = json.loads(glb[20:20 + clen])
    off = 20 + clen + ((4 - clen % 4) % 4)
    bin_ = glb[off + 8: off + 8 + struct.unpack_from('<I', glb, off)[0]]
    bad = []
    for idx, im in enumerate(js.get('images', [])):
        if im.get('mimeType') != 'image/png' or 'bufferView' not in im:
            continue
        v = js['bufferViews'][im['bufferView']]
        r = check_png(bin_[v.get('byteOffset', 0): v.get('byteOffset', 0) + v['byteLength']])
        if r != 'ok':
            bad.append(f'#{idx} {r}')
    if bad:
        raise RuntimeError(f'{label}: 壊れた PNG があるので書き出さない -> {bad}')


def strip(path):
    js, bin_ = read_glb(path)
    before = os.path.getsize(path)

    meta = js.get('extensions', {}).get('VRMC_vrm', {}).get('meta', {})
    idx = meta.get('thumbnailImage')
    if idx is None:
        print(f'{os.path.basename(path)}: サムネイルなし')
        return
    img = js['images'][idx]
    bv_index = img.get('bufferView')
    if bv_index is None:
        print(f'{os.path.basename(path)}: サムネイルが外部参照なので触らない')
        return

    tiny = tiny_png()
    assert check_png(tiny) == 'ok', '組み立てた 1x1 PNG が壊れている'

    views = js['bufferViews']
    old_len = views[bv_index]['byteLength']

    # bufferView を順に詰め直す。中身はそのまま写し、サムネイルだけ差し替える
    out = bytearray()
    for i, v in enumerate(views):
        src = tiny if i == bv_index else bin_[v.get('byteOffset', 0): v.get('byteOffset', 0) + v['byteLength']]
        out.extend(b'\x00' * ((4 - len(out) % 4) % 4))
        v['byteOffset'] = len(out)
        v['byteLength'] = len(src)
        out.extend(src)
    js['buffers'][0]['byteLength'] = len(out)

    glb = build_glb(js, bytes(out))
    verify_all_pngs(glb, os.path.basename(path))  # 通らなければここで止まり、元ファイルは残る
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(glb)
    os.replace(tmp, path)
    after = os.path.getsize(path)
    print(f'{os.path.basename(path)}: サムネイル {old_len / 1048576:.2f}MB を 1x1 PNG に置換 '
          f'→ {before / 1048576:.2f}MB から {after / 1048576:.2f}MB（全 PNG 検査 OK）')


if __name__ == '__main__':
    for p in sys.argv[1:]:
        strip(p)
