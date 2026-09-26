"""Where the moments of a take are, to anchor the narration.

    python anchors.py part1.webm part2.webm

Part 1: each prompt's send and reply times (from the .waits.json the recorder
writes). Part 2: every change of page (the boundaries of the still stretches
freezedetect finds), with the cut's time for each, so SCENES in
narrate_short.py can be set without scrubbing through the video.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from edit_demo import duration, ffmpeg, still  # noqa: E402
from narrate import mapper  # noqa: E402
from narrate_short import short_segments  # noqa: E402


def main(p1, p2):
    ff = ffmpeg()
    s1 = short_segments(ff, p1, False)
    len1 = sum((b - a) / sp for a, b, sp in s1)
    to = {1: mapper(s1, 0.0), 2: mapper(short_segments(ff, p2, True), len1)}
    waits = json.load(open(p1 + ".waits.json", encoding="utf-8")) if os.path.exists(p1 + ".waits.json") else []
    print(f"part 1: {duration(ff, p1):.0f}s raw -> {len1:.0f}s cut")
    for k, (a, b) in enumerate(waits):
        print(f"  prompt {k + 1}: sent {a - 1.0:6.1f}s  reply {b + 1.5:6.1f}s   (cut {to[1](a - 1.0):5.1f}s / {to[1](b + 1.5):5.1f}s)")
    print(f"part 2: {duration(ff, p2):.0f}s raw, cut starts at {len1:.0f}s")
    prev = 0.0
    for a, b in still(ff, p2):
        if a - prev > 0.5:
            print(f"  moves {prev:6.1f}s -> {a:6.1f}s   still until {min(b, duration(ff, p2)):6.1f}s   (cut {to[2](a):5.1f}s)")
        prev = b


if __name__ == "__main__":
    main(*sys.argv[1:3])
