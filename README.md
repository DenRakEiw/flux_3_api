# Flux 3 API — ComfyUI Nodes

ComfyUI nodes for the BFL Flux 3 video API, plus an LLM-powered prompt generator.

## Nodes

- **Flux 3 Video (API)** — generates video via `POST https://api.bfl.ai/v1/flux-3-video`
  ([Docs](https://docs.bfl.ai/flux_3/flux3_video),
  [API reference](https://docs.bfl.ai/api-reference/utility/generate-a-video-with-flux-3)).
- **Flux 3 Video Upscale (API)** — super-resolution for short clips (≤20s, ≤50MB)
  via `POST https://api.bfl.ai/v1/flux-tools/video-upscale-v1`
  ([Docs](https://docs.bfl.ai/flux_3/video_upscale),
  [API reference](https://docs.bfl.ai/api-reference/utility/video-upscale-v1)).
  Precise mode preserves identity; creative mode enhances detail. Output capped
  at ~14.4 MP per frame; source audio preserved.
- **Flux 3 Openrouter Prompt** — turns a vague idea into a structured FLUX 3 prompt
  using an OpenRouter LLM, guided by a prompting skill. Output feeds straight into
  the Video node's `prompt` input.

## Setup

Add your keys to `.env`:

```
BFL_API_KEY=bfl_...
OPENROUTER_API_KEY=sk-or-...
```

Get an OpenRouter key at https://openrouter.ai/keys. Keys are looked up in this
order: node `api_key` field → `.env` → environment variable.

Optional in `.env`:

```
BFL_BASE_URL=https://api.bfl.ai
```

(Default is `https://api.bfl.ai`; only change it if BFL announces a different host.)

---

## Flux 3 Video (API)

Outputs: `video` (mp4, with audio track) and `metadata`. Feed straight into `Save Video`.

### Modes (`mode`)

One mode per request; the rest of the request stays the same.

| mode | requires | does |
|---|---|---|
| `t2v` | – | Text → Video |
| `i2v` | `images` (1–10) | Image(s) → Video (keyframes) |
| `v2v` | `video` | Continue a clip (start_video) |
| `draft_enhance` | `draft_cache` | Final-render a previous `draft` run |

### Parameters

| Field | Required | Values |
|---|---|---|
| `mode` | always | `t2v` `i2v` `v2v` `draft_enhance` |
| `prompt` | t2v/i2v/v2v | free-form |
| `keyframes` | i2v | 1 image (opening frame) · 2 images (start+end) · 3–10 images (evenly spread, needs `duration`) · or `[seconds, image]` pairs (storyboard). URL or base64. |
| `start_video` | v2v | mp4 URL or base64 |
| `draft_cache` | draft_enhance | base64 bundle or URL from the `draft` output of a previous run |
| `aspect_ratio` | – | `auto` (default), `21:9`, `2:1`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16` |
| `duration` | – | whole seconds 5–20 or `auto` (default) |
| `resolution` | – | `hd` (default) or `fhd` |
| `generate_audio` | – | bool, default `true` |
| `safety_tolerance` | – | 0 (strictest) to 4, default 2. Capped at 2 when conditioning media is present. |
| `draft` | – | bool. `true` = fast hd preview; result includes a `draft_cache`. |
| `version` | – | `latest` (default) |

`draft_enhance` accepts **only** `mode`, `draft_cache` and `safety_tolerance` — the bundle pins
mode, prompt, seed and conditioning. All other inputs are ignored (with a console warning).

### i2v keyframes (ComfyUI UX)

The node translates ComfyUI inputs into the documented `keyframes` schema:

| what's connected | keyframe_times | sent |
|---|---|---|
| 1 image | – | `"<base64>"` (opening frame) |
| 2 images *or* `images`+`end_image` | – | `["<a>", "<b>"]` (start+end) |
| 3–10 images | – | `["<a>", ...]` (evenly spread — `duration` must be set) |
| n images | n seconds | `[[t1, "<a>"], ...]` (storyboard — values ascending) |

### metadata output (debug)

The `STRING` output contains everything about the run — meant for a *Show Any* node for debugging. Untruncated:

```
=== FLUX 3 VIDEO ===
endpoint       : POST https://api.bfl.ai/v1/flux-3-video
task_id        : e696c5eb-e97a-484f-9976-d7a5722658b2
polling_url    : https://api.bfl.ai/v1/get_result?id=e696c5eb-…

--- REQUEST (sent to the API) ---
mode           : i2v
prompt         : a seed grows into a tree through the seasons
duration       : 10
resolution     : hd
generate_audio : True
keyframes      : [[0, <base64, 412 KB>], [10, <base64, 398 KB>]]

--- RESPONSE (from the API) ---
sample         : https://delivery.bfl.ai/results/…
```

Base64 images/videos/bundles are rendered as a size rather than a megabyte-long string.

---

## Flux 3 Video Upscale (API)

Super-resolution for short clips via `POST /v1/flux-tools/video-upscale-v1`.
Outputs: `video` (mp4, source audio preserved) and `metadata`. Feed straight into
`Save Video`.

### Limits

- Source max **20 s** and **50 MB**. Longer sources are rejected before
  processing (not truncated, no charge).
- Output max **~14.4 MP per frame** (4K and beyond). Very large sources get
  upscaled by less than the requested `upscale_factor`.
- The **source audio track** is preserved in the output.

### Parameters

| Field | Required | Values |
|---|---|---|
| `video` | yes (or `input_video_url`) | ComfyUI VIDEO input (mp4) |
| `input_video_url` | – | HTTP(S) URL to the source; takes priority over `video`, saves base64-encoding a large clip |
| `upscale_factor` | – | `1.5`–`3.0`, default `2.0`. Preserves the source aspect ratio |
| `creativity` | – | `precise` (0) or `creative` (1, default) — see modes below |
| `prompt` | – | Optional description steering the enhanced detail (mostly in creative mode) |
| `safety_tolerance` | – | 0 (strictest) to 4, default 2. Moderates the prompt and delivered frames |
| `timeout_minutes` | – | default 45, up to 240 |
| `api_key` | – | empty = from `.env` |

### Modes (`creativity`)

| Mode | Value | Behaviour |
|---|---|---|
| `precise` | 0 | preserves the source exactly and sharpens it. For faces, products, brand assets, real people. |
| `creative` | 1 (default) | restores/invents fine detail more aggressively. For generated footage, textures, crowds, scenery. Identity (faces/products) can drift. |

### Pricing

Per **megapixel-second** of delivered output (megapixels per output frame × output
duration in seconds). You're charged for delivered output only — rejected clips
cost nothing.

| Mode | Price |
|---|---|
| precise (`creativity: 0`) | $0.075 / MP·s |
| creative (`creativity: 1`) | $0.105 / MP·s |

Roughly per second of output: 1080p $0.15 (precise) / $0.21 (creative), 2K $0.26 / $0.37, 4K $0.59 / $0.83.

### Tips

- Start from the least compressed source material — compression artifacts limit
  recoverable detail.
- `creative` for generated footage/landscapes/textures; `precise` when
  faces/products/brand assets must stay exact.
- Run upscaling as your **final step**, after editing/trimming, so you only pay
  for footage you keep.
- The signed delivery URL expires ~1 h after Ready — download the video in time.

## Queue / Timeout

The BFL queue can run well past 15 minutes for videos. ComfyUI itself has **no**
execution timeout — it waits indefinitely. Only `timeout_minutes` caps it
(default **45**, adjustable up to 240).

If the time runs out, **only the node** aborts; the job keeps running at BFL and still costs
credits. The error message therefore includes the `polling_url`, so the result can be fetched
afterwards.

While waiting, the console logs status every minute so a long run doesn't look hung. The poll
interval grows with wait time (2 s up to max 10 s) — over 25 minutes that's 201 instead of 750
requests.

**Network hiccups:** polling survives them. `requests` does no retries by default, so a single
dropped keep-alive connection (`RemoteDisconnected`) would have killed an already-paid job.
Now: up to 5 automatic retries per request (with backoff), and beyond that up to 10 consecutive
failures before giving up. Only GETs are retried — a repeated POST could submit the job twice
and double-charge. The finished-asset download is also retried up to 4×, since the signed
result URL expires.

## Parallel runs

The node is **async** (`async def generate`). ComfyUI recognises coroutine `FUNCTION`s, parks a
waiting node as `PENDING` and runs other nodes meanwhile. Multiple Flux nodes in one graph
therefore generate **simultaneously**, not one after another — the wait time is that of the
slowest clip, not the sum of all.

For this to hold, nothing may block the event loop: polling uses `await asyncio.sleep()`, and
all blocking parts (HTTP requests, base64 encoding of images/videos) run via
`asyncio.to_thread()`. A single `time.sleep()` in the wrong place would re-serialise the whole
graph.

---

## Flux 3 Openrouter Prompt

Turns a plain-words idea into a complete, structured FLUX 3 prompt using an
[OpenRouter](https://openrouter.ai) LLM. No variants, no metadata — just the
prompt, ready to paste into the Video node.

| Input | What it does |
|---|---|
| `idea` | Describe your video in plain words. |
| `model` | OpenRouter model slug. The dropdown is filled **live** from the OpenRouter API; refresh ComfyUI to pick up new models. |
| `skill` | Prompting skill that steers the LLM (see below). |
| `images` | Optional: reference image(s) for vision-capable models (GPT-4o, Claude Sonnet, Gemini, …). |
| `model_custom` | Optional: any OpenRouter slug not in the dropdown. |
| `api_key` | Optional override; empty = from `.env` (`OPENROUTER_API_KEY`). |
| `extra_instructions` | Optional directives appended to the skill. |
| `temperature` | LLM sampling temperature (0–2). |
| `max_tokens` | Max output tokens. |
| `timeout_seconds` | How long to wait for the LLM. |

**Output:** `prompt` (STRING) — feed it into the Flux 3 Video node's `prompt` input.

### Skills

Skills are `.md` files in the `skills/` folder. Two are pre-installed:

- **Flux3Director** — full structured prompting skill for FLUX 3 video (all modes,
  multi-segment timing, camera vocabulary, tag system).
- **Flux3Director4Discord** — the same skill compressed for the FLUX3 Discord bot's
  2,000-character hard limit.

**Add your own:** drop any `.md` file into the `skills/` folder and refresh
ComfyUI — it shows up in the `skill` dropdown automatically. Set `skill` to
`none` for freeform prompting without a skill.
