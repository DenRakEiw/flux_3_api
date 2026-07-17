"""HTTP client + media helpers for the BFL Flux 3 API.

The Flux 3 endpoints are not part of the public openapi.json. The schema below was
probed against the live API (Pydantic discriminated union on the `mode` field).
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from typing import Any

import numpy as np
import requests
import torch
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("Flux3API")

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE_URL = "https://api.isr.bfl.ai"

# Two different APIs live behind these names:
#   "modes"   - the mode-discriminated union on api.isr.bfl.ai (t2i/i2v/ir2v/...)
#   "preview" - a flat text-to-video endpoint on api.bfl.ai, extra="forbid",
#               with only prompt/aspect_ratio/duration/resolution/seed/webhook_url.
ENDPOINTS = {
    "flux-3-large": {"host": None, "path": "flux-3-large", "schema": "modes"},
    "flux-3-distilled": {"host": None, "path": "flux-3-distilled", "schema": "modes"},
    "flux-3-preview-high": {"host": "https://api.bfl.ai", "path": "flux-3-preview-high",
                            "schema": "preview"},
}

MODELS = ["flux-3-large", "flux-3-distilled"]          # image node: mode-based only
VIDEO_MODELS = ["flux-3-large", "flux-3-distilled", "flux-3-preview-high"]

# flux-3-preview-high only. duration is an int here (not "5s"), and there is no 2s option.
PREVIEW_ASPECT_RATIOS = ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "9:21"]
PREVIEW_DURATIONS = ["auto", "5", "10", "15", "20"]
PREVIEW_RESOLUTIONS = ["480p", "720p"]
PREVIEW_MAX_SEED = 4294967295
PREVIEW_FPS = 24
PREVIEW_MAX_REFERENCE_IMAGES = 10
PREVIEW_MAX_VIDEO_MB = 50
PREVIEW_MAX_VIDEO_SECONDS = 15
# Undocumented, found by probing: images below this are rejected with a 422.
PREVIEW_MIN_IMAGE_PX = 256

# preview-high has no `mode` field: the input field you fill decides the behaviour.
# The docs use the same short codes as the mode-based API, so one mode widget drives both.
PREVIEW_MODE_TO_FIELD = {
    "t2v": None,                    # nothing attached
    "i2v": "keyframes",             # one image at frame 0
    "ii2v": "keyframes",            # start + end frame, needs explicit duration
    "k2v": "keyframes",             # storyboard at chosen frame positions
    "ir2v": "reference_images",     # 1..10, subject only, never shown on screen
    "ve2v": "edit_video",           # re-render, keep motion/timing/framing
    "vr2v": "reference_video",      # new clip with the subjects from yours
    "f2v": "start_video",           # continue from the final frames
}
# 720p + edit_video/reference_video rejects 15/20s (docs: Constraints).
PREVIEW_LONG_DURATION_BLOCKED = ("ve2v", "vr2v")

IMAGE_ASPECT_RATIOS = [
    "1:2", "9:21", "9:16", "2:3", "3:4", "4:5", "5:7",
    "1:1",
    "7:5", "5:4", "4:3", "3:2", "16:9", "21:9", "2:1",
]
IMAGE_SIZES = ["256sq", "384sq", "512sq", "768sq", "1024sq", "2048sq", "4096sq"]

VIDEO_ASPECT_RATIOS = ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "9:21"]
VIDEO_DURATIONS = ["2s", "5s", "10s", "15s", "20s"]
VIDEO_RESOLUTIONS = ["192p", "352p", "480p", "720p"]

# Schema as probed on 2026-07-12. The API renamed/split several modes:
# i2v_ref -> ir2v, and v2v -> ve2v + vr2v. ii2v and k2v are new.
VIDEO_MODES = ["t2v", "i2v", "ii2v", "ir2v", "k2v", "f2v", "ve2v", "vr2v", "t2v_sdedit"]

# Which video modes take images, and how many.
MODES_ONE_IMAGE = ("i2v",)              # input_image: str
MODES_MANY_IMAGES = ("ir2v", "k2v")     # input_image: str | list[str]
MODES_START_END = ("ii2v",)             # start_image + end_image: str
MODES_VIDEO = ("f2v", "ve2v", "vr2v", "t2v_sdedit")

# conditioning_noise does not exist on t2v, ve2v, vr2v or t2v_sdedit.
MODES_CONDITIONING_NOISE = ("i2v", "ii2v", "ir2v", "k2v", "f2v")

# Queues on the BFL side can run well past 15 minutes; ComfyUI itself never times out.
DEFAULT_TIMEOUT_MINUTES = 45
DEFAULT_TIMEOUT = DEFAULT_TIMEOUT_MINUTES * 60

# Consecutive polling failures tolerated before giving up on an already-paid job.
MAX_POLL_ERRORS = 10

# Terminal states from StatusResponse
STATUS_READY = "Ready"
STATUS_FAILED = {"Task not found", "Request Moderated", "Content Moderated", "Error"}


def _load_dotenv() -> dict:
    values = {}
    path = os.path.join(NODE_DIR, ".env")
    if not os.path.isfile(path):
        return values
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip().strip('"').strip("'")
    except OSError as exc:
        log.warning("Flux3: could not read .env: %s", exc)
    return values


def get_api_key(override: str = "") -> str:
    if override and override.strip():
        return override.strip()
    env = _load_dotenv()
    key = env.get("BFL_API_KEY") or os.environ.get("BFL_API_KEY") or ""
    if not key:
        raise RuntimeError(
            "Kein BFL API Key gefunden. Trage ihn in Flux_3_API/.env als "
            "BFL_API_KEY=... ein, setze die Umgebungsvariable BFL_API_KEY, "
            "oder fülle das api_key-Feld der Node."
        )
    return key


def get_base_url() -> str:
    env = _load_dotenv()
    url = env.get("BFL_BASE_URL") or os.environ.get("BFL_BASE_URL") or DEFAULT_BASE_URL
    return url.rstrip("/")


# --------------------------------------------------------------------------- media

def tensor_to_base64(image: torch.Tensor, fmt: str = "PNG") -> str:
    """ComfyUI IMAGE tensor [H,W,C] in 0..1 -> base64 string (no data: prefix)."""
    arr = image.detach().cpu().numpy()
    arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    if arr.shape[-1] == 4:
        pil = Image.fromarray(arr, "RGBA").convert("RGB")
    else:
        pil = Image.fromarray(arr, "RGB")
    buf = io.BytesIO()
    pil.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def batch_to_base64(images: torch.Tensor, limit: int | None = None) -> list[str]:
    out = [tensor_to_base64(img) for img in images]
    return out[:limit] if limit else out


def video_to_base64(video) -> str:
    """ComfyUI VIDEO -> base64 mp4."""
    buf = io.BytesIO()
    video.save_to(buf)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def bytes_to_image_tensor(data: bytes) -> torch.Tensor:
    pil = Image.open(io.BytesIO(data))
    pil = pil.convert("RGB")
    arr = np.array(pil).astype(np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)


# --------------------------------------------------------------------------- api

class Flux3Client:
    def __init__(self, api_key: str = "", base_url: str = ""):
        self.api_key = get_api_key(api_key)
        self.base_url = (base_url.rstrip("/") if base_url else get_base_url())
        self.session = requests.Session()
        self.session.headers.update(
            {"x-key": self.api_key, "accept": "application/json"}
        )
        # requests retries nothing by default, so a single dropped keep-alive connection
        # would kill a job that already cost credits. Retry GETs only: replaying a POST
        # could submit the job (and charge) twice.
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def submit(self, model: str, payload: dict) -> dict:
        ep = ENDPOINTS.get(model, {"host": None, "path": model})
        # preview-high lives on its own host; the others follow BFL_BASE_URL from .env.
        host = ep["host"] or self.base_url
        url = f"{host}/v1/{ep['path']}"
        size_mb = len(json.dumps(payload).encode()) / (1024 * 1024)
        if size_mb > 1:
            log.info("Flux3: sende %.1f MB an %s (mode=%s)", size_mb, model, payload.get("mode"))

        resp = self.session.post(url, json=payload, timeout=300)

        if resp.status_code in (401, 403):
            raise RuntimeError(f"Flux3: API-Key ungültig oder fehlt (HTTP {resp.status_code}).")
        if not resp.ok:
            # raise_for_status() would swallow the body - and the body is the whole point:
            # it says WHY the API refused (bad base64, payload too large, moderation, ...).
            detail = resp.text[:1000] if resp.text else "(kein Fehlertext)"
            hint = ""
            if resp.status_code in (400, 413) and size_mb > 5:
                hint = (f" Der Request war {size_mb:.1f} MB groß — vermutlich ist das "
                        f"input_video/input_image zu groß. Kürzeren/kleineren Clip verwenden.")
            raise RuntimeError(
                f"Flux3: API lehnt den Request ab (HTTP {resp.status_code}) "
                f"für mode={payload.get('mode')}: {detail}{hint}"
            )

        data = resp.json()
        cost = data.get("cost")
        log.info("Flux3: task %s submitted (cost: %s credits)", data.get("id"), cost)
        return data

    def poll(self, task: dict, timeout: float = 900.0, interval: float = 2.0) -> Any:
        polling_url = task.get("polling_url") or f"{self.base_url}/v1/get_result"
        task_id = task["id"]
        deadline = time.monotonic() + timeout
        last_status = None

        while True:
            resp = self.session.get(polling_url, params={"id": task_id}, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")

            if status != last_status:
                log.info("Flux3: task %s -> %s", task_id, status)
                last_status = status

            if status == STATUS_READY:
                result = data.get("result")
                if not result:
                    raise RuntimeError(f"Flux3: Task fertig, aber ohne Ergebnis: {data}")
                return result
            if status in STATUS_FAILED:
                raise RuntimeError(
                    f"Flux3: Task fehlgeschlagen ({status}): {data.get('details')}"
                )
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"Flux3: Timeout nach {timeout:.0f}s (letzter Status: {status})"
                )
            time.sleep(interval)

    async def poll_async(self, task: dict, timeout: float = DEFAULT_TIMEOUT,
                         interval: float = 2.0) -> Any:
        """Same as poll(), but yields the event loop so other nodes keep running.

        ComfyUI runs coroutine FUNCTIONs concurrently: a node that awaits is parked as
        PENDING while independent nodes execute. A blocking time.sleep() here would
        stall the whole graph, so every wait and every HTTP call has to yield.
        """
        polling_url = task.get("polling_url") or f"{self.base_url}/v1/get_result"
        task_id = task["id"]
        started = time.monotonic()
        deadline = started + timeout
        last_status = None
        last_log = 0.0
        net_errors = 0

        while True:
            def _fetch():
                resp = self.session.get(polling_url, params={"id": task_id}, timeout=60)
                resp.raise_for_status()
                return resp.json()

            try:
                data = await asyncio.to_thread(_fetch)
            except (requests.RequestException, ValueError) as exc:
                # The job is already running and paid for — a network hiccup must not
                # throw it away. Keep polling until the deadline.
                net_errors += 1
                if net_errors > MAX_POLL_ERRORS:
                    raise RuntimeError(
                        f"Flux3: {net_errors} Netzwerkfehler in Folge beim Pollen von "
                        f"{task_id}. Der Job läuft serverseitig weiter — Ergebnis später "
                        f"abrufbar unter {polling_url} . Letzter Fehler: {exc}"
                    ) from exc
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"Flux3: Timeout beim Pollen von {task_id} nach Netzwerkfehler: {exc}"
                    ) from exc
                log.warning("Flux3: Netzwerkfehler beim Pollen (%d/%d), neuer Versuch in %.0fs: %s",
                            net_errors, MAX_POLL_ERRORS, interval * 2, exc)
                await asyncio.sleep(interval * 2)
                continue

            net_errors = 0
            status = data.get("status")
            waited = time.monotonic() - started

            if status != last_status:
                log.info("Flux3: task %s -> %s", task_id, status)
                last_status = status
            elif waited - last_log >= 60:  # long queue: show it's alive, not hung
                progress = data.get("progress")
                log.info("Flux3: task %s wartet seit %.0f min (%s%s)",
                         task_id, waited / 60, status,
                         f", {progress:.0%}" if isinstance(progress, (int, float)) else "")
                last_log = waited

            if status == STATUS_READY:
                result = data.get("result")
                if not result:
                    raise RuntimeError(f"Flux3: Task fertig, aber ohne Ergebnis: {data}")
                return result
            if status in STATUS_FAILED:
                raise RuntimeError(
                    f"Flux3: Task fehlgeschlagen ({status}): {data.get('details')}"
                )
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"Flux3: Timeout nach {timeout / 60:.0f} min (letzter Status: {status}). "
                    f"Der Job läuft serverseitig weiter — Ergebnis abrufbar unter "
                    f"{polling_url} . Für lange Warteschlangen timeout_minutes erhöhen."
                )

            # Back off on long waits: no point hammering get_result every 2s for 20 minutes.
            await asyncio.sleep(min(interval * 5, interval + waited / 60))

    def download(self, url: str) -> bytes:
        """Fetch the finished asset. Retried: the result URL is signed and expires,
        so a dropped connection here would lose the video for good."""
        last: Exception | None = None
        for attempt in range(4):
            try:
                # Plain requests.get() - the signed delivery URL must not carry our api key.
                resp = requests.get(url, timeout=300)
                resp.raise_for_status()
                return resp.content
            except requests.RequestException as exc:
                last = exc
                wait = 2 ** attempt
                log.warning("Flux3: Download fehlgeschlagen (Versuch %d/4), neuer Versuch in %ds: %s",
                            attempt + 1, wait, exc)
                time.sleep(wait)
        raise RuntimeError(f"Flux3: Download des Ergebnisses fehlgeschlagen: {last}") from last


DISCORD_LIMIT = 2000  # Discord rejects any message over this with HTTP 400.

# Payload keys whose values are base64 blobs - never dump those into the debug output.
BLOB_KEYS = ("input_image", "input_video", "input_image_blob_path",
             "start_image", "end_image",
             # preview-high field names
             "keyframes", "reference_images", "edit_video", "reference_video",
             "start_video", "reference_audio")


def _describe_blob(value: Any) -> str:
    """Render a base64 input as a size, not as megabytes of gibberish."""
    if isinstance(value, list):
        return "[" + ", ".join(_describe_blob(v) for v in value) + "]"
    if isinstance(value, dict):
        # keyframes: {"image_url": <base64>, "frame_index": 0}
        inner = ", ".join(f"{k}: {_describe_blob(v)}" for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, str):
        if value.startswith("http"):
            return value
        kb = len(value) * 3 / 4 / 1024  # base64 -> bytes
        return f"<base64, {kb:.0f} KB>"
    return repr(value)


def format_metadata(model: str, payload: dict, result: Any, task: dict) -> str:
    """Full, untruncated dump of everything about a run - for a Show Any node.

    Not sized for Discord: the webhook path sends the bare task id instead.
    """
    result = result if isinstance(result, dict) else {}
    ep = ENDPOINTS.get(model, {})
    host = ep.get("host") or get_base_url()
    lines = [
        "=== FLUX 3 ===",
        f"endpoint       : POST {host}/v1/{ep.get('path', model)}",
        f"schema         : {ep.get('schema', '?')}",
        f"task_id        : {task.get('id', '?')}",
        f"polling_url    : {task.get('polling_url', '?')}",
    ]
    for key in ("cost", "input_mp", "output_mp"):
        if task.get(key) is not None:
            lines.append(f"{key:<15}: {task[key]}")

    lines.append("")
    lines.append("--- REQUEST (an die API gesendet) ---")
    for key, value in payload.items():
        shown = _describe_blob(value) if key in BLOB_KEYS else value
        lines.append(f"{key:<15}: {shown}")

    lines.append("")
    lines.append("--- RESPONSE (von der API) ---")
    for key, value in result.items():
        lines.append(f"{key:<15}: {value}")

    seed = result.get("seed", payload.get("seed"))
    if seed is not None:
        lines.append("")
        lines.append(f"seed (genutzt) : {seed}")

    return "\n".join(lines)


DISCORD_WEBHOOK_RE = re.compile(
    r"^https://(?:\w+\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+$",
    re.IGNORECASE,
)


def get_webhook_url(override: str = "") -> str:
    if override and override.strip():
        return override.strip()
    env = _load_dotenv()
    return (env.get("DISCORD_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()


def redact_webhook(url: str) -> str:
    """Never log the token part of a webhook URL."""
    match = re.match(r"(https://[^/]+/api/(?:v\d+/)?webhooks/\d+/)(.+)", url or "")
    return f"{match.group(1)}[REDACTED]" if match else "[REDACTED_WEBHOOK_URL]"


def split_for_discord(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split text into Discord-sized chunks, breaking on paragraphs, then lines, then words."""
    chunks: list[str] = []
    remaining = text.strip()

    while len(remaining) > limit:
        window = remaining[:limit]
        # Prefer the latest clean break inside the window.
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit  # one giant unbroken token: hard cut
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


