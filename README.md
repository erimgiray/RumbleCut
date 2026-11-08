# RumbleCut

A Windows desktop app that **downloads long Rumble videos and livestream replays, finds the most interesting moments by itself, and exports them as short, polished clips**. You can export them one by one, as vertical Shorts, or as a "best moments" compilation with transitions.

---

## Build the .exe (one time, about 5 minutes)

1. Install **Python 3.10+** from python.org. Tick **"Add python.exe to PATH"** during setup.
2. Unzip this folder anywhere, e.g. `C:\Users\Erim\Documents\RumbleCut`.
3. Double-click **`build_exe.bat`**.

The script creates a private Python environment, installs the libraries, downloads FFmpeg (the build includes NVIDIA GPU encoding), and builds the app. When it's done, Explorer opens on:

```
dist\RumbleCut\RumbleCut.exe      ← run this (keep the RumbleCut folder together)
```

Options:

| Command | Result |
|---|---|
| `build_exe.bat onefile` | a single `dist\RumbleCut.exe` (easy to move around, but slower to start) |
| `run_from_source.bat` | runs the app without building it (for development) |

**No Python on the PC you're sending it to?** Push the folder to GitHub. The included workflow (`.github/workflows/build-exe.yml`) builds the .exe on GitHub's Windows machines, and you download it from the run's *Artifacts*.

> Rumble changes its site from time to time. If downloads start failing, run `build_exe.bat` again. It pulls the latest **yt-dlp** downloader.

---

## Using it

**① Get video:** paste a `rumble.com/...` link, pick a quality, and click **Download**. Large streams resume if interrupted. You can also open any video already on your PC.
- **Only download part of the video:** tick the box and type the ranges you want, e.g. `1:05:00-1:20:00, 2:10:30-2:16:00`.
  - Times can be `h:mm:ss`, `mm:ss`, plain seconds, or `1h5m`.
  - Leave the end blank (`2:30:00-`) to mean "to the end".
  - Overlapping ranges are merged.
  - Each range is saved as its own file, and only those bytes are fetched.
  - Press **Check link** first to see the video's length and how much you're skipping.

  Clip titles and `clips.csv` still show times in the **original stream**, so you can find each moment again.

**⚡ Scan audio first (fastest for long streams):** paste the link and press **⚡ Scan audio first** instead of Download.
1. Only the sound is downloaded. If Rumble has no separate audio track, the version with the best sound is fetched and only its sound is kept, copied exactly as it is in the stream (no re-encoding), as an `.m4a`.
2. The best moments appear in the Clip studio. Press **▶ Play clip** to listen to each one, and tick the ones you like.
3. Press **⬇ Get video for ticked moments**.

   Tick **Only download part of the video** first, and the audio scan only fetches those parts too. They're joined into one scan, and every time shown is still the real stream time. Only those parts of the video are downloaded, with 3 seconds of padding either side. They become normal clips you can edit and export.

**Your downloads** shows **🔊 Audio scans** and **🎬 Videos** in separate lists, with each file's length and size. Unfinished downloads left behind by a cancel are marked ⏸; download again to resume, or delete them. Select one or more (Ctrl/Shift-click) and press **🗑 Delete selected** (or the Delete key) to move them to the Recycle Bin.

The **Only download part of the video** box and its times are remembered between restarts. They apply to both **Download** and **⚡ Scan audio first**, and the status line always says what's being fetched (e.g. "Audio of 2 parts (25:00 of 4:05:48)" or "Audio of the whole stream").

**② Clip studio**
- **How moments are judged** (the **Judge by** box):
  - **Voice & energy only** (fast): pitch compared with the speaker's normal (upbeat = higher), how animated the voice is, talking pace, laughter, and loudness as a minor signal.
  - **AI reads lively parts + samples** (recommended):
    - **What gets read:** the ~20 liveliest stretches *and* a passage every few minutes across the whole stream are transcribed on your PC. Calm-but-great moments get read too.
    - **How it's rated:** your AI model (Settings → AI models) rates each passage as funny, hot take, shocking, heated, emotional, quotable or story payoff.
    - **How it ranks:** its rating **leads the ranking**, and voice energy is only a tie-breaker. An AI pick's clip is timed to the exact passage the AI chose.
  - **AI reads everything** (slowest): the whole stream is read, so nothing is missed.
- **🧠 Look for:** tell the AI what you want, e.g. "funny moments", "political hot takes", "anything about Amy Acton". Then press **↻ Ask AI again** to re-rank. It reuses the transcript from the last scan, so it's much faster than a new scan.
- **AI log** (the tab next to Moments) shows live what the AI is reading and what it finds, with the quote and the reason. Use Copy log to keep it.
- **Summary** (the tab next to AI log): the AI describes each section it reads, e.g. "2:01:00 – 2:14:00 · argues with a viewer about the Vivek ad", with an interest rating (●●●●○ 8/10).
  - Neighbouring sections that are the same conversation are joined into one. Sections never run across two separately downloaded parts.
  - The ratings steer the ranking: moments come from the interesting sections, and transitions, filler, chat-reading, technical trouble and coughing are pushed down. Each moment's hover text says which section it's part of.
  - The sections also show as a thin strip along the bottom of the timeline (gold = interesting, grey = filler).
  - Click a section to listen to it. Tick the ones you like and press **★ Add to Moments** (or double-click one) to add them to the top of Moments as **your picks**.
