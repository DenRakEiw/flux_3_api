"""ComfyUI nodes for the BFL Flux 3 API (flux-3-large / flux-3-distilled)."""

import asyncio
import io
import logging

import torch

from .flux3_client import (
    DEFAULT_TIMEOUT_MINUTES,
    ENDPOINTS,
    IMAGE_ASPECT_RATIOS,
    IMAGE_SIZES,
    MODELS,
    PREVIEW_ASPECT_RATIOS,
    PREVIEW_DURATIONS,
    PREVIEW_FPS,
    PREVIEW_LONG_DURATION_BLOCKED,
    PREVIEW_MAX_REFERENCE_IMAGES,
    PREVIEW_MAX_SEED,
    PREVIEW_MAX_VIDEO_SECONDS,
    PREVIEW_MIN_IMAGE_PX,
    PREVIEW_MODE_TO_FIELD,
    PREVIEW_RESOLUTIONS,
    VIDEO_MODELS,
    MODES_CONDITIONING_NOISE,
    MODES_MANY_IMAGES,
    MODES_ONE_IMAGE,
    MODES_START_END,
    MODES_VIDEO,
    VIDEO_ASPECT_RATIOS,
    VIDEO_DURATIONS,
    VIDEO_MODES,
    VIDEO_RESOLUTIONS,
    Flux3Client,
    batch_to_base64,
    bytes_to_image_tensor,
    extract_url,
    format_metadata,
    get_webhook_url,
    post_discord_message,
    tensor_to_base64,
    video_to_base64,
)

log = logging.getLogger("Flux3API")

from comfy_api.input_impl import VideoFromFile

CATEGORY = "Flux3 API"


def _common_inputs() -> dict:
    return {
        "model": (MODELS, {"default": "flux-3-large",
                           "tooltip": "large = volle Qualität, distilled = schnell"}),
        "prompt": ("STRING", {"multiline": True, "default": ""}),
        "negative_prompt": ("STRING", {"multiline": True, "default": ""}),
        # Capped at 2^32-1, not 2^64-1: flux-3-preview-high rejects anything above that,
        # and ComfyUI's "randomize" would otherwise produce a failing seed nearly every run.
        # large/distilled accept any integer, so this range is safe for all endpoints.
        "seed": ("INT", {"default": 0, "min": 0, "max": PREVIEW_MAX_SEED,
                         "tooltip": "0 = zufällig (Feld wird nicht gesendet). Max 4294967295 — "
                                    "flux-3-preview-high erlaubt nicht mehr."}),
        "steps": ("INT", {"default": 0, "min": 0, "max": 50,
                          "tooltip": "1-50. 0 = API-Default verwenden"}),
        "guidance": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 100.0, "step": 0.1,
                               "tooltip": "0 = API-Default verwenden"}),
        "prompt_upsampling": ("BOOLEAN", {"default": False}),
    }


async def _notify_discord(webhook_override: str, task: dict) -> None:
    """Post the bare task id to Discord; a bot-side script turns it into the video."""
    webhook = get_webhook_url(webhook_override)
    if not webhook:
        return
    task_id = task.get("id")
    if task_id:
        await asyncio.to_thread(post_discord_message, webhook, task_id)


async def _run(client: Flux3Client, model: str, payload: dict,
               timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES) -> tuple[dict, dict, bytes]:
    """Submit, poll and download without blocking the executor.

    Every step is either awaited or pushed onto a thread, so ComfyUI can run other
    Flux nodes in the same graph concurrently instead of one after another.
    """
    task = await asyncio.to_thread(client.submit, model, payload)
    result = await client.poll_async(task, timeout=timeout_minutes * 60)
    data = await asyncio.to_thread(client.download, extract_url(result))
    return task, result, data