def post_discord_message(webhook_url: str, text: str) -> int:
    """Post text to a Discord webhook, split across as many messages as needed.

    Returns the number of messages sent. Never raises: a failed notification must not
    kill a generation that already cost credits.
    """
    if not webhook_url:
        return 0
    if not DISCORD_WEBHOOK_RE.match(webhook_url):
        log.warning("Flux3: %s sieht nicht wie eine Discord-Webhook-URL aus - nicht gesendet.",
                    redact_webhook(webhook_url))
        return 0

    chunks = split_for_discord(text)
    sent = 0
    for index, chunk in enumerate(chunks, start=1):
        body = chunk
        if len(chunks) > 1:
            suffix = f"\n-# ({index}/{len(chunks)})"
            body = chunk[: DISCORD_LIMIT - len(suffix)] + suffix

        for attempt in range(4):
            try:
                resp = requests.post(webhook_url, json={"content": body}, timeout=30)
            except requests.RequestException as exc:
                log.warning("Flux3: Discord-Webhook nicht erreichbar: %s", exc)
                break

            if resp.status_code == 429:  # rate limited
                wait = float(resp.json().get("retry_after", 1))
                log.info("Flux3: Discord rate limit, warte %.1fs", wait)
                time.sleep(wait)
                continue
            if resp.status_code in (200, 204):
                sent += 1
                break

            # Unlike ComfyUI-DiscordSend, surface what Discord actually complained about.
            log.warning("Flux3: Discord lehnt Nachricht %d/%d ab (HTTP %d): %s",
                        index, len(chunks), resp.status_code, resp.text[:300])
            break

    if sent:
        log.info("Flux3: %d Discord-Nachricht(en) gesendet an %s",
                 sent, redact_webhook(webhook_url))
    return sent


def extract_url(result: Any) -> str:
    """The result payload puts the asset under a url-ish key; be liberal about which."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("sample", "video", "url", "image", "output"):
            val = result.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
        for val in result.values():
            if isinstance(val, str) and val.startswith("http"):
                return val
    raise RuntimeError(f"Flux3: keine Ergebnis-URL gefunden in: {result}")