- **Your picks** (sections you add, clips you add with **＋ Add** or **⧉ Copy**) are marked ★ and shown in gold, at the top of Moments and on the timeline, so they don't mix with the app's guesses. They're kept when you run **Find highlights** or **↻ Ask AI again**.
- **Show / Min length / Max length / Gap** are dropdowns with common choices (5 / 10 / 15 moments; 5 to 30 min; 10 / 20 / 30 seconds). You can also type your own value, e.g. `45 s`, `2 min` or `1:30`.
- **Find highlights** scans the whole stream and draws an *excitement timeline*. Every moment it finds is ranked, but only the best **5** are shown at first, each with a reason ("Volume spike +18 dB over normal", "Sudden reaction", "Said 'no way'"). Press **＋ 5 more moments** under the list for the next batch. The batch size is the **Show** box.
- **Hover over any moment** in the list, or over its block on the timeline, to see **why it was picked**. For example: "27 dB louder than the 2 minutes around it", "volume jumped 20 dB in a few seconds", "you said 'no way'". You also see **why it starts and ends where it does**.
- **How each clip's time range is chosen:**
  1. The clip covers the stretch where the excitement is clearly above normal around the peak.
  2. It starts a few seconds earlier, so viewers get the build-up, and ends 2 s after the energy settles.
  3. It's kept between your **Min length** and **Max length**. Short moments get extra lead-in; long ones are trimmed around their peak.
  4. Both ends are moved to natural pauses (or scene cuts), so nobody is cut off mid-word.
- **Zoom the timeline:** scroll up on it to zoom in (around the mouse), down to zoom out, or use the **＋ / － / Fit** buttons beside it. Shift+scroll moves left and right while zoomed; a green bar along the top shows which part you're looking at, and the view follows the playhead and the moment you select.
- **Scrubber:** the bar under the player shows where you are; drag it (or drag across the main timeline) to move anywhere. Blue parts have video; the rest is audio only. Where there's nothing to show (an audio-only part, or nothing loaded), the player shows an icon and a note explaining why, and you get a notice when you scrub out of a part with video.
- **The moment's own timeline:** click a moment and a zoomed timeline for it slides open under the main one. Drag its handles to set the exact start and end (in 0.1 s steps), drag the middle to move the whole moment, or click outside it to jump there. You hear the edge as you drag it.
- **Start / End** in the Clip panel show and accept stream times (`2:14:05.5`).
- Click a clip to preview it. Use **[ Set start / Set end ]** (keys `I` / `O`) to trim, drag clips to reorder them, and untick the ones you don't want.
- **Effects per clip:**
  - speed (0.5× to 2×)
  - punch-in zoom
  - colour looks (Vivid, B&W, Warm, Cool, Cinematic, Retro)
  - vignette
  - fade in/out
  - loudness levelling
  - captions (top or bottom)
  - the transition into the clip
  - **images on screen**, timed to the clip, with fades and a border
- **Preview with effects** renders a quick low-res version so you can check the look before exporting.

**③ Export**
- **Shapes:** Original, 16:9, **9:16 vertical** (crop or blurred background), and 1:1.
- **Settings:** 480p to 4K, 24 to 60 fps, quality, and an optional watermark such as `@yourchannel`.
- **Exports:** each clip as its own MP4 (plus a thumbnail), and/or a **compilation** joined with 16 transitions (cross-fade, fade through black, wipes, slides, circle, radial, zoom, pixelate, blur, random mix and more).
- A `clips.csv` lists each clip's timestamps in the original stream and why it was picked.

Encoding uses the **NVIDIA GPU (NVENC)** automatically when one is present. You can turn this off in *☰ → File → Settings*.

Projects can be saved and reopened later (`.rcproj`), including the AI's Summary and your picks. **File** and **Help** live in the **☰** menu at the left of the tabs; the keyboard shortcuts (Ctrl+O, Ctrl+S…) still work.

**✨ Context images:** coming soon (see below).

**⚙ Settings**
- Folders, GPU encoding and the speech model. Changes save automatically.
- **Updates:**
  - **Rumble downloader:** a one-click update when Rumble changes its site, with no rebuild needed. It's installed into `%APPDATA%\RumbleCut\updates` and used from the next launch. *Undo downloader update* goes back to the downloader that came with the app.
  - **RumbleCut app:** downloads the newest `RumbleCut.exe` from your GitHub Releases, swaps it in and relaunches. This needs the single-file build, which you get from `build_onefile.bat`.
  - The app checks quietly at start-up, and the Settings tab gets a ● when an update is available.
  - **Relaunch RumbleCut** restarts the app; you're offered the chance to save the project first.

**✨ AI titles & captions** (Clip studio): **✨ Write title & caption with AI** on one clip, or **✨ AI titles for all ticked clips**. Each clip's speech is transcribed on your PC (Whisper), then your chosen AI model writes a short title and an on-screen caption.

