"""ComfyUI node for the BFL Flux 3 Video API (POST /v1/flux-3-video).

Single documented endpoint, four modes: t2v, i2v, v2v, draft_enhance.
Spec: https://docs.bfl.ai/api-reference/utility/generate-a-video-with-flux-3
"""

import asyncio
import io
import logging
from typing import Any

import torch

from .flux3_client import (
    ASPECT_RATIOS,
    DEFAULT_TIMEOUT_MINUTES,
    DURATION_MAX,
    DURATION_MIN,
    DURATIONS,
    MAX_KEYFRAMES,
    RESOLUTIONS,
    SAFETY_TOLERANCE_DEFAULT,
    SAFETY_TOLERANCE_MAX,
    SAFETY_TOLERANCE_MIN,
    VIDEO_MODES,
    Flux3Client,
    batch_to_base64,
    extract_url,
    format_metadata,
    tensor_to_base64,
    video_to_base64,
)

log = logging.getLogger("Flux3API")

from comfy_api.input_impl import VideoFromFile

CATEGORY = "Flux3 API"


async def _run(client: Flux3Client, payload: dict,
               timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES) -> tuple[dict, dict, bytes]:
    """Submit, poll and download without blocking the executor.

    Every step is either awaited or pushed onto a thread, so ComfyUI can run other
    Flux nodes in the same graph concurrently instead of one after another.
    """
    task = await asyncio.to_thread(client.submit, payload)
    result = await client.poll_async(task, timeout=timeout_minutes * 60)
    data = await asyncio.to_thread(client.download, extract_url(result))
    return task, result, data


def _parse_keyframe_times(text: str) -> list[float]:
    """'0, 4.5, 10' -> [0.0, 4.5, 10.0]. Accepts commas or semicolons."""
    try:
        return [float(x) for x in text.replace(";", ",").split(",") if x.strip()]
    except ValueError as exc:
        raise ValueError(
            f"Flux3: keyframe_times muss eine Liste von Sekunden sein (z.B. '0, 4.5, 10'), "
            f"bekommen: {text!r}"
        ) from exc


async def _build_keyframes(images: torch.Tensor | None, end_image: torch.Tensor | None,
                           keyframe_times: str, duration_value: str) -> Any:
    """Build the `keyframes` payload for i2v per the documented schema.

    - 1 image, no times: opening frame           -> str
    - 2 images (or images + end_image): start+end -> [str, str]
    - 3+ images, no times: evenly spread          -> [str, ...]  (duration muss gesetzt sein)
    - images + keyframe_times: storyboard         -> [[seconds, str], ...]
    """
    if images is None:
        raise ValueError("Flux3: i2v braucht einen images-Input.")

    # images + end_image ist die bequeme Art, Start+Ende zu stecken.
    if end_image is not None:
        imgs = torch.cat([images[:1], end_image[:1]])
    else:
        imgs = images

    n = len(imgs)
    if n < 1:
        raise ValueError("Flux3: i2v braucht mindestens ein Bild am images-Input.")
    if n > MAX_KEYFRAMES:
        raise ValueError(
            f"Flux3: i2v nimmt höchstens {MAX_KEYFRAMES} Keyframes, der Batch hat {n}."
        )

    times_text = keyframe_times.strip()
    if times_text:
        times = _parse_keyframe_times(times_text)
        if len(times) != n:
            raise ValueError(
                f"Flux3: keyframe_times braucht genauso viele Werte wie Bilder — "
                f"{n} Bild(er), aber {len(times)} Zeit(en)."
            )
        if any(times[i] > times[i + 1] for i in range(len(times) - 1)):
            raise ValueError(
                f"Flux3: keyframe_times müssen aufsteigend sein, bekommen: {times}"
            )
        if any(t < 0 for t in times):
            raise ValueError(f"Flux3: keyframe_times dürfen nicht negativ sein: {times}")
        encoded = await asyncio.to_thread(batch_to_base64, imgs)
        return [[t, b64] for t, b64 in zip(times, encoded)]

    if n >= 3 and duration_value == "auto":
        # Spec: "3 or more need a set duration."
        raise ValueError(
            "Flux3: 3+ Keyframes ohne keyframe_times brauchen eine feste duration "
            "(5–20). Stelle duration auf einen Wert, oder gib keyframe_times an."
        )

    encoded = await asyncio.to_thread(batch_to_base64, imgs)
    if n == 1:
        return encoded[0]
    return encoded  # list[str]: 2 -> start+end, 3+ -> evenly spread


