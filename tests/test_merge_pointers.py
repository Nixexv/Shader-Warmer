"""Pipeline records point at stage records by serial; a merge must carry those pointers along."""
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ow"))
import merge_ns  # noqa: E402
from xxh3lib import xxh3_64  # noqa: E402


def record(payload: bytes) -> bytes:
    framed = (len(payload) + 32 + 7) & ~7
    body = struct.pack("<QQQ", framed, len(payload), xxh3_64(payload)) + payload
    return body + b"\0" * (framed - 8 - len(body)) + struct.pack("<q", -framed)


def stage(st: int, serial: int, shader_id: bytes) -> bytes:
    key = shader_id + b"\x11" * 16
    return record(struct.pack("<4I", st, serial, 0, len(key)) + b"\0" * (0x28 - 16) + key)


def pipeline(vs: int, ps: int, vs_id: bytes, ps_id: bytes) -> bytes:
    header = [0] * 16
    header[0], header[1], header[7], header[8], header[14] = 0xAB, 0xCD, vs, ps, 57
    return record(struct.pack("<16I", *header) + vs_id + ps_id + b"state")


def namespace(where: Path, stages, pipelines) -> Path:
    where.mkdir(parents=True)
    for name, recs in (("stage_cache.bin", stages), ("pipeline_cache.bin", pipelines),
                       ("bytecode_cache.bin", []), ("rootsignature_cache.bin", [])):
        w = merge_ns.ArenaWriter(where / name)
        for r in recs:
            w.add(r)
        w.close()
    (where / "version.bin").write_bytes(b"same")
    return where


def sid(n: int) -> bytes:
    return bytes([n]) * 16


class PointerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_second_input_keeps_its_shaders(self):
        """The black-screen bug: the second input's pipelines ended up on the first input's shaders."""
        a = namespace(self.tmp / "a", [stage(1, 1, sid(1)), stage(2, 1, sid(2))],
                      [pipeline(1, 1, sid(1), sid(2))])
        b = namespace(self.tmp / "b", [stage(1, 1, sid(3)), stage(2, 1, sid(4))],
                      [pipeline(1, 1, sid(3), sid(4))])
        rep = merge_ns.merge([a, b], self.tmp / "out", with_bytecode=False)
        self.assertEqual(rep["pipeline_cache.bin"]["records"], 2)
        self.assertEqual(merge_ns.damaged_pipelines(self.tmp / "out"), 0)

    def test_duplicate_stage_points_at_the_survivor(self):
        a = namespace(self.tmp / "a", [stage(1, 1, sid(1)), stage(2, 1, sid(2))], [])
        b = namespace(self.tmp / "b", [stage(1, 1, sid(5)), stage(2, 1, sid(2))],   # PS is a duplicate
                      [pipeline(1, 1, sid(5), sid(2))])
        merge_ns.merge([a, b], self.tmp / "out", with_bytecode=False)
        self.assertEqual(merge_ns.damaged_pipelines(self.tmp / "out"), 0)

    def test_already_damaged_pipelines_are_dropped_and_repair_fixes_in_place(self):
        """What 1.21-1.35 left behind: a pipeline whose PS pointer names another shader."""
        bad = namespace(self.tmp / "bad", [stage(1, 1, sid(1)), stage(2, 1, sid(2)), stage(2, 2, sid(3))],
                        [pipeline(1, 1, sid(1), sid(2)), pipeline(1, 1, sid(1), sid(3))])
        self.assertEqual(merge_ns.damaged_pipelines(bad), 1)
        # merging a damaged cache takes none of its pipelines, not just the mismatched one
        rep = merge_ns.merge([bad], self.tmp / "out", with_bytecode=False)
        self.assertEqual(rep["pipeline_cache.bin"]["records"], 0)
        self.assertEqual(rep["pipeline_cache.bin"]["damaged_input_dropped"], 2)
        # repair drops every pipeline (a matching ShaderID can still be the wrong variant) and keeps the stages
        stages = sum(1 for _ in merge_ns.records(bad / "stage_cache.bin"))
        self.assertEqual(merge_ns.repair(bad, self.tmp / "work"), 2)
        self.assertEqual(sum(1 for _ in merge_ns.records(bad / "pipeline_cache.bin")), 0)
        self.assertEqual(sum(1 for _ in merge_ns.records(bad / "stage_cache.bin")), stages)
        self.assertEqual(merge_ns.repair(bad, self.tmp / "work"), 0)       # nothing left to fix


if __name__ == "__main__":
    unittest.main()


class RepairOnceTests(unittest.TestCase):
    def test_a_cache_only_half_repaired_by_136_loses_all_its_pipelines(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        fam = namespace(tmp / "fam", [stage(1, 1, sid(1)), stage(2, 1, sid(2))], [pipeline(1, 1, sid(1), sid(2))])
        home = tmp / "home"
        state = home / "Library/Application Support/ShaderWarmer/pipeline_checks.json"
        state.parent.mkdir(parents=True)
        pc = fam / "pipeline_cache.bin"
        import json
        state.write_text(json.dumps({str(fam): [pc.stat().st_size, int(pc.stat().st_mtime)]}))   # 1.36's format
        real_home = Path.home
        Path.home = staticmethod(lambda: home)
        try:
            self.assertEqual(merge_ns.repair_once(fam, tmp / "work"), 1)     # no visible damage, still dropped
            self.assertEqual(merge_ns.repair_once(fam, tmp / "work"), 0)     # and only once
        finally:
            Path.home = real_home
        self.assertEqual(sum(1 for _ in merge_ns.records(fam / "stage_cache.bin")), 2)
