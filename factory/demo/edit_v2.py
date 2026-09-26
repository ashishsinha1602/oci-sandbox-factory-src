"""Second cut of the demo, from the author's notes.

    python edit_v2.py out.mp4 part1.webm part2.webm LANDING_START LANDING_END

- part 1 (the conversation with the agent) plays at normal speed. Only the
  wait while the assistant thinks (part1.webm.waits.json) plays fast, and the
  seconds right after each answer arrives play at half speed, so every answer
  can be read.
- part 2: the card / landing-page section (LANDING_START..LANDING_END seconds)
  plays at 4x; every other still stretch after it keeps 1 s and then plays
  fast; the final 4 s (the MCP rows) hold.
Prints the edited timeline (scene start times in the output) for narration.
"""
import json
import sys

from edit_demo import KEEP, duration, ffmpeg, still

import subprocess

THINK = 12.0      # spinner speed-up
READ = 4.0        # seconds after each answer, played at half speed
LANDING = 4.0


def part1_segments(total, waits):
    segs, t = [], 0.0
    for a, b in sorted(waits):
        a, b = max(a, t), min(b, total)
        if b - a < 0.5:
            continue
        if a > t:
            segs.append((t, a, 1.0))
        segs.append((a, b, THINK))
        r = min(b + READ, total)
        segs.append((b, r, 0.5))
        t = r
    if t < total:
        segs.append((t, total, 1.0))
    return segs


def part2_segments(total, stills, l0, l1):
    segs = [(0.0, l0, 1.0), (l0, l1, LANDING)]
    t = l1
    for a, b in sorted(stills):
        a, b = max(a + KEEP, t), min(b, total - 4.0)
        if b - a < 0.4:
            continue
        if a > t:
            segs.append((t, a, 1.0))
        segs.append((a, b, 12.0))
        t = b
    if t < total:
        segs.append((t, total, 1.0))
    return segs


def main(out, p1, p2, l0, l1):
    ff = ffmpeg()
    waits = json.load(open(p1 + ".waits.json", encoding="utf-8"))
    parts = [(p1, part1_segments(duration(ff, p1), waits)),
             (p2, part2_segments(duration(ff, p2), still(ff, p2), l0, l1))]
    chains, labels, n, clock, timeline = [], [], 0, 0.0, []
    for i, (path, segs) in enumerate(parts):
        timeline.append((round(clock, 1), f"part {i + 1} starts"))
        for a, b, speed in segs:
            chains.append(f"[{i}:v]trim=start={a:.2f}:end={b:.2f},setpts=(PTS-STARTPTS)/{speed},fps=25,scale=1366:860,setsar=1[s{n}]")
            labels.append(f"[s{n}]")
            n += 1
            clock += (b - a) / speed
        timeline.append((round(clock, 1), f"part {i + 1} ends"))
    graph = ";".join(chains) + ";" + "".join(labels) + f"concat=n={n}:v=1:a=0[v]"
    subprocess.run([ff, "-loglevel", "error", "-y", "-i", p1, "-i", p2, "-filter_complex", graph, "-map", "[v]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", out], check=True)
    print(f"{out}: {duration(ff, out):.0f}s", flush=True)
    for t, what in timeline:
        print(f"  {t:6.1f}s  {what}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]))
