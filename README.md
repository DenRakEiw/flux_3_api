# Flux 3 API — ComfyUI Nodes

ComfyUI node for the BFL Flux 3 video API.

**Single endpoint:** `POST https://api.bfl.ai/v1/flux-3-video`
([Docs](https://docs.bfl.ai/flux_3/flux3_video),
[API reference](https://docs.bfl.ai/api-reference/utility/generate-a-video-with-flux-3)).

## Setup

Add your key to `.env`:

```
BFL_API_KEY=bfl_...
```

The key is looked up in this order: node `api_key` field → `.env` → `BFL_API_KEY` environment variable.

Optional in `.env`:

```
BFL_BASE_URL=https://api.bfl.ai
```

(Default is `https://api.bfl.ai`; only change it if BFL announces a different host.)

## Node

**Flux 3 Video (API)** — outputs: `video` (mp4, with audio track) and `metadata`. Feed straight into `Save Video`.

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