def _build_preview_payload(mode: str, prompt: str, seed: int, aspect_ratio: str,
                           duration: str, video_resolution: str, generate_audio: bool,
                           grounding: bool, version: str, ignored: dict) -> dict:
    """Payload for flux-3-preview-high (the documented early-access API).

    There is no `mode` field here: whichever input field is filled decides the
    behaviour. The mode widget picks that field, so the node's UI stays the same
    across both endpoints. Constraints from the docs are enforced before sending.
    """
    if mode not in PREVIEW_MODE_TO_FIELD:
        raise ValueError(
            f"Flux3: flux-3-preview-high kennt den Modus '{mode}' nicht. "
            f"Erlaubt: {', '.join(PREVIEW_MODE_TO_FIELD)}."
        )

    payload = {"prompt": prompt}

    if aspect_ratio not in PREVIEW_ASPECT_RATIOS:
        raise ValueError(
            f"Flux3: flux-3-preview-high kennt das Seitenverhältnis '{aspect_ratio}' nicht. "
            f"Erlaubt: {', '.join(PREVIEW_ASPECT_RATIOS)}."
        )
    payload["aspect_ratio"] = aspect_ratio

    # "5s" -> 5; this endpoint wants ints and has no 2s option.
    dur = duration.rstrip("s")
    if dur not in PREVIEW_DURATIONS:
        raise ValueError(
            f"Flux3: flux-3-preview-high unterstützt die Länge '{duration}' nicht. "
            f"Erlaubt: auto, 5s, 10s, 15s, 20s (kein 2s)."
        )
    payload["duration"] = dur if dur == "auto" else int(dur)

    if video_resolution not in PREVIEW_RESOLUTIONS:
        raise ValueError(
            f"Flux3: flux-3-preview-high unterstützt die Auflösung '{video_resolution}' nicht. "
            f"Erlaubt: {', '.join(PREVIEW_RESOLUTIONS)} (kein 192p/352p)."
        )
    payload["resolution"] = video_resolution   # note: not "video_resolution" here

    # Documented constraint: 720p + edit_video/reference_video rejects 15/20s.
    if (mode in PREVIEW_LONG_DURATION_BLOCKED and video_resolution == "720p"
            and dur in ("15", "20")):
        raise ValueError(
            f"Flux3: '{mode}' erlaubt bei 720p keine {dur}s — nimm 5s/10s oder 480p."
        )

    if seed:
        if seed > PREVIEW_MAX_SEED:
            raise ValueError(
                f"Flux3: flux-3-preview-high erlaubt seed nur bis {PREVIEW_MAX_SEED}, "
                f"bekommen: {seed}."
            )
        payload["seed"] = int(seed)

    if not generate_audio:
        payload["generate_audio"] = False     # default is true
    if not grounding:
        payload["grounding"] = False          # default is true
    if version.strip() and version.strip() != "latest":
        payload["version"] = version.strip()

    used = [k for k, v in ignored.items() if v]
    if used:
        log.warning("Flux3: flux-3-preview-high kennt diese Einstellungen nicht, sie werden "
                    "ignoriert: %s", ", ".join(used))
    return payload


def _check_preview_images(images, field: str) -> None:
    """Undocumented API rule: images must be at least 256x256, else a cryptic 422."""
    h, w = int(images.shape[1]), int(images.shape[2])
    if h < PREVIEW_MIN_IMAGE_PX or w < PREVIEW_MIN_IMAGE_PX:
        raise ValueError(
            f"Flux3: {field} braucht Bilder von mindestens "
            f"{PREVIEW_MIN_IMAGE_PX}x{PREVIEW_MIN_IMAGE_PX} px — deins ist {w}x{h}."
        )


def _check_preview_video(video) -> None:
    """Docs: video inputs must be mp4, <= 50 MB and <= 15 s. Catch it before the upload."""
    try:
        seconds = video.get_duration()
    except Exception:      # pragma: no cover - exotic VIDEO impls
        return
    if seconds > PREVIEW_MAX_VIDEO_SECONDS + 0.5:
        raise ValueError(
            f"Flux3: flux-3-preview-high nimmt Videos bis "
            f"{PREVIEW_MAX_VIDEO_SECONDS}s — dein Clip ist {seconds:.1f}s lang."
        )


