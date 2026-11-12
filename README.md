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
- **Only download part of the video:** tick the box and fill in **Starts from** and **Ends at** (e.g. `1:05:00` and `1:20:00`). Press **⏭ To the end** to run that part to the end of the video, and **＋ Add another part** for more parts.
  - Times can be `h:mm:ss`, `mm:ss`, plain seconds, or `1h5m`.
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
- **Summary** (the tab next to AI log): the AI describes each section it reads, e.g. "2:01:00 – 2:14:00 · argues with a viewer about the Vivek ad", with an interest rating out of five stars (each star is 2 points, so 5/10 shows 2½ stars).
  - Neighbouring sections that are the same conversation are joined into one. Sections never run across two separately downloaded parts.
  - The ratings steer the ranking: moments come from the interesting sections, and transitions, filler, chat-reading, technical trouble and coughing are pushed down. Each moment's hover text says which section it's part of.
  - The sections also show as a thin strip along the bottom of the timeline (gold = interesting, grey = filler).
  - Click a section to listen to it. Tick the ones you like and press **★ Add to Moments** (or double-click one) to add them to the top of Moments as **your picks**.
- **Your picks:** sections you add from the Summary are marked ★ and shown in gold; moments you make yourself (right-click on the timeline, **＋ Add**, **⧉ Copy**) are marked ✎ and shown in blue. Both sit at the top of Moments, so they don't mix with the app's guesses. They're kept when you run **Find highlights** or **↻ Ask AI again**.
- **Show / Min length / Max length / Gap** are dropdowns with common choices (5 / 10 / 15 moments; 5 to 30 min; 0 / 10 / 20 / 30 seconds). You can also type your own value, e.g. `45 s`, `2 min` or `1:30`.
- **Find highlights** scans the whole stream and draws an *excitement timeline*. Every moment it finds is ranked, but only the best **5** are shown at first, each with a reason ("Volume spike +18 dB over normal", "Sudden reaction", "Said 'no way'"). Press **＋ 5 more moments** under the list for the next batch. The batch size is the **Show** box.
- **Hover over any moment** in the list, or over its block on the timeline, to see **why it was picked**. For example: "27 dB louder than the 2 minutes around it", "volume jumped 20 dB in a few seconds", "you said 'no way'". You also see **why it starts and ends where it does**.
- **How each clip's time range is chosen:** a clip should make sense to someone who hasn't seen the stream, so it follows the moment's rhetorical structure.
  1. **With an AI model,** the AI marks three points for each moment: where the context starts (the question being answered, the claim being responded to, the start of the story or argument), the moment itself, and where the point has landed (the punchline, the conclusion, the reaction). The clip runs from the first to the last. The hover text says what you hear first, e.g. "a viewer asks about tariffs".
  2. **Without one,** the clip covers the stretch where the excitement is clearly above normal, starting a few seconds earlier for the build-up, but never reaching back into a different topic.
  3. It's kept between your **Min length** and **Max length**. If the full setup is longer than the maximum, the clip starts at the latest sentence that still keeps the payoff.
  4. Wherever the speech was transcribed, both ends land on **whole sentences**; elsewhere they move to natural pauses or scene cuts. Nobody is cut off mid-sentence.
  5. Passages the AI reads reach about 75 s before each lively stretch, so it hears what led up to it. Press **↻ Ask AI again** on an older scan to get the new timing.
- **On the timeline:** right-click empty space to add a moment of your own there (shown blue and dashed, ✎). It's 5 seconds long; change that in *Settings → Moments you add*. Click a moment to select it; the selected one is drawn on top, and you can drag it to move it or drag its edges to resize it (it can't go off the visible part). Right-click a moment (here, in the Moments list, or on its zoomed timeline) for options: play it, trim its start or end at the clicked spot or the playhead, split it in two, **↺ Reset** (timing back to how it was found, remove script cuts, effects back to normal, or everything), **💾 Save as clip…** (exports just that moment wherever you choose), duplicate, deselect or delete it. While a clip plays, its box glows white with ▶ on both timelines so you can tell it from the others.
- **Overlaps:** the zoomed timeline under the video also shows the other moments that overlap it (dashed and numbered); right-click one of them for its own menu. When the moment you right-click covers the start or end of another clip, the menu offers **✨ AI: find a good start/end for "…"**: RumbleCut transcribes what's said around that point and your AI picks the sentence where the clip should begin (so the topic is introduced first) or end (right after the payoff). **✨ AI: this moment's start or end** does the same for the moment itself.
- **🙈 Hide this moment** turns it into a see-through box with faint diagonal lines and a **?** above it, on both timelines (and the Script tab). It's still exported if ticked; right-click → **👁 Show this moment** brings it back.
- **Zoom the timeline:** scroll up on it to zoom in (around the mouse), down to zoom out, or use the **＋ / － / Fit** buttons beside it. Shift+scroll, or the scroll bar that appears under the timeline, moves left and right while zoomed (drag it, or click either side to jump a screen), and the view follows the playhead and the moment you select.
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
- **Every clip has its own row:** a thumbnail, a file name you can change, and its own **shape** and **quality** (or *Same as all*, the defaults on the right). **💾 Export** saves just that clip; **✏** opens it in the Clip studio. Tick the ones you want and **Export ticked clips** does them together, as separate videos and/or one compilation.
- **Shapes:** Original, 16:9, **9:16 vertical** (crop or blurred background), and 1:1.
- **Settings:** 480p to 4K, 24 to 60 fps, quality, and an optional watermark such as `@yourchannel`.
- **Exports:** each clip as its own MP4 (plus a thumbnail), and/or a **compilation** joined with 16 transitions (cross-fade, fade through black, wipes, slides, circle, radial, zoom, pixelate, blur, random mix and more).
- A `clips.csv` lists each clip's timestamps in the original stream and why it was picked.

