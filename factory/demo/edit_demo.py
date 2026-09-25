"""Join the demo recordings into one MP4 and fast-forward the waiting.

    python edit_demo.py out.mp4 part1.webm [part2.webm ...]

Each part may have a <part>.waits.json next to it (written by
record_demo_in_oci.py): [[start, end], ...] seconds spent waiting on the
assistant. Those stretches play at SPEED x; everything else at normal speed.
Needs ffmpeg (imageio-ffmpeg's bundled binary is used when ffmpeg is not on
PATH).
"""
import json
import os
import shutil
import subprocess
import sys

SPEED = 8.0


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def duration(ff: str, path: str) -> float:
    out = subprocess.run([ff, "-i", path], capture_output=True, text=True).stderr
    h, m, s = out.split("Duration: ")[1].split(",")[0].split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def segments(total: float, waits: list) -> list:
    """[(start, end, fast?)] covering 0..total, waits merged and clipped."""
    ws = sorted([max(0.0, a), min(total, b)] for a, b in waits if b - a > 1.0)
    segs, t = [], 0.0
    for a, b in ws:
        if a < t:
            a = t
        if b <= a:
            continue
        if a > t:
            segs.append((t, a, False))
        segs.append((a, b, True))
        t = b
    if t < total:
        segs.append((t, total, False))
    return segs


def main(out: str, parts: list) -> None:
    ff = ffmpeg()
    inputs, chains, labels, n = [], [], [], 0
    for i, part in enumerate(parts):
        inputs += ["-i", part]
        waits = []
        if os.path.exists(part + ".waits.json"):
            waits = json.load(open(part + ".waits.json", encoding="utf-8"))
        total = duration(ff, part)
        for a, b, fast in segments(total, waits):
            lab = f"s{n}"
            pts = f"(PTS-STARTPTS)/{SPEED}" if fast else "PTS-STARTPTS"
            chains.append(f"[{i}:v]trim=start={a:.2f}:end={b:.2f},setpts={pts},fps=25,scale=1366:860,setsar=1[{lab}]")
            labels.append(f"[{lab}]")
            n += 1
        fast_s = sum(b - a for a, b, f in segments(total, waits) if f)
        print(f"{os.path.basename(part)}: {total:.0f}s, {fast_s:.0f}s of waiting at {SPEED:g}x", flush=True)
    graph = ";".join(chains) + ";" + "".join(labels) + f"concat=n={n}:v=1:a=0[v]"
    subprocess.run([ff, "-loglevel", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", out], check=True)
    print(f"{out}: {duration(ff, out):.0f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
