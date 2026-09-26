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

# Anchors are seconds into the raw recordings (part, time); a fourth value is
# where in the cut to hold the frame when the line runs long. Set them with
# anchors.py after each take.
SCENES = [
    (1, 0.2, "Meet Sandbox Factory. You chat, and it builds, right inside Oracle Cloud."),
    (1, 21.3, "Ask for Grafana from its public image. It plans the container, prices it from Oracle's price list, and asks only for what the code needs: here, the admin password, typed in a box that never reaches the AI."),
    (1, 33.0, "One click, and it is building."),
    (1, 97.1, "Now a migration: Airflow writing to S3, and Glue building Iceberg tables. It maps each piece. S3 becomes Object Storage, Glue jobs become Spark on Data Flow, the Glue catalog becomes Data Catalog, and Airflow keeps running your DAGs unchanged.", 19.0),
    (1, 106.0, "Hand it the repository. It reads the DAG and the Spark job, and builds the pipeline: a bucket for raw and gold data, a Data Flow application for the Spark job, a Data Catalog, Airflow with the DAG loaded, and an Autonomous Database for the gold tables.", 28.2),
    (2, 1.0, "Minutes later, everything is live, with links, credentials, logs, and an expiry date."),
    (2, 39.7, "Grafana, signed in with the password you gave, ready for your team."),
    (2, 68.3, "Airflow is up, with the telemetry DAG loaded."),
    (2, 72.6, "The run is green, in four steps. It lands raw sensor readings in Object Storage, runs the Spark gold job on Data Flow, registers the bucket in Data Catalog, and loads the gold tables into Oracle.", 69.0),
    (2, 98.0, "Daily telemetry and device sessions, queryable in the database, and served straight away as REST."),
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
    "Schedule the DAG every fifteen minutes, and the same pipeline runs in micro batches.",
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
    for k, (part, raw_t, text, *hold_at) in enumerate(SCENES):
        path = os.path.join(work, f"scene{k:02d}.mp3")
        scenes.append((to_out[part](raw_t), text, path, say(text, path, voice), hold_at[0] if hold_at else None))
    fillers = []
    for k, text in enumerate(FILLERS):
        path = os.path.join(work, f"filler{k:02d}.mp3")
        fillers.append((text, path, say(text, path, voice)))
    # Place each scene line at its moment in the cut. When the previous line is
    # still speaking, HOLD the video on that moment (freeze the frame) until the
    # voice is free, so an explanation is never cut short and never drifts.
    # Gaps before a scene get filler lines about the factory.
    # A scene may name its own hold point (seconds in the cut, where its
    # explanation is on screen); a line that runs long freezes there instead.
    placed, free, shift, holds, speaking_hold = [], 0.0, 0.0, [], None
    for at, text, path, d, hold_at in scenes:
        t = at + shift
        while True:
            room = t - free - GAP
            fit = [f for f in fillers if f[2] + GAP <= room]
            if not fit:
                break
            f = max(fit, key=lambda x: x[2])
            fillers.remove(f)
            placed.append((free + GAP, f[0], f[1], f[2]))
            free += GAP + f[2]
        need = free + GAP - t
        if need > 0.05:
            where = speaking_hold if speaking_hold is not None and speaking_hold < at else at
            holds.append((where, need))     # freeze the source video there for `need` s
            shift += need
            t += need
        placed.append((t, text, path, d))
        free = t + d
        speaking_hold = hold_at
    for s, text, _, d in placed:
        print(f"{s:6.1f}s +{d:4.1f}s  {text}", flush=True)
    total = video_len + shift
    hold = max(0.0, free + 0.8 - total)
    # the video: cut at every hold point, freeze the last frame of each piece
    merged = {}
    for at, need in holds:
        merged[round(at, 3)] = merged.get(round(at, 3), 0.0) + need
    cuts = sorted(merged.items())
    pieces, prev = [], 0.0
    for k, (at, need) in enumerate(cuts):
        pieces.append((prev, at, need))
        prev = at
    pieces.append((prev, video_len, hold))
    vchain = []
    for k, (a0_, b0_, pad) in enumerate(pieces):
        tp = f",tpad=stop_mode=clone:stop_duration={pad:.2f}" if pad > 0 else ""
        vchain.append(f"[0:v]trim=start={a0_:.3f}:end={b0_:.3f},setpts=PTS-STARTPTS,fps=25{tp}[p{k}]")
    vid = ";".join(vchain) + ";" + "".join(f"[p{k}]" for k in range(len(pieces))) + f"concat=n={len(pieces)}:v=1:a=0[v]"
    inputs = ["-i", cut]
    for _, _, path, _ in placed:
        inputs += ["-i", path]
    chains = [f"[{i + 1}:a]adelay={int(s_ * 1000)}|{int(s_ * 1000)},aresample=48000[a{i}]" for i, (s_, _, _, _) in enumerate(placed)]
    mix = "".join(f"[a{i}]" for i in range(len(placed))) + f"amix=inputs={len(placed)}:normalize=0,volume=1.6[aud]"
    subprocess.run([ff, "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(chains + [mix, vid]),
                    "-map", "[v]", "-map", "[aud]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out], check=True)
    for at, need in cuts:
        print(f"  held the frame at {at:6.1f}s of the cut for {need:4.1f}s so the explanation fits", flush=True)
    speech = sum(d for _, _, _, d in placed)
    print(f"{out}: {duration(ff, out):.0f}s, voice {speech:.0f}s of it (held {hold:.1f}s at the end)", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:5], *(sys.argv[5:6] or []))
