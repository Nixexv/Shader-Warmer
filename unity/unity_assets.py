#!/usr/bin/env python3
"""Minimal reader for Unity serialized files (.assets / levelN), enough to locate objects by class.

Release builds strip the type trees, so the field layout of each object is unknown — but the header,
the type table and the object table are fixed structures, so we can still say "object of class 48
(Shader) lives at bytes [start, start+size)".  That is all the shader carver needs.

  unity_assets.py FILE [FILE ...]      # class histogram per file
"""
from __future__ import annotations
import struct, sys
from pathlib import Path

SHADER = 48
COMPUTE_SHADER = 72


class Reader:
    def __init__(self, data: bytes, little=True):
        self.d, self.p, self.little = data, 0, little

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def _n(self, fmt, size):
        v = struct.unpack_from(("<" if self.little else ">") + fmt, self.d, self.p)[0]
        self.p += size
        return v

    def i16(self): return self._n("h", 2)
    def u32(self): return self._n("I", 4)
    def i32(self): return self._n("i", 4)
    def i64(self): return self._n("q", 8)
    def u64(self): return self._n("Q", 8)

    def cstr(self):
        e = self.d.find(b"\0", self.p)                 # find, not index: the data may be an mmap
        if e < 0:
            raise ValueError("unterminated string")
        s = self.d[self.p:e].decode("utf-8", "replace")
        self.p = e + 1
        return s

    def align(self, n=4):
        self.p = (self.p + n - 1) & ~(n - 1)


def skip_type_tree(r: "Reader", version: int):
    """Blob-format type tree: a node array plus a string buffer, both length-prefixed.
    The node grew a reference-type hash in format 19, so its size depends on the version."""
    node_count = r.u32()
    string_size = r.u32()
    r.p += node_count * (32 if version >= 19 else 24)
    r.p += string_size


def parse(path: Path):
    return parse_bytes(Path(path).read_bytes())


def parse_bytes(data: bytes):
    """-> {"version":int, "unity":str, "platform":int, "objects":[(path_id, start, size, class_id)]}

    Handles every serialized-file format from 9 (Unity 5) to 22+ (Unity 2020.2 and newer).  The
    older ones are not exotic: the header keeps 32-bit offsets, the object entry carries its own
    class id, and the type entry gained its fields one version at a time.
    """
    r = Reader(data, little=False)               # the header is big-endian
    meta_size, file_size, version, data_offset = r.u32(), r.u32(), r.u32(), r.u32()
    if not 9 <= version <= 40:
        raise ValueError(f"serialized file version {version} not supported")
    endian = r.u8()
    r.p += 3                                      # reserved
    if version >= 22:                             # wide header: 64-bit sizes and offsets
        meta_size = r.u32()
        file_size = r.i64()
        data_offset = r.i64()
        r.i64()                                   # unknown
    r.little = endian == 0
    unity = r.cstr()
    platform = r.u32()
    has_type_tree = bool(r.u8()) if version >= 13 else False

    type_count = r.u32()
    types = []
    for _ in range(type_count):
        class_id = r.i32()
        if version >= 16:
            r.u8()                                # is stripped
        if version >= 17:
            script_index = r.i16()
        if version >= 13:
            if (version < 16 and class_id < 0) or (version >= 16 and class_id == 114):
                r.p += 16                         # MonoBehaviour carries a script hash
            r.p += 16                             # old type hash
        if has_type_tree:
            skip_type_tree(r, version)            # present in some files; we never need the fields
            if version >= 21:
                r.p += 4 * r.u32()                # type dependencies
        types.append(class_id)

    big_id = r.i32() if 7 <= version < 14 else 0
    obj_count = r.u32()
    objects = []
    for _ in range(obj_count):
        if big_id:
            path_id = r.i64()
        elif version < 14:
            path_id = r.i32()
        else:
            r.align(4)
            path_id = r.i64()
        start = r.i64() if version >= 22 else r.u32()
        size = r.u32()
        type_id = r.i32()
        if version < 16:
            class_id = r.i16()                    # the entry names its own class
            r.i16()                               # script type index
            if version <= 10:
                r.u8()                            # is destroyed
        else:
            class_id = types[type_id] if 0 <= type_id < len(types) else -1
        if version in (15, 16):
            r.u8()                                # stripped
        objects.append((path_id, data_offset + start, size, class_id))

    # A misread table produces offsets that point nowhere; better to fail than to carve noise.
    for _, obj_start, obj_size, _ in objects:
        if obj_start < data_offset or obj_start + obj_size > len(data):
            raise ValueError(f"object table out of range (format {version})")
    return {"version": version, "unity": unity, "platform": platform, "objects": objects,
            "data_offset": data_offset, "file_size": file_size, "data": data}


def main():
    import collections
    for f in sys.argv[1:]:
        p = Path(f)
        try:
            info = parse(p)
        except Exception as e:
            print(f"{p.name}: {e}")
            continue
        hist = collections.Counter(c for _, _, _, c in info["objects"])
        shaders = [o for o in info["objects"] if o[3] == SHADER]
        print(f"{p.name}: unity {info['unity']} platform {info['platform']} objects {len(info['objects'])} "
              f"shaders {len(shaders)} top classes {hist.most_common(6)}")


if __name__ == "__main__":
    main()