class Flux3Video:
    """FLUX 3 Video — t2v / i2v / v2v / draft_enhance, mit synchronem Audio."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mode": (VIDEO_MODES, {
                    "default": "t2v",
                    "tooltip": "t2v: Text→Video · "
                               "i2v: Bild(er)→Video (keyframes) · "
                               "v2v: Clip fortsetzen (start_video) · "
                               "draft_enhance: einen vorherigen draft final rendern."}),
                "prompt": ("STRING", {
                    "multiline": True, "default": "",
                    "tooltip": "Pflicht für t2v/i2v/v2v. Bei draft_enhance nicht "
                               "erlaubt — der draft_cache pins alles."}),
                "aspect_ratio": (ASPECT_RATIOS, {
                    "default": "auto",
                    "tooltip": "'auto' läßt die API anhand von Prompt und Referenzen wählen."}),
                "duration": (DURATIONS, {
                    "default": "auto",
                    "tooltip": "Ganze Sekunden 5–20, oder 'auto'. 3+ Keyframes ohne "
                               "keyframe_times brauchen eine feste Länge."}),
                "resolution": (RESOLUTIONS, {
                    "default": "hd",
                    "tooltip": "hd = Default · fhd = höhere Auflösung (Video-Upscaler)."}),
                "generate_audio": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Default an. Aus = stummer Clip."}),
                "safety_tolerance": ("INT", {
                    "default": SAFETY_TOLERANCE_DEFAULT,
                    "min": SAFETY_TOLERANCE_MIN, "max": SAFETY_TOLERANCE_MAX,
                    "tooltip": "0 (strengste) bis 4. Default 2. Mit Conditioning-Media "
                               "maximal 2."}),
                "draft": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Schnelle hd-Vorschau. Das Ergebnis enthält einen "
                               "draft_cache, den man mit mode=draft_enhance final "
                               "rendern kann."}),
                "version": ("STRING", {
                    "default": "latest",
                    "tooltip": "Aktuell nur 'latest' freigegeben."}),
            },
            "optional": {
                "images": ("IMAGE", {
                    "tooltip": "Nur i2v: 1 Bild (Startframe) · 2 Bilder (Start+Ende) · "
                               "3–10 Bilder (gleichmäßig verteilt, braucht duration) · "
                               "oder zusammen mit keyframe_times (Storyboard)."}),
                "end_image": ("IMAGE", {
                    "tooltip": "Nur i2v: wenn gesetzt, gelten images=Start und "
                               "end_image=Ende (genau 2 Keyframes)."}),
                "keyframe_times": ("STRING", {
                    "default": "",
                    "tooltip": "Nur i2v: Sekunden je Bild, kommagetrennt (z.B. '0, 4.5, 10'). "
                               "Anzahl muss mit images übereinstimmen, Werte aufsteigend. "
                               "Bilder werden zu Frames an diesen Sekunden."}),
                "video": ("VIDEO", {
                    "tooltip": "Nur v2v: der Clip, der fortgesetzt wird (mp4)."}),
                "draft_cache": ("STRING", {
                    "default": "",
                    "tooltip": "Nur draft_enhance: base64-Bundle oder URL aus dem "
                               "draft-Output eines vorherigen Laufs. Pflicht für diesen Modus."}),
                "timeout_minutes": ("INT", {
                    "default": DEFAULT_TIMEOUT_MINUTES, "min": 1, "max": 240,
                    "tooltip": "Wie lange die Node auf das Ergebnis wartet. Bei voller "
                               "BFL-Warteschlange dauert ein Job schnell 20+ Minuten. "
                               "Läuft die Zeit ab, bricht nur die Node ab — der Job läuft "
                               "serverseitig weiter (und kostet trotzdem Credits)."}),
                "api_key": ("STRING", {"default": "", "tooltip": "Leer = aus .env"}),
            },
        }

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "metadata")
    FUNCTION = "generate"
    CATEGORY = CATEGORY

    async def generate(self, mode, prompt, aspect_ratio, duration, resolution,
                       generate_audio, safety_tolerance, draft, version,
                       images=None, end_image=None, keyframe_times="", video=None,
                       draft_cache="",
                       timeout_minutes=DEFAULT_TIMEOUT_MINUTES, api_key=""):
        if mode not in VIDEO_MODES:
            raise ValueError(
                f"Flux3: Modus '{mode}' nicht unterstützt. Erlaubt: {', '.join(VIDEO_MODES)}.")

        # --- draft_enhance: only mode, draft_cache, safety_tolerance are accepted.
        if mode == "draft_enhance":
            cache = draft_cache.strip()
            if not cache:
                raise ValueError(
                    "Flux3: draft_enhance braucht draft_cache (base64-Bundle oder URL "
                    "aus einem vorherigen draft-Lauf)."
                )
            payload = {
                "mode": "draft_enhance",
                "draft_cache": cache,
                "safety_tolerance": int(safety_tolerance),
            }
            # The bundle pins everything else; the API rejects any further field
            # (additionalProperties: false). Warn about inputs that would be silently dropped.
            ignored = [n for n, used in (
                ("prompt", bool(prompt.strip())),
                ("images", images is not None),
                ("end_image", end_image is not None),
                ("keyframe_times", bool(keyframe_times.strip())),
                ("video", video is not None),
                ("aspect_ratio", aspect_ratio != "auto"),
                ("duration", duration != "auto"),
                ("resolution", resolution != "hd"),
                ("generate_audio", not generate_audio),
                ("draft", draft),
                ("version", version.strip() not in ("", "latest")),
            ) if used]
            if ignored:
                log.warning("Flux3: draft_enhance akzeptiert nur draft_cache + safety_tolerance. "
                            "Diese Inputs werden ignoriert: %s", ", ".join(ignored))
        else:
            if not prompt.strip():
                raise ValueError("Flux3: prompt darf nicht leer sein.")

            payload: dict = {"mode": mode, "prompt": prompt.strip()}

            if aspect_ratio not in ASPECT_RATIOS:
                raise ValueError(
                    f"Flux3: aspect_ratio '{aspect_ratio}' nicht unterstützt. "
                    f"Erlaubt: {', '.join(ASPECT_RATIOS)}."
                )
            payload["aspect_ratio"] = aspect_ratio

            # duration: "auto" oder int 5..20
            if duration not in DURATIONS:
                raise ValueError(
                    f"Flux3: duration '{duration}' nicht unterstützt. "
                    f"Erlaubt: auto oder ganze Sekunden 5–20."
                )
            payload["duration"] = duration if duration == "auto" else int(duration)

            if resolution not in RESOLUTIONS:
                raise ValueError(
                    f"Flux3: resolution '{resolution}' nicht unterstützt. "
                    f"Erlaubt: {', '.join(RESOLUTIONS)}."
                )
            payload["resolution"] = resolution

            payload["generate_audio"] = bool(generate_audio)

            if not (SAFETY_TOLERANCE_MIN <= safety_tolerance <= SAFETY_TOLERANCE_MAX):
                raise ValueError(
                    f"Flux3: safety_tolerance muss {SAFETY_TOLERANCE_MIN}..{SAFETY_TOLERANCE_MAX} "
                    f"sein, bekommen: {safety_tolerance}."
                )
            payload["safety_tolerance"] = int(safety_tolerance)

            if draft:
                payload["draft"] = True

            v = version.strip()
            if v and v != "latest":
                # Spec enum currently only allows "latest"; keep the field ready for dated tags.
                payload["version"] = v

            # --- mode-specific conditioning input ---
            if mode == "i2v":
                payload["keyframes"] = await _build_keyframes(
                    images, end_image, keyframe_times, duration)

            elif mode == "v2v":
                if video is None:
                    raise ValueError("Flux3: v2v braucht einen video-Input (start_video).")
                payload["start_video"] = await asyncio.to_thread(video_to_base64, video)

            # t2v: no extra conditioning field.

        client = Flux3Client(api_key)
        task, result, data = await _run(client, payload, timeout_minutes)
        return (VideoFromFile(io.BytesIO(data)),
                format_metadata(payload, result, task))


NODE_CLASS_MAPPINGS = {
    "Flux3Video": Flux3Video,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "Flux3Video": "Flux 3 Video (API)",
}
