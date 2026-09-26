"""Continuous narration for the short cut (edit_demo.py output).

    python narrate_short.py out.mp4 short.mp4 part1.webm part2.webm [voice]

Scene lines are pinned to moments in the raw recordings and mapped through the
same segments edit_demo.py cut the video with; filler lines about the factory
fill every gap long enough to hold one, so the voice never goes quiet. OCI
Speech TTS_2_NATURAL, played 8% faster to keep pace with the cut.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from edit_demo import duration, ffmpeg, segments, still  # noqa: E402
from narrate import mapper, tts  # noqa: E402

TEMPO = 1.08
GAP = 0.25

SCENES = [
    (1, 0.2, "Meet Sandbox Factory. You chat, and it builds, right inside Oracle Cloud."),
    (1, 16.0, "Ask for a database with MCP and a chat UI. It plans it, prices it, and builds it in one click."),
    (1, 39.0, "Point it at a GitHub folder. It reads the code, and deploys the app."),
    (1, 62.0, "Migrating Airflow and Glue? It maps every piece to OCI, and compares the options."),
    (1, 89.0, "Hand it the repository, and it builds the whole pipeline."),
    (2, 1.0, "Minutes later, everything is live, with links, credentials, and an expiry date."),
    (2, 42.0, "Airflow, with the DAG loaded."),
    (2, 60.0, "A green run. Spark on Data Flow, gold tables in Oracle."),
    (2, 66.0, "Served straight away as REST."),
    (2, 84.0, "The app, live on HTTPS."),
    (2, 94.0, "Studio, to ask the data in plain English."),
    (2, 126.0, "And agents connect over MCP. They discover the tools, pick the tables, and run the query. That is Sandbox Factory."),
]
FILLERS = [
    "Everything runs in your own tenancy, under your policies and your budget.",
    "Each sandbox belongs to one person. Nobody else can see it, or destroy it.",
    "Costs come from Oracle's live price list, before anything is built.",
    "Behind the chat, Terraform on Resource Manager builds the same stack every time.",
    "The workers are container instances. Nothing runs on a laptop.",
    "Pick a lifetime from one to thirty days. The reaper cleans up the rest.",
    "Every database comes with Select AI and REST, switched on.",
    "Paid databases stay private. Their tools open through the sandbox gateway.",
    "No tickets, no waiting on an admin. Just ask.",
    "Built for hackathons, proofs of concept, and customer demos.",
]


def say(text, path, voice):
    raw = path + ".raw.mp3"
    tts(text, raw, voice)
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-i", raw, "-filter:a", f"atempo={TEMPO}", path], check=True)
    return duration(ffmpeg(), path)


def short_segments(ff, p, last):
    waits = json.load(open(p + ".waits.json", encoding="utf-8")) if os.path.exists(p + ".waits.json") else []
    total = duration(ff, p)
    segs = segments(total, waits + still(ff, p))
    if last:                                     # edit_demo holds the final 4 s
        fixed = []
        for a, b, fast in segs:
            if fast and b > total - 4.0:
                if a < total - 4.0:
                    fixed.append((a, total - 4.0, True))
                fixed.append((max(a, total - 4.0), b, False))
            else:
                fixed.append((a, b, fast))
        segs = fixed
    from edit_demo import SPEED
    return [(a, b, SPEED if fast else 1.0) for a, b, fast in segs]


def main(out, cut, p1, p2, voice="Brian"):
    ff = ffmpeg()
    work = os.path.join(os.path.dirname(out), "narration_short")
    os.makedirs(work, exist_ok=True)
    s1 = short_segments(ff, p1, False)
    len1 = sum((b - a) / sp for a, b, sp in s1)
    s2 = short_segments(ff, p2, True)
    to_out = {1: mapper(s1, 0.0), 2: mapper(s2, len1)}
    video_len = duration(ff, cut)
    scenes = []
    for k, (part, raw_t, text) in enumerate(SCENES):
        path = os.path.join(work, f"scene{k:02d}.mp3")
        scenes.append((to_out[part](raw_t), text, path, say(text, path, voice)))
    fillers = []
    for k, text in enumerate(FILLERS):
        path = os.path.join(work, f"filler{k:02d}.mp3")
        fillers.append((text, path, say(text, path, voice)))
    placed, free = [], 0.0
    for at, text, path, d in scenes:
        # fill the gap before this scene line with whatever filler fits
        while True:
            room = at - free - GAP
            fit = [f for f in fillers if f[2] + GAP <= room]
            if not fit:
                break
            f = max(fit, key=lambda x: x[2])      # the longest that fits
            fillers.remove(f)
            placed.append((free + GAP, f[0], f[1], f[2]))
            free += GAP + f[2]
        start = max(at, free + GAP)
        placed.append((start, text, path, d))
        free = start + d
    for s, text, _, d in placed:
        print(f"{s:6.1f}s +{d:4.1f}s  {text}", flush=True)
    hold = max(0.0, free + 0.8 - video_len)
    inputs = ["-i", cut]
    for _, _, path, _ in placed:
        inputs += ["-i", path]
    chains = [f"[{i + 1}:a]adelay={int(s * 1000)}|{int(s * 1000)},aresample=48000[a{i}]" for i, (s, _, _, _) in enumerate(placed)]
    mix = "".join(f"[a{i}]" for i in range(len(placed))) + f"amix=inputs={len(placed)}:normalize=0,volume=1.6[aud]"
    vid = f"[0:v]tpad=stop_mode=clone:stop_duration={hold:.2f}[v]" if hold > 0 else "[0:v]null[v]"
    subprocess.run([ff, "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(chains + [mix, vid]),
                    "-map", "[v]", "-map", "[aud]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out], check=True)
    speech = sum(d for _, _, _, d in placed)
    print(f"{out}: {duration(ff, out):.0f}s, voice {speech:.0f}s of it (held {hold:.1f}s at the end)", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:5], *(sys.argv[5:6] or []))
