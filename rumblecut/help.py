"""The Help page: what every part of RumbleCut does, one topic per feature (Markdown)."""

TOPICS: list[tuple[str, str, str]] = [
    # (icon, title, markdown)
    ("👋", "Getting started", """
# Getting started

RumbleCut turns a long Rumble stream into short clips:

1. **① Get video** - paste a Rumble link and download it (or just its sound, which is much faster).
2. **② Clip studio** - press **Find highlights**. The best moments appear in the list and on the timeline.
   Listen to them, trim them, cut words out of them, add pictures and effects.
3. **③ Export & publish** - save the moments as video files, or post them straight to YouTube, Instagram,
   TikTok and X.

Your work is saved automatically every 30 seconds, when you quit and when you update, and RumbleCut opens
where you left off.

**Tips**
- Hover over almost anything to see what it does.
- Right-click moments, the timeline and words in the script for more options.
- **Ctrl+Z** undoes your last change to the moments, **Ctrl+Shift+Z** redoes it.
- Press **F1** (or the **?** in Settings) to open this help.
"""),
    ("⬇", "① Get video", """
# ① Get video

Paste a `rumble.com/...` link, pick a quality and press **Download**. Big streams carry on where they left
off if a download is interrupted. **☰ → File → Open video…** (Ctrl+O) uses a file that's already on your PC.

**⚡ Scan audio first (fastest for long streams)**
1. Press **⚡ Scan audio first** instead of Download. Only the sound is downloaded, exactly as it is in the
   stream (no quality loss).
2. The best moments appear in the Clip studio. Listen with **▶ Play clip** and tick the ones you like.
3. Press **⬇ Get video for ticked moments** - only those bits of the video are downloaded (with 3 seconds
   either side), and they become normal clips.

**Only download part of the video**
- Tick the box, then fill in **Starts from** (empty = 0:00) and **Ends at** (empty, shown as —, = to the
  end). **⏭ To the end** runs that part to the end of the video.
- **＋ Add part** adds another part. Overlapping parts are joined.
- Times can be `1:05:00`, `65:00`, plain seconds or `1h5m`.
- **Check link** shows the video's length and how much you're skipping.
- It works for both Download and Scan audio first, and every time you see later is still the real stream
  time.

**Your downloads** lists audio scans and videos separately with their length and size. Unfinished ones are
marked ⏸ - download again to resume. Select some and press **🗑 Delete selected** to move them to the
Recycle Bin.

**Rumble refuses the download (403)?** The video may be private, members-only or region-locked. Otherwise
try *Settings → Updates → Update the Rumble downloader*.
"""),
    ("🔎", "Finding highlights", """
# Finding highlights

Set the options in the top row of the Clip studio and press **Find highlights**.

- **Show** - how many moments to show at a time (more with **＋ more moments** under the list).
- **Min length / Max length** - how long each clip may be.
- **Gap** - the least time between two moments.
- **Scene changes** - also notice camera cuts and busy action on screen.
- Type your own values too, e.g. `45 s`, `2 min`, `1:30`.

**Judge by**
- **AI reads lively parts + samples** (recommended) - the liveliest stretches *and* a passage every few
  minutes are written out on your PC, and your AI model rates what's said: funny, hot take, shocking,
  heated, emotional, quotable, story payoff.
- **AI reads everything** - slowest, misses nothing.
- **Voice & energy only** - fast, but doesn't understand the words.

**Only what's said makes a moment.** A stretch that just sounds lively (higher pitch, faster talking,
louder) is never picked for that alone. If fewer moments stand out than you asked for, you get fewer.

**🧠 Look for** - tell the AI what you want ("funny moments", "anything about Amy Acton") and press
**↻ Ask AI again**. It re-ranks using the text from the last scan, so it's quick. The box starts empty each
time RumbleCut opens.

**How a clip's start and end are chosen** - a clip should make sense to someone who hasn't seen the stream:
it starts where the topic is introduced (the question, the claim being answered, the start of the story) and
ends once the point has landed. Both ends fall on whole sentences, and the clip stays within your Min and
Max length. Hover a moment to see why it was picked and why it starts and ends where it does.

**AI log** shows live what the AI reads and finds. **Summary** - see its own topic.
"""),
    ("📋", "Summary", """
# Summary

The **Summary** tab (next to Moments and AI log) lists each section of the stream the AI read, e.g.
*2:01:00 – 2:14:00 · argues with a viewer about the ad*, with stars for how interesting it is
(5 stars = 10/10, half stars in between).

- Neighbouring sections about the same thing are joined; sections never run across two separately
  downloaded parts.
- The ratings steer which moments are picked - filler, chat-reading and technical trouble are pushed down.
- A thin strip along the bottom of the timeline shows the sections (gold = interesting, grey = filler).
- Click a section to listen to it. Tick the ones you like and press **★ Add to Moments** (or double-click
  one) to add them as **your picks**.

It needs an AI model (*Settings → AI models*) and one of the "AI reads…" options in **Judge by**.
"""),
    ("🎞", "Moments & the timeline", """
# Moments & the timeline

**The Moments list** - found moments are green; ★ your picks (from the Summary) are gold; ✎ moments you
made yourself are blue. Your own sit at the top and are kept when you scan again. Tick the ones to export.

**Player controls** (top row, next to *Scene changes*) - **⏮** back to the start of the selected moment,
**▶** play/pause (Space), **⏭** to its end (Home / End keys), **−5s / +5s**, the time, and the
listening volume (it never changes what's exported).

**The main timeline**
- The moment you're on is filled in; the others are dotted outlines. While one plays it glows white with ▶.
- Click a moment to select it; drag it to move it, or drag its edges.
- **Right-click empty space** to add your own moment there (5 seconds long - change it in
  *Settings → Moments you add*).
- Scroll up/down on it to zoom in/out (or **＋ / － / Fit**). Shift+scroll or the bar under it moves along.
- The thin strip at the bottom shows the Summary's sections.

**The moment's own timeline** (under the main one) - zoomed in on the selected moment. Drag its handles for
the exact start and end, drag the middle to move it, click inside to put the cursor there. Other moments
that overlap it show dashed and numbered; cuts from the script show hatched.

**Scrubber** - drag it to move anywhere. Blue parts have video; the rest is sound only.
"""),
    ("🖱", "Right-click menu", """
# The right-click menu

Right-click a moment - on either timeline or in the Moments list:

- **▶ Play this moment**
- **✂ Trim** - start or end it at the spot you clicked or at the playhead, or cut words by script
- **Split** it in two at the spot you clicked
- **✨ AI start / end for "…"** - shown when the moment you right-click covers the start or end
  of another clip. RumbleCut listens around that point and your AI picks the sentence where the clip should
  begin (so the topic is introduced first) or end (right after the payoff).
- **✨ AI start / end** (submenu) - the same, for the moment itself
- **↺ Reset** - timing back to how it was found, remove script cuts, effects back to normal, or everything
- **💾 Save as clip…** - export just this moment wherever you choose
- **⧉ Duplicate**
- **🙈 Hide** - it becomes a see-through box with faint diagonal lines and a **?**, everywhere.
  Still exported if ticked. **👁 Show** brings it back.
- **Deselect / Select** (whether it's exported) and **Delete**

**Ctrl+Z / Ctrl+Shift+Z** undo and redo any of these (also in ☰ → Edit).
"""),
    ("🎛", "Clip settings & effects", """
# Clip settings & effects

The **🎛 Clip** tab beside the player:

- **Title** (also the file name), **Start / End** as stream times (`2:14:05.5`), **[ Set start / Set end ]**
  at the playhead (keys `I` / `O`).
- **Effects:** speed (0.5× to 2×), punch-in zoom, colour looks (Vivid, B&W, Warm, Cool, Cinematic, Retro),
  vignette, fade in/out, even out the loudness, captions (top or bottom), the transition into the clip,
  and **images on screen**.
- **Preview effects** renders a quick low-quality copy so you can check the look.
- **✨ Write title & caption with AI** for one clip, or **✨ AI titles for all ticked clips**.
"""),
    ("✂", "Script", """
# Script

The **✂ Script** tab beside the player shows the **whole stream's text**, with the selected moment marked
**⟦3 … 3⟧**. Other moments are tinted in their colour (grey when deselected), so you can see where
they are without their points getting in the way.

**Writing it out** - opening the tab starts writing the whole stream out word by word in the background:
around the selected moment first, then the other moments, then everything else. Until a part is done you
see the rougher text from the scan in grey. **■ Stop** pauses it; press the button again to carry
on. It's saved with your work. (With an NVIDIA graphics card and the GPU speed-up installed it's much
faster.)

**Change where a moment starts and ends**
- **Drag the markers** - hover a **⟦3** or **3⟧** until it becomes a solid chip and the pointer a hand,
  then drag it onto a word. The word it will land on is highlighted while you drag.
- **Right-click any word** - **⟦ Start here**, **End here ⟧**, **⟦ Start of sentence**, **End of sentence ⟧** (by its
  punctuation). Also: **＋ New moment** from the sentence (or **＋ Moment from selection**), go to another moment, play from there.
- **⟦ Start here / End here ⟧** use the word you clicked; **⟦ Selection ⟧** makes the selected words the
  moment.

**Cut words** (inside the selected moment)
- Select words and press **Delete** to cut them, **Enter** to bring them back; double-click toggles a word.
  The video follows the text.
- **🧹 Clean up** suggests cutting "um"/"uh", stutters, repeated words and long pauses.
  **✨ AI tighten** suggests cutting asides, false starts and restated points. Suggestions show amber
  and struck through: **✓ Accept** or **✖ Keep** each, or all at once.
- **⏸ 0.9s** marks a pause; **[… ]** marks speech the speech-to-text left out (usually an "um").
- **Jump cuts:** *punch-in zoom* zooms in a little on every other piece so cuts look like a camera change;
  *hard cut* just joins them.

**Playing** - click a word to hear it; **▶ Play** (or Space) plays the moment skipping the cuts.
The word being heard is highlighted and the text follows the timeline.
"""),
    ("🖼", "Pictures", """
# Pictures (context images)

When the streamer mentions a person, place, organisation or thing, RumbleCut suggests a picture of it for
that exact moment - like an editor adding B-roll. You approve every one; only approved pictures are exported.

1. Open **🖼 Pictures** beside the player and press **✨ Find pictures for this moment** (or for all ticked
   moments).
2. Pictures come from the sources you tick:
   - **Wikipedia / Wikimedia** - free to reuse with credit, no key needed.
   - **Pexels** stock photos - needs a free key.
   - **Web image search (Brave)** - widest choice, but most results are copyrighted; check before posting.
     Needs a key. Add keys in *Settings → Context images*.
3. For each suggestion: swap it for another in the strip or **🔍 Search again**, choose **Corner pop-up** or
   **Full-screen cutaway**, set when it appears and for how long, then **✓ Approve** or **✖ Remove**.
4. A **picture credits.txt** is saved next to exported clips, ready to paste into a description.
"""),
    ("💾", "③ Save as files", """
# ③ Export & publish → 💾 Save as files

- **Every clip has its own row:** a thumbnail, a file name you can change, its own **shape**, **quality**
  and **volume** knob (or "same as all" - the defaults on the right).
  **💾 Export** saves just that clip; **✏** opens it in the Clip studio.
- **Volume knobs** - the knob in *Defaults for all clips* sets how loud the exported sound is
  (100 % = as it is, up to 200 %). Each clip's knob shows "· all" while it follows the default;
  double-click it to go back to that. The player's volume never affects exports, so you can edit quietly
  and still export at 100 %.
- **Shapes:** Original, 16:9, **9:16 vertical** (crop or blurred background), 1:1. Resolution 480p to 4K,
  24 to 60 fps, quality, and an optional watermark like `@yourchannel`.
- Tick the clips you want and **Export ticked clips** - each as its own MP4 (with a thumbnail) and/or one
  **compilation** joined with transitions (cross-fade, wipes, slides, zoom, pixelate, blur…).
- A `clips.csv` lists each clip's times in the original stream and why it was picked.
- Uses the NVIDIA graphics card to encode when there is one (you can turn this off in Settings).
"""),
    ("🚀", "③ Post online", """
# ③ Export & publish → 🚀 Post online

Post moments to **YouTube, Instagram, TikTok and X**.

1. **Set up each platform once** in *Settings → Publishing accounts*: press **Setup steps**, create your own
   free developer app (the steps link to the right page), paste its keys and press **Connect**.
2. Pick a moment and tick the platforms. **✨ Write titles, captions & hashtags with AI** drafts a post per
   platform from what's said; edit anything.
3. **Shape:** short clips default to vertical 9:16 for TikTok, Instagram and YouTube Shorts.
4. **🚀 Post this moment** or **Post all ticked moments**, now or **scheduled** (YouTube schedules it itself;
   the others are posted by Windows at that time - RumbleCut can be closed, the PC must be on).
   Results and links appear under *Posted and scheduled*.

| Platform | Posts as | Notes |
|---|---|---|
| YouTube | Straight to your channel | Free; public, unlisted or private |
| Instagram | A Reel | Free; needs a Creator/Business account linked to a Facebook Page |
| TikTok | A draft in your TikTok inbox | Free; paste the caption (copied for you) and press Post in TikTok |
| X | A post with the video | About $0.015 per post; up to 2:20 |
"""),
    ("🤖", "AI models & speech", """
# AI models & speech-to-text (Settings → AI models)

**On this PC** (default, private and free) - install [Ollama](https://ollama.com) and pick a model. The list
shows the ones you have, then popular ones marked ⬇ with their size - pick one and RumbleCut downloads it for
you. LM Studio and other OpenAI-compatible servers work too.

**Cloud** - paste an Anthropic API key (stored encrypted). Choose Haiku 5.5 (default, fastest), Sonnet 5.5,
Opus 5.5 or Fable 5.1, plus any other model your key can use (⟳ reloads the list).

**Speech-to-text** always runs on your PC. Every Whisper model is listed, from `tiny` to `large-v3`;
`.en` ones are English only, `distil` and `large-v3-turbo` are fast. With an NVIDIA card, press
**⚡ Install GPU speed-up** (one-time ~550 MB) for roughly 10-20× faster writing-out - `large-v3-turbo`
is a good choice then.

**Test AI** checks the setup; the AI log says which model is being used.
"""),
    ("⚙", "Settings & updates", """
# Settings & updates

- **Folders**, GPU encoding and the speech model. Changes save automatically.
- **Moments you add** - how long a moment is when you right-click the timeline to add one (5 s by default).
- **Updates** - **RumbleCut app** downloads the newest version and relaunches; **Rumble downloader** is a
  one-click fix when Rumble changes its site (*Undo* goes back). RumbleCut checks quietly at start-up and the
  Settings tab shows ● when an update is ready. Your work carries over updates.
- **Relaunch RumbleCut** restarts the app.
- **Context images** - keys for Pexels and Brave image search.
- **Publishing accounts** - connect YouTube, Instagram, TikTok and X.
"""),
    ("⌨", "Keyboard shortcuts", """
# Keyboard shortcuts

| Keys | What it does |
|---|---|
| Space | Play / pause (plays the edit, skipping cuts, while the Script is open) |
| Home / End | Start / end of the selected moment |
| I / O | Set the moment's start / end at the playhead |
| Ctrl+Z | Undo (in the Script: your last cut first) |
| Ctrl+Shift+Z or Ctrl+Y | Redo |
| Delete | Script: cut the selected words · Downloads: delete the selected files |
| Enter | Script: bring the selected words back |
| Ctrl+O | Open a video from your PC |
| Ctrl+Shift+O / Ctrl+S | Open / save a project |
| F1 | This help |
| Ctrl+Q | Quit |
"""),
    ("💡", "Saving your work", """
# Saving your work

Everything is kept automatically - every 30 seconds, when you quit, and before an update:
the scan (timeline and the moments not shown yet), the Summary, the script, every moment with its edits,
cuts, pictures and post text. Next time RumbleCut opens it picks up where you left off.

You can also **☰ → File → Save project** (`.rcproj`) to keep a copy and open it later.

Only download and re-publish videos you own or have permission to use.
"""),
]
