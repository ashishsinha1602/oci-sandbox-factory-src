"""Append the Studio and MCP scenes (from an earlier take) to a narrated demo.

    python append_scenes.py out.mp4 main_voice.mp4 part2_with_scenes.webm

The scenes are the sandbox's own pages (Studio, an agent's MCP calls), so they
join a take recorded on another install without a visible seam. Each scene is
held until its line is spoken, the MCP calls play a little faster, and both
files are re-encoded alike before the join.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from edit_demo import duration, ffmpeg  # noqa: E402
from narrate import tts  # noqa: E402
from narrate_short import TEMPO  # noqa: E402

STUDIO = (94.5, 104.0, 1.0)      # raw start, raw end, speed
MCP = (128.5, 153.7, 1.4)
LINES = [
    "And there is more. Ask for a database with Studio and an MCP endpoint, and you explore the data in plain English.",
    "Agents connect over MCP. They discover the tools, pick the tables, and run the query. That is Sandbox Factory.",
]


def say(text, path, voice="Brian"):
    raw = path + ".raw.mp3"
    tts(text, raw, voice)
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-i", raw, "-filter:a", f"atempo={TEMPO}", path], check=True)
    return duration(ffmpeg(), path)


def main(out, main_mp4, src):
    ff = ffmpeg()
    work = os.path.join(os.path.dirname(out), "narration_append")
    os.makedirs(work, exist_ok=True)
    voices = [os.path.join(work, f"line{k}.mp3") for k in range(2)]
    durs = [say(t, p) for t, p in zip(LINES, voices)]
    s_len = (STUDIO[1] - STUDIO[0]) / STUDIO[2]
    s_hold = max(0.0, durs[0] + 1.0 - s_len)
    m_len = (MCP[1] - MCP[0]) / MCP[2]
    m_hold = max(2.0, durs[1] + 1.0 - m_len)
    clip = os.path.join(work, "scenes.mp4")
    vf = (f"[0:v]trim=start={STUDIO[0]}:end={STUDIO[1]},setpts=(PTS-STARTPTS)/{STUDIO[2]},fps=25,tpad=stop_mode=clone:stop_duration={s_hold:.2f}[s];"
          f"[0:v]trim=start={MCP[0]}:end={MCP[1]},setpts=(PTS-STARTPTS)/{MCP[2]},fps=25,tpad=stop_mode=clone:stop_duration={m_hold:.2f}[m];"
          f"[s][m]concat=n=2:v=1:a=0[v];"
          f"[1:a]adelay=300|300,aresample=48000[a0];"
          f"[2:a]adelay={int((s_len + s_hold + 0.3) * 1000)}|{int((s_len + s_hold + 0.3) * 1000)},aresample=48000[a1];"
          f"[a0][a1]amix=inputs=2:normalize=0,volume=1.6,apad=whole_dur={s_len + s_hold + m_len + m_hold:.2f}[a]")
    subprocess.run([ff, "-loglevel", "error", "-y", "-i", src, "-i", voices[0], "-i", voices[1], "-filter_complex", vf,
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-ar", "48000", clip], check=True)
    print(f"scenes clip: {duration(ff, clip):.1f}s (studio {s_len:.1f}+{s_hold:.1f}, mcp {m_len:.1f}+{m_hold:.1f})", flush=True)
    subprocess.run([ff, "-loglevel", "error", "-y", "-i", main_mp4, "-i", clip, "-filter_complex",
                    "[0:v]fps=25,setsar=1[v0];[1:v]fps=25,setsar=1[v1];[0:a]aresample=48000[a0];[1:a]aresample=48000[a1];[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out], check=True)
    print(f"{out}: {duration(ff, out):.0f}s", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:4])
