"""End-to-end engine check on a synthetic 'stream' (no internet needed).

    python -m tests.test_engine

Makes a 3-minute video with loud moments at 0:50 and 2:10, checks the detector finds
both, then exports clips with effects, a vertical version and a transition compilation.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from rumblecut import analysis, editor
from rumblecut import ffmpeg_tools as ft

OUT = Path(__file__).parent / "_out"


def make_stream(path: Path) -> None:
    ft.run([ft.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=180",
            "-f", "lavfi", "-i", "anoisesrc=d=180:c=pink:r=48000:a=0.02:seed=42",
            "-f", "lavfi", "-i", "sine=f=440:d=180:r=48000",
            "-filter_complex",
            "[2:a]volume='if(between(t,48,54)+between(t,128,133),0.9,0)':eval=frame[s];"
            "[1:a][s]amix=inputs=2:normalize=0[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
            "-c:a", "aac", "-shortest", str(path)])


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    src = OUT / "stream.mp4"
    make_stream(src)
    res = analysis.find_highlights(str(src), num_clips=3, clip_length=15, min_gap=20)
    peaks = sorted(h.peak for h in res.highlights[:2])
    print("highlights:", [(h.start, h.end, h.score) for h in res.highlights])
    assert 45 <= peaks[0] <= 57 and 125 <= peaks[1] <= 136, f"unexpected peaks {peaks}"

    clips = [editor.Clip(source=str(src), start=h.start, end=h.end, title=f"Test {i}")
             for i, h in enumerate(res.highlights[:2], 1)]
    clips[0].caption, clips[0].zoom, clips[0].color = "Caption test", 1.2, "Vivid"
    clips[1].speed, clips[1].color = 1.5, "Black & white"
    proj = editor.Project(source=str(src), title="Engine test", clips=clips)
    proj.export.height, proj.export.compilation = 360, True
    proj.export.aspect = "Vertical 9:16 (blurred background)"
    files = editor.export_project(proj, OUT)
    for f in files:
        info = ft.MediaInfo(f)
        print(Path(f).name, info)
        assert info.duration > 5 and info.width == 360 and info.height == 640
    print("ENGINE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
