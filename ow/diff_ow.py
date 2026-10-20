#!/usr/bin/env python3
"""After a play session: what did Overwatch append to the installed warm-all cache?

Compares the live namespace with the newest reports/ow_postinstall_snapshot_*.json.  Goal: ~0 new
VS/PS stage records (every shader the game drew was already compiled).  Also flags a wiped cache
(fewer records than installed) and records that went to a *_1 GPU-family dir the game never reads.

  diff_ow.py [--snapshot FILE]
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from install_ow import LAB, LIVE, LIVE_PARENT, FILES, snapshot  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path)
    a = ap.parse_args()
    snap_path = a.snapshot or max((LAB / "reports").glob("ow_postinstall_snapshot_*.json"))
    before = json.loads(snap_path.read_text())
    now = snapshot(LIVE)
    report = {"snapshot": str(snap_path), "installed_at": before["time"], "checked_at": now["time"],
              "version_bin_changed": before.get("version_bin_sha256") != now.get("version_bin_sha256"),
              "other_family_dirs": [d.name for d in LIVE_PARENT.iterdir() if d.is_dir() and d.name != LIVE.name]}
    for f in FILES[:-1]:
        b, n = before.get(f, {}), now.get(f, {})
        report[f] = {"installed": b.get("records"), "now": n.get("records"),
                     "appended": (n.get("records") or 0) - (b.get("records") or 0), "consistent": n.get("consistent")}
        if f == "stage_cache.bin":
            kb, kn = b.get("kinds", {}), n.get("kinds", {})
            report[f]["appended_by_kind"] = {k: kn.get(k, 0) - kb.get(k, 0) for k in sorted(set(kb) | set(kn))
                                             if kn.get(k, 0) != kb.get(k, 0)}
    wiped = any((report[f]["now"] or 0) < (report[f]["installed"] or 0) for f in FILES[:-1])
    report["verdict"] = ("CACHE_WIPED_OR_REPLACED" if wiped or report["version_bin_changed"]
                         else "WARM_HIT" if report["stage_cache.bin"]["appended_by_kind"].get("PS", 0)
                         + report["stage_cache.bin"]["appended_by_kind"].get("VS", 0) <= 5 else "PARTIAL")
    out = LAB / "reports" / f"ow_postplay_diff_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
