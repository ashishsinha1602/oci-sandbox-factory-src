"""Add a spoken narration (OCI Speech, natural voice) to the demo cut.

    python narrate.py out.mp4 cut.mp4 part1.webm part2.webm LANDING_START LANDING_END [voice]

Each line is pinned to a moment in the RAW recordings (seconds into part 1 or
part 2), mapped through the same segments edit_v2 used, synthesized with OCI
Speech TTS_2_NATURAL, and mixed in. A line never starts before the previous one
has finished; if the voice runs past the end, the last frame is held.
"""
import json
import os
import subprocess
import sys

import oci

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sandbox_factory as sf  # noqa: E402

from edit_demo import duration, ffmpeg, still  # noqa: E402
from edit_v2 import part1_segments, part2_segments  # noqa: E402

LINES = [
    (1, 0.5, "This is Sandbox Factory, on Oracle Cloud. You say what you need, and it builds it in your tenancy."),
    (1, 9.0, "A database, an MCP endpoint, and a UI to chat with the data. It plans it, prices it, and one click builds it."),
    (1, 33.0, "Point it at a GitHub folder. It reads the code, and deploys the app with a database."),
    (1, 56.0, "Now a migration: Airflow on S3, and Glue building Iceberg tables. It maps each piece to OCI, compares options and cost, and asks for the code."),
    (1, 84.0, "Give it the repository. It reads the DAG and the Spark job, and builds the whole pipeline."),
    (2, 1.0, "Minutes later, it is all running. Every sandbox has its links, credentials, and a lifetime. It deletes itself when it expires."),
    (2, 42.0, "Airflow is up, with the DAG loaded."),
    (2, 59.0, "The run is green. Spark built the gold tables on Data Flow, and loaded them into Oracle."),
    (2, 66.0, "The gold tables are already REST endpoints."),
    (2, 84.0, "The app from GitHub is live, on an HTTPS address."),
    (2, 94.0, "Studio lets you explore the data in plain English."),
    (2, 126.0, "And any agent can use it over MCP. It finds the tools, picks the tables, and runs the query. That is Sandbox Factory."),
]


def mapper(segs, offset):
    def f(t):
        clock = offset
        for a, b, speed in segs:
            if t <= a:
                return clock
            if t < b:
                return clock + (t - a) / speed
            clock += (b - a) / speed
        return clock
    return f


def tts(text, path, voice):
    m = oci.ai_speech.models
    sp = sf.client(oci.ai_speech.AIServiceSpeechClient)
    d = m.SynthesizeSpeechDetails(
        text=text, is_stream_enabled=False, compartment_id=os.environ.get("SBX_TTS_COMPARTMENT") or sf.config()["tenancy"],
        configuration=m.TtsOracleConfiguration(
            model_family="ORACLE",
            model_details=m.TtsOracleTts2NaturalModelDetails(model_name="TTS_2_NATURAL", voice_id=voice),
            speech_settings=m.TtsOracleSpeechSettings(text_type="TEXT", sample_rate_in_hz=24000, output_format="MP3")))
    r = sp.synthesize_speech(d)
    data = r.data.content if hasattr(r.data, "content") else r.data.raw.read()
    open(path, "wb").write(data)


def main(out, cut, p1, p2, l0, l1, voice="Brian"):
    ff = ffmpeg()
    work = os.path.join(os.path.dirname(out), "narration")
    os.makedirs(work, exist_ok=True)
    s1 = part1_segments(duration(ff, p1), json.load(open(p1 + ".waits.json", encoding="utf-8")))
    len1 = sum((b - a) / sp for a, b, sp in s1)
    s2 = part2_segments(duration(ff, p2), still(ff, p2), l0, l1)
    to_out = {1: mapper(s1, 0.0), 2: mapper(s2, len1)}
    video_len = duration(ff, cut)
    placed, free_at = [], 0.0
    for k, (part, raw_t, text) in enumerate(LINES):
        path = os.path.join(work, f"line{k:02d}.mp3")
        tts(text, path, voice)
        d = duration(ff, path)
        start = max(to_out[part](raw_t), free_at + 0.3)
        placed.append((start, path, d))
        free_at = start + d
        print(f"{start:6.1f}s  +{d:4.1f}s  {text}", flush=True)
    audio_end = free_at + 1.0
    hold = max(0.0, audio_end - video_len)
    inputs = ["-i", cut]
    for _, path, _ in placed:
        inputs += ["-i", path]
    parts = [f"[{i + 1}:a]adelay={int(s * 1000)}|{int(s * 1000)},aresample=48000[a{i}]" for i, (s, _, _) in enumerate(placed)]
    mix = "".join(f"[a{i}]" for i in range(len(placed))) + f"amix=inputs={len(placed)}:normalize=0,volume=1.6[aud]"
    vid = f"[0:v]tpad=stop_mode=clone:stop_duration={hold:.2f}[v]" if hold > 0 else "[0:v]null[v]"
    subprocess.run([ff, "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(parts + [mix, vid]),
                    "-map", "[v]", "-map", "[aud]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out], check=True)
    print(f"{out}: {duration(ff, out):.0f}s (video {video_len:.0f}s, held {hold:.1f}s at the end)", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:3], sys.argv[3], sys.argv[4], float(sys.argv[5]), float(sys.argv[6]),
         *(sys.argv[7:8] or []))