Encoding uses the **NVIDIA GPU (NVENC)** automatically when one is present. You can turn this off in *☰ → File → Settings*.

Your work is kept automatically: the scan (timeline curve and the moments not shown yet), the Summary, every moment with its edits, cuts, pictures and Publish text are saved every 30 seconds and when you quit or update. Next time RumbleCut opens, it picks up where you left off. Projects can also be saved and reopened later (`.rcproj`), including the AI's Summary and your picks. **File** and **Help** live in the **☰** menu at the left of the tabs; the keyboard shortcuts (Ctrl+O, Ctrl+S…) still work.

**🎬 Edit clips:** context pictures that match what's being said - see [Context images](#context-images) below.

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
| **On this PC** (default) | Everything runs locally: private and free, using your GPU | Install [Ollama](https://ollama.com) and pick a model. The list shows the models you have, then popular ones marked ⬇ (with download size and what they suit); pick one of those and RumbleCut downloads it into Ollama for you. LM Studio, llama.cpp and other OpenAI-compatible servers work too: choose "OpenAI-compatible" and enter the server address. |
| **Cloud** | Claude via the Anthropic API | Paste an API key from console.anthropic.com. It's stored encrypted for your Windows account. The Model list shows Haiku 5.5 (default), Sonnet 5.5, Opus 5.5 and Fable 5.1, plus every other model your key can use (loaded from your account; ⟳ reloads it). |

Speech-to-text always runs on your PC. Every Whisper model is in the list, from `tiny` (75 MB) to `large-v3` (3.1 GB), including the English-only `.en` models and the fast `distil` and `large-v3-turbo` ones.
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

The signals are combined into a score. The top peaks are picked with a minimum gap between clips, and each clip is built mostly from the *build-up* to the peak. With an AI model the clip starts where the context starts and ends when the point lands; the start and end then snap to whole sentences (or the nearest pause), so clips don't cut mid-sentence.

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
  context_images.py         Context images: topics, picture sources, downloads, credits
  ai.py                     one switch for AI: local (Ollama / OpenAI-compatible) or cloud (Claude)
  ffmpeg_tools.py           finds the bundled ffmpeg, runs jobs with progress and cancel, NVENC detection
  config.py                 settings in %APPDATA%\RumbleCut
  updater.py                downloader + app self-update, relaunch
  ui/main_window.py         PySide6 interface (4 tabs), dark theme
  ui/timeline.py            excitement timeline widget
  ui/workers.py             background threads, so the UI never freezes
tests/test_engine.py        end-to-end check on a synthetic stream:  python -m tests.test_engine
```

## 🚀 Publish

Post moments to **YouTube, Instagram, TikTok and X** straight from RumbleCut.

1. **Set up each platform once** in *Settings → Publishing accounts*: press **Setup steps** for that platform, create your own free developer app (the steps link straight to the right page), paste its keys, and press **Connect** to sign in in your browser. The redirect / callback address to give each platform is shown there: `http://localhost:47123/callback/`. Logins are stored encrypted for your Windows account and refresh themselves.
2. In the **🚀 Publish** tab, pick a moment and tick the platforms. **✨ Write titles, captions & hashtags with AI** (or **Write posts for all ticked**) drafts a post per platform from what's said in the clip; edit anything.
3. **Shape:** TikTok, Instagram and YouTube clips up to 3 minutes default to **vertical 9:16** (a Short / Reel); X and longer YouTube videos keep the original shape. Change it per platform. Effects and approved pictures are included.
4. **🚀 Post this moment** or **Post all ticked moments**, now or **scheduled**: YouTube schedules the video itself; the others are posted by a Windows scheduled task at that time (RumbleCut can be closed, the PC must be on). Results and links appear under *Posted and scheduled* (double-click to open; select a scheduled one to cancel it).

| Platform | How it posts | Notes |
|---|---|---|
| YouTube | Straight to your channel | Free; public, unlisted or private; up to 100 uploads a day |
| Instagram | A Reel on your profile | Free; needs a Creator/Business account linked to a Facebook Page (switching is free) |
| TikTok | A draft in your TikTok inbox | Free; RumbleCut copies the caption, you paste it and press Post in TikTok (direct public posting needs TikTok to audit the app) |
| X | A post with the video | Pay-per-use API credit, about $0.015 per post; videos up to 2:20 |

## ✂ Cut words by script

Trim a clip by editing its transcript, like a document: delete words in the text and they're cut from the video.

1. In **🎬 Edit clips → ✂ Script** pick a moment (or press **✂ Cut words by script** in the Clip studio) and press **📝 Get the script**. Every word is written out with its own timing. Gaps show as **⏸ pauses** (blue) or **[… ]** (purple) where someone spoke but the speech-to-text left it out; that's usually an "um".
2. **Select words and press Delete** to cut them; **Enter** brings them back; double-click a word to toggle it. **Ctrl+Z / Ctrl+Y** undo and redo.
3. Let RumbleCut suggest cuts (amber, struck through), then **✓ Accept** or **✖ Keep** each one, or all at once:
   - **🧹 Quick clean-up** (no AI): "um"/"uh", stutters and repeated words ("I I think"), pauses longer than 0.6 s (shortened to 0.25 s), and unscripted bits.
   - **✨ Tighten with AI**: your AI marks asides, false starts, filler phrases and restated points, keeping the setup and the payoff (hover a suggestion to see why).
4. **▶ Play the edit** plays the moment skipping everything cut, highlighting each word as it's said; click any word to play from there. Playing the clip in the Clip studio skips the cuts too, and the moment's timeline shows them hatched.
5. **Jump cuts:** *punch-in zoom* (default) zooms in 8% on every other piece so cuts look like a camera change; *hard cut* just joins them. Cuts land at the quietest point between words, with a tiny audio fade, so nothing clicks.

Context pictures stay on the words they belong to when you cut around them, and the export, Publish and preview all use the edited clip.

**Where you are:** above the script, a strip of the whole stream shows every moment (the one you're editing is outlined; click another to switch, drag one to move it, right-click for options). Under it, the moment's own timeline shows the cuts hatched; drag its handles to change where it starts and ends. If you stretch it past the words in the script, press **📝 Get the script again**.

**This moment's look** (beside the script): title, speed, zoom, colour look, vignette, fades and caption, kept in step with the Clip studio. **👁 Preview with effects** renders a small copy with everything applied and plays it there.

## Context images

When the streamer mentions a person, place, organisation, event or thing, RumbleCut suggests a picture of it for that exact moment, like an editor adding B-roll. You approve every picture: only approved ones are exported.

1. Pick a moment in the **🎬 Edit clips** tab (or press **✨ Find pictures** in the Clip studio's *Images on screen*) and press **✨ Find pictures for this moment**, or **Find for all ticked moments**.
2. RumbleCut transcribes the moment, your AI (Settings → AI models) picks what's worth showing and when (without an AI, names in the transcript are used), and pictures are fetched from the sources you tick:
   - **Wikipedia / Wikimedia**: Wikipedia article pictures and Wikimedia Commons. Free to reuse with credit, no key needed. Best for people, places, organisations and events.
   - **Pexels stock photos**: free stock photos for general things (money, crowds, oil rigs). Needs a free API key.
   - **Web image search (Brave)**: the widest coverage, but most results are copyrighted, so check before you publish. Needs a Brave Search API key (monthly free credit, about 1,000 searches).
   - Add keys in *Settings → Context images*. They're stored encrypted for your Windows account.
3. Review each suggestion (gold = to review, green = approved):
   - see it over the clip's frame, exactly as it will look;
   - click another picture in the strip to swap, or **🔍 Search again** with your own words;
   - choose the **Look**: *Corner pop-up* (pick the corner and size, optionally with the name in a strip under it) or *Full-screen cutaway* (fills the frame with a slow zoom);
   - set when it **Appears** and how long it **Shows**;
   - **✓ Approve**, **✖ Remove**, or **Approve all** for the moment.
4. Export as usual. A **picture credits.txt** is saved next to the clips: author, licence and link for every picture, ready to paste into the description.

Finding pictures again replaces only the suggestions you haven't approved; approved pictures and ones you added by hand stay.

---

Only download and re-upload videos you own or have permission to use.