def _preview_keyframes(mode: str, encoded: list[str], duration_value,
                       keyframe_indices: str) -> list[dict]:
    """Build the keyframes list: [{image_url, frame_index}, ...] (docs' field names)."""
    if mode == "i2v":
        if len(encoded) > 1:
            log.warning("Flux3: i2v nutzt 1 Bild als Startframe — der Batch hat %d. "
                        "Für mehrere Bilder: k2v (Storyboard) oder ir2v (Referenzen).",
                        len(encoded))
        return [{"image_url": encoded[0], "frame_index": 0}]

    if mode == "ii2v":
        # Docs: a two-image morph needs a whole-number duration, closing frame at duration*24.
        if duration_value == "auto":
            raise ValueError(
                "Flux3: ii2v (Start→Endbild) braucht eine feste Länge — 'auto' geht nicht. "
                "Stelle duration auf 5s/10s/15s/20s."
            )
        last = int(duration_value) * PREVIEW_FPS
        return [{"image_url": encoded[0], "frame_index": 0},
                {"image_url": encoded[1], "frame_index": last}]

    # k2v: storyboard at explicit positions
    try:
        indices = [int(x) for x in keyframe_indices.replace(";", ",").split(",") if x.strip()]
    except ValueError as exc:
        raise ValueError(
            f"Flux3: keyframe_indices muss eine Liste ganzer Zahlen sein (z.B. '0, 24, 48'), "
            f"bekommen: {keyframe_indices!r}"
        ) from exc
    if len(indices) != len(encoded):
        raise ValueError(
            f"Flux3: k2v braucht genau so viele keyframe_indices wie Bilder — "
            f"{len(encoded)} Bild(er), aber {len(indices)} Index/Indizes."
        )
    if len(set(indices)) != len(indices):
        raise ValueError(f"Flux3: keyframe_indices müssen eindeutig sein, bekommen: {indices}")
    if duration_value != "auto":
        limit = int(duration_value) * PREVIEW_FPS
        too_big = [i for i in indices if i > limit]
        if too_big:
            raise ValueError(
                f"Flux3: keyframe_indices dürfen höchstens duration×{PREVIEW_FPS} = {limit} "
                f"sein (bei {duration_value}s), zu groß: {too_big}"
            )
    return [{"image_url": img, "frame_index": idx} for img, idx in zip(encoded, indices)]


def _build_payload(mode: str, prompt: str, negative_prompt: str, seed: int,
                   steps: int, guidance: float, prompt_upsampling: bool) -> dict:
    payload = {"mode": mode, "prompt": prompt}
    if negative_prompt.strip():
        payload["negative_prompt"] = negative_prompt.strip()
    if seed:
        payload["seed"] = int(seed)
    if steps:
        payload["steps"] = int(steps)
    if guidance:
        payload["guidance"] = float(guidance)
    if prompt_upsampling:
        payload["prompt_upsampling"] = True
    return payload