### AI models: local or cloud (Settings → AI models)
| Mode | What runs where | Setup |
|---|---|---|
| **On this PC** (default) | Everything runs locally: private and free, using your GPU | Install [Ollama](https://ollama.com), then run e.g. `ollama pull llama3.1` (or `qwen2.5:14b`), press ⟳ and pick the model. LM Studio, llama.cpp and other OpenAI-compatible servers work too: choose "OpenAI-compatible" and enter the server address. |
| **Cloud** | Claude via the Anthropic API | Paste an API key from console.anthropic.com. It's stored encrypted for your Windows account. The default model is `claude-haiku-5-5`. |

Speech-to-text always runs on your PC.
- **If you have an NVIDIA GPU:** press **⚡ Install GPU speed-up** (Settings → AI models) for ~10–20× faster transcription.
  - It's a one-time 550 MB download of NVIDIA's free cuBLAS libraries from PyPI, about 800 MB on disk.
  - The app tests the GPU before using it, and falls back to the CPU automatically if it can't run there.
- **The 🧠 AI log** says which one is being used. The speech model downloads once, on first use, into `%APPDATA%\RumbleCut\models`. **Test AI** checks the setup.

### Rumble 403 errors
Rumble refuses requests that don't look like a real browser. RumbleCut sends its requests with Chrome's browser fingerprint (yt-dlp with `curl_cffi`), which this needs. If a download still fails with 403, the video may be private, members-only or region-locked. Otherwise, try **Settings → Updates → update the Rumble downloader**.

### Publishing an app update
1. Change `__version__` in `rumblecut/__init__.py`, e.g. to `1.2.0`.
2. Commit and push to `main` on GitHub.
3. The workflow builds `RumbleCut.exe`. Because v1.2.0 doesn't exist yet, it publishes it as a Release. Every copy of the app then sees the update under Settings → Check for updates.

Pushes that don't change the version just build a test `.exe`, which you can download from the run's Artifacts.

The repository the app checks can be changed in Settings. It's `erimgiray/RumbleCut` by default.

---

## How highlight detection works

Everything runs locally, with no AI service or internet needed:

| Signal | What it catches |
|---|---|
| **Excitement:** loudness compared with the *local* 2-minute baseline | shouting, laughing, crowd noise. Because it's relative, quiet streams still produce highlights. |
| **Rise:** sudden jumps in loudness | reactions, the instant something happens |
| **Sustained energy** | longer heated stretches |
| **Scene changes** (optional) | bursts of on-screen action, camera cuts |
| **Hype words** (optional, needs `faster-whisper`) | "no way", "let's go", "oh my god", "clip that"… |

The signals are combined into a score. The top peaks are picked with a minimum gap between clips, and each clip is built mostly from the *build-up* to the peak. The start and end then snap to the nearest pause, so clips don't cut mid-sentence.

---

## Architecture

```
app.py                      entry point → RumbleCut.exe
rumblecut/
  downloader.py             yt-dlp wrapper: Rumble videos and stream replays, progress, resume, friendly errors
  analysis.py               moment finding: combines voice, words and energy; picks and times the clips
  voice.py                  how it's said: pitch, pitch swings, pace, laughter (streams audio, 4 h ≈ 1 min)
  diction.py                what's said: AI reads the transcript and rates passages; offline word list
  transcribe.py             optional faster-whisper speech-to-text (GPU when available)
  editor.py                 Clip / Project model, effects, aspect ratios, overlays, transitions, batch export
  context_images.py         interfaces for the upcoming Context Images feature
  ai.py                     one switch for AI: local (Ollama / OpenAI-compatible) or cloud (Claude)
  ffmpeg_tools.py           finds the bundled ffmpeg, runs jobs with progress and cancel, NVENC detection
  config.py                 settings in %APPDATA%\RumbleCut
  updater.py                downloader + app self-update, relaunch
  ui/main_window.py         PySide6 interface (4 tabs), dark theme
  ui/timeline.py            excitement timeline widget
  ui/workers.py             background threads, so the UI never freezes
tests/test_engine.py        end-to-end check on a synthetic stream:  python -m tests.test_engine
```

## Coming feature: Context Images

The goal is to automatically show a relevant picture whenever the streamer talks about something (a person, a place, a headline or a product), the way a video editor adds B-roll.

```
clip audio → transcribe (word timings) → TopicExtractor → ImageProvider(s) → ContextImagePlanner → ImageOverlay → export
```

- **Already done:**
  - the rendering side (`ImageOverlay`: timed, positioned, faded, bordered). You can use it by hand today with *＋ Image at playhead*.
  - the plug-in interfaces in `context_images.py`, with a working offline topic extractor and a "my image folder" provider.
- **Still to do:**
  - an online provider (Wikimedia Commons / Pexels / search API)
  - an LLM topic extractor, for better topics than the keyword heuristic
  - the tab's settings UI
  - a review step where suggestions appear in each clip's image list before export

---

Only download and re-upload videos you own or have permission to use.