class Flux3Image:
    """Text-to-image (t2i) and image-to-image / edit (i2i)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                **_common_inputs(),
                "aspect_ratio": (IMAGE_ASPECT_RATIOS, {"default": "16:9"}),
                "image_size": (IMAGE_SIZES, {"default": "1024sq",
                                             "tooltip": "Größe als Quadrat-Äquivalent; "
                                                        "aspect_ratio bestimmt die Form"}),
            },
            "optional": {
                "images": ("IMAGE", {"tooltip": "Angeschlossen = i2i (Edit/Remix). "
                                                "Bis zu 4 Bilder aus einem Batch."}),
                "alpha": ("FLOAT", {
                    "default": -1.0, "min": -1.0, "max": 100.0, "step": 0.01,
                    "tooltip": "Undokumentierter Parameter der API (float). "
                               "-1 = nicht senden / API-Default."}),
                "timeout_minutes": ("INT", {
                    "default": DEFAULT_TIMEOUT_MINUTES, "min": 1, "max": 240,
                    "tooltip": "Wie lange die Node auf das Ergebnis wartet. Bei voller "
                               "BFL-Warteschlange dauert ein Job schnell 20+ Minuten. "
                               "Läuft die Zeit ab, bricht nur die Node ab — der Job läuft "
                               "serverseitig weiter (und kostet trotzdem Credits)."}),
                "discord_webhook_url": ("STRING", {
                    "default": "",
                    "tooltip": "Wenn gesetzt, postet diese Node nach der Generierung die nackte "
                               "Task-ID in den Discord-Kanal (nichts sonst). Leer = aus .env "
                               "(DISCORD_WEBHOOK_URL), sonst kein Versand."}),
                "api_key": ("STRING", {"default": "", "tooltip": "Leer = aus .env"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "metadata")
    FUNCTION = "generate"
    CATEGORY = CATEGORY

    async def generate(self, model, prompt, negative_prompt, seed, steps, guidance,
                       prompt_upsampling, aspect_ratio, image_size, images=None,
                       alpha=-1.0, timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
                       discord_webhook_url="", api_key=""):
        if not prompt.strip():
            raise ValueError("Flux3: prompt darf nicht leer sein.")

        mode = "i2i" if images is not None else "t2i"
        payload = _build_payload(mode, prompt, negative_prompt, seed, steps,
                                 guidance, prompt_upsampling)
        payload["aspect_ratio"] = aspect_ratio
        payload["image_size"] = image_size
        if alpha >= 0:
            payload["alpha"] = float(alpha)

        if images is not None:
            # The old "1 to 4 reference images" cap was lifted; the API now takes more.
            encoded = await asyncio.to_thread(batch_to_base64, images)
            payload["input_image"] = encoded if len(encoded) > 1 else encoded[0]

        client = Flux3Client(api_key)
        task, result, data = await _run(client, model, payload, timeout_minutes)
        await _notify_discord(discord_webhook_url, task)
        return (bytes_to_image_tensor(data), format_metadata(model, payload, result, task))


class Flux3Video:
    """All video modes: t2v, i2v, ii2v, ir2v, k2v, f2v, ve2v, vr2v, t2v_sdedit."""

    @classmethod
    def INPUT_TYPES(cls):
        inputs = _common_inputs()
        inputs["model"] = (VIDEO_MODELS, {
            "default": "flux-3-large",
            "tooltip": "large = volle Qualität · distilled = schnell · "
                       "flux-3-preview-high = eigener Endpunkt auf api.bfl.ai: NUR Text→Video "
                       "(mode/Bilder/Videos, steps, guidance, negative_prompt, audio, alpha "
                       "werden dort nicht unterstützt; nur 480p/720p, kein 2s)",
        })
        return {
            "required": {
                **inputs,
                "mode": (VIDEO_MODES, {
                    "default": "t2v",
                    "tooltip": "t2v: Text→Video · "
                               "i2v: 1 Bild als Startframe → Video · "
                               "ii2v: Start- UND Endbild → Video dazwischen · "
                               "ir2v: Referenzbilder → Video (früher i2v_ref) · "
                               "k2v: Keyframes an festen Frame-Positionen → Video · "
                               "f2v: Clip aus seinen ersten Frames fortsetzen · "
                               "ve2v: Video editieren → Video (früher v2v) · "
                               "vr2v: Video als Referenz → Video (früher v2v) · "
                               "t2v_sdedit: Clip neu verrauschen & denoisen (ohne Audio)",
                }),
                "aspect_ratio": (["auto"] + VIDEO_ASPECT_RATIOS, {
                    "default": "16:9",
                    "tooltip": "'auto' nur bei flux-3-preview-high: die API wählt anhand von "
                               "Prompt und Referenzen."}),
                "duration": (["auto"] + VIDEO_DURATIONS, {
                    "default": "5s",
                    "tooltip": "'auto' nur bei flux-3-preview-high. 2s gibt es dort nicht, "
                               "und ii2v braucht eine feste Länge."}),
                "video_resolution": (VIDEO_RESOLUTIONS, {
                    "default": "720p",
                    "tooltip": "flux-3-preview-high kennt nur 480p und 720p."}),
                "audio": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Bei preview-high = generate_audio. Wird bei t2v_sdedit "
                               "ignoriert (kein Audio)."}),
            },
            "optional": {
                "image": ("IMAGE", {
                    "tooltip": "i2v: 1 Bild (Startframe) · ii2v: Startbild · "
                               "ir2v: Referenzbilder (Batch) · k2v: Keyframes (Batch)"}),
                "end_image": ("IMAGE", {"tooltip": "Nur ii2v: das Zielbild am Ende des Clips"}),
                "video": ("VIDEO", {"tooltip": "Für f2v, ve2v, vr2v und t2v_sdedit"}),
                "keyframe_indices": ("STRING", {
                    "default": "",
                    "tooltip": "Nur k2v: Frame-Positionen der Keyframes, kommagetrennt "
                               "(z.B. '0, 24, 48'). Muss so viele Werte haben wie Bilder "
                               "am image-Input hängen. 24 fps."}),
                "start_step": ("INT", {"default": 0, "min": 0, "max": 50,
                                       "tooltip": "Nur t2v_sdedit: ab welchem Step neu denoised "
                                                  "wird (0 = API-Default). Niedriger = stärkere "
                                                  "Veränderung."}),
                "alpha": ("FLOAT", {
                    "default": -1.0, "min": -1.0, "max": 100.0, "step": 0.01,
                    "tooltip": "Undokumentierter Parameter der API (float, keine Grenzen). "
                               "-1 = nicht senden / API-Default."}),
                "conditioning_noise": ("FLOAT", {
                    "default": -1.0, "min": -1.0, "max": 10.0, "step": 0.01,
                    "tooltip": "Nur i2v, ii2v, ir2v, k2v, f2v: Rauschen auf dem Eingangsbild "
                               "(>= 0). -1 = nicht senden / API-Default. 0 = kein Rauschen."}),
                "grounding": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Nur flux-3-preview-high: kurzer Recherche-Schritt vor der "
                               "Generierung. Default an."}),
                "version": ("STRING", {
                    "default": "latest",
                    "tooltip": "Nur flux-3-preview-high: 'latest' bekommt automatisch "
                               "Verbesserungen; eine feste Version hält das Verhalten stabil "
                               "(nötig für exakt reproduzierbare Seeds)."}),
                "timeout_minutes": ("INT", {
                    "default": DEFAULT_TIMEOUT_MINUTES, "min": 1, "max": 240,
                    "tooltip": "Wie lange die Node auf das Ergebnis wartet. Bei voller "
                               "BFL-Warteschlange dauert ein Job schnell 20+ Minuten. "
                               "Läuft die Zeit ab, bricht nur die Node ab — der Job läuft "
                               "serverseitig weiter (und kostet trotzdem Credits)."}),
                "discord_webhook_url": ("STRING", {
                    "default": "",
                    "tooltip": "Wenn gesetzt, postet diese Node nach der Generierung die nackte "
                               "Task-ID in den Discord-Kanal (nichts sonst). Leer = aus .env "
                               "(DISCORD_WEBHOOK_URL), sonst kein Versand."}),
                "api_key": ("STRING", {"default": "", "tooltip": "Leer = aus .env"}),
            },
        }

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "metadata")
    FUNCTION = "generate"
    CATEGORY = CATEGORY

    async def generate(self, model, prompt, negative_prompt, seed, steps, guidance,
                       prompt_upsampling, mode, aspect_ratio, duration, video_resolution,
                       audio, image=None, end_image=None, video=None, keyframe_indices="",
                       start_step=0, alpha=-1.0, conditioning_noise=-1.0,
                       grounding=True, version="latest",
                       timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
                       discord_webhook_url="", api_key=""):
        if not prompt.strip():
            raise ValueError("Flux3: prompt darf nicht leer sein.")

        # flux-3-preview-high: no `mode` field - the input field you fill decides.
        if ENDPOINTS[model]["schema"] == "preview":
            payload = _build_preview_payload(
                mode, prompt, seed, aspect_ratio, duration, video_resolution,
                audio, grounding, version,
                ignored={
                    "negative_prompt": bool(negative_prompt.strip()),
                    "steps": bool(steps),
                    "guidance": bool(guidance),
                    "prompt_upsampling": bool(prompt_upsampling),
                    "alpha": alpha >= 0,
                    "conditioning_noise": conditioning_noise >= 0,
                },
            )
            field = PREVIEW_MODE_TO_FIELD[mode]

            if field == "keyframes":
                if image is None:
                    raise ValueError(f"Flux3: Modus '{mode}' braucht einen image-Input.")
                _check_preview_images(image, "keyframes")
                imgs = image
                if mode == "ii2v":
                    if end_image is None:
                        raise ValueError(
                            f"Flux3: '{mode}' braucht zusätzlich einen end_image-Input "
                            f"(image = Startbild, end_image = Zielbild)."
                        )
                    imgs = torch.cat([image[:1], end_image[:1]])
                encoded = await asyncio.to_thread(batch_to_base64, imgs)
                payload["keyframes"] = _preview_keyframes(
                    mode, encoded, payload["duration"], keyframe_indices)

            elif field == "reference_images":
                if image is None:
                    raise ValueError(f"Flux3: Modus '{mode}' braucht einen image-Input.")
                _check_preview_images(image, "reference_images")
                if len(image) > PREVIEW_MAX_REFERENCE_IMAGES:
                    raise ValueError(
                        f"Flux3: reference_images nimmt höchstens "
                        f"{PREVIEW_MAX_REFERENCE_IMAGES} Bilder, der Batch hat {len(image)}."
                    )
                payload["reference_images"] = await asyncio.to_thread(batch_to_base64, image)

            elif field in ("edit_video", "reference_video", "start_video"):
                if video is None:
                    raise ValueError(f"Flux3: Modus '{mode}' braucht einen video-Input.")
                _check_preview_video(video)
                payload[field] = await asyncio.to_thread(video_to_base64, video)

            client = Flux3Client(api_key)
            task, result, data = await _run(client, model, payload, timeout_minutes)
            await _notify_discord(discord_webhook_url, task)
            return (VideoFromFile(io.BytesIO(data)),
                    format_metadata(model, payload, result, task))

        needs_image = mode in MODES_ONE_IMAGE + MODES_MANY_IMAGES + MODES_START_END
        needs_video = mode in MODES_VIDEO
        if needs_image and image is None:
            raise ValueError(f"Flux3: Modus '{mode}' braucht einen image-Input.")
        if needs_video and video is None:
            raise ValueError(f"Flux3: Modus '{mode}' braucht einen video-Input.")
        if mode in MODES_START_END and end_image is None:
            raise ValueError(f"Flux3: Modus '{mode}' braucht zusätzlich einen end_image-Input "
                             f"(image = Startbild, end_image = Zielbild).")

        payload = _build_payload(mode, prompt, negative_prompt, seed, steps,
                                 guidance, prompt_upsampling)
        payload["aspect_ratio"] = aspect_ratio
        payload["duration"] = duration
        payload["video_resolution"] = video_resolution

        # t2v_sdedit is the one video mode without an audio field.
        if mode != "t2v_sdedit":
            payload["audio"] = bool(audio)
        elif start_step:
            payload["start_step"] = int(start_step)

        if alpha >= 0:
            payload["alpha"] = float(alpha)
        # Only send fields the mode actually has, so the debug output stays trustworthy.
        if conditioning_noise >= 0 and mode in MODES_CONDITIONING_NOISE:
            payload["conditioning_noise"] = float(conditioning_noise)

        if mode in MODES_ONE_IMAGE:
            # i2v takes exactly one image (the API rejects a list outright).
            if len(image) > 1:
                log.warning("Flux3: i2v nimmt nur 1 Bild — es wird das erste des Batches "
                            "verwendet (%d übergeben). Für mehrere Bilder: mode=ir2v.",
                            len(image))
            payload["input_image"] = await asyncio.to_thread(tensor_to_base64, image[0])

        elif mode in MODES_START_END:
            payload["start_image"] = await asyncio.to_thread(tensor_to_base64, image[0])
            payload["end_image"] = await asyncio.to_thread(tensor_to_base64, end_image[0])

        elif mode in MODES_MANY_IMAGES:
            encoded = await asyncio.to_thread(batch_to_base64, image)
            payload["input_image"] = encoded if len(encoded) > 1 else encoded[0]

            if mode == "k2v":
                try:
                    indices = [int(x) for x in keyframe_indices.replace(";", ",").split(",")
                               if x.strip()]
                except ValueError as exc:
                    raise ValueError(
                        f"Flux3: keyframe_indices muss eine Liste ganzer Zahlen sein "
                        f"(z.B. '0, 24, 48'), bekommen: {keyframe_indices!r}"
                    ) from exc
                if len(indices) != len(image):
                    raise ValueError(
                        f"Flux3: k2v braucht genau so viele keyframe_indices wie Bilder — "
                        f"{len(image)} Bild(er), aber {len(indices)} Index/Indizes."
                    )
                payload["keyframe_indices"] = indices

        if needs_video:
            # Re-encoding a clip to base64 is slow and CPU-bound: keep it off the loop.
            payload["input_video"] = await asyncio.to_thread(video_to_base64, video)

        client = Flux3Client(api_key)
        task, result, data = await _run(client, model, payload, timeout_minutes)
        await _notify_discord(discord_webhook_url, task)
        return (VideoFromFile(io.BytesIO(data)),
                format_metadata(model, payload, result, task))


NODE_CLASS_MAPPINGS = {
    "Flux3Image": Flux3Image,
    "Flux3Video": Flux3Video,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "Flux3Image": "Flux 3 Image (API)",
    "Flux3Video": "Flux 3 Video (API)",
}
