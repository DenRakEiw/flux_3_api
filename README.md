# Flux 3 API — ComfyUI Nodes

ComfyUI-Node für die BFL Flux-3-Video-API.

**Ein Endpunkt:** `POST https://api.bfl.ai/v1/flux-3-video`
([Doku](https://docs.bfl.ai/flux_3/flux3_video),
[API-Referenz](https://docs.bfl.ai/api-reference/utility/generate-a-video-with-flux-3)).

## Setup

Key in `.env` eintragen:

```
BFL_API_KEY=bfl_...
```

Der Key wird in dieser Reihenfolge gesucht: `api_key`-Feld der Node → `.env` → Umgebungsvariable `BFL_API_KEY`.

Optional in `.env`:

```
BFL_BASE_URL=https://api.bfl.ai
```

(Default ist `https://api.bfl.ai`; nur ändern, wenn BFL einen anderen Host bekannt gibt.)

## Node

**Flux 3 Video (API)** — Ausgaben: `video` (mp4, mit Audiospur) und `metadata`. Direkt an `Save Video` hängen.

### Modi (`mode`)

Ein Modus pro Request; der Rest des Requests bleibt gleich.

| mode | braucht | macht |
|---|---|---|
| `t2v` | – | Text → Video |
| `i2v` | `images` (1–10) | Bild(er) → Video (keyframes) |
| `v2v` | `video` | Clip fortsetzen (start_video) |
| `draft_enhance` | `draft_cache` | einen vorherigen `draft`-Lauf final rendern |

### Parameter

| Feld | Pflicht | Werte |
|---|---|---|
| `mode` | immer | `t2v` `i2v` `v2v` `draft_enhance` |
| `prompt` | t2v/i2v/v2v | frei |
| `keyframes` | i2v | 1 Bild (Startframe) · 2 Bilder (Start+Ende) · 3–10 Bilder (gleichmäßig, braucht `duration`) · oder `[sekunden, bild]`-Paare (Storyboard). URL oder base64. |
| `start_video` | v2v | mp4 URL oder base64 |
| `draft_cache` | draft_enhance | base64-Bundle oder URL aus dem `draft`-Output eines vorherigen Laufs |
| `aspect_ratio` | – | `auto` (Default), `21:9`, `2:1`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16` |
| `duration` | – | ganze Sekunden 5–20 oder `auto` (Default) |
| `resolution` | – | `hd` (Default) oder `fhd` |
| `generate_audio` | – | bool, Default `true` |
| `safety_tolerance` | – | 0 (strengste) bis 4, Default 2. Mit Conditioning-Media maximal 2. |
| `draft` | – | bool. `true` = schnelle hd-Vorschau; Ergebnis enthält einen `draft_cache`. |
| `version` | – | `latest` (Default) |

`draft_enhance` akzeptiert **nur** `mode`, `draft_cache` und `safety_tolerance` — der Bundle pins
Modus, Prompt, Seed und Conditioning. Alle anderen Inputs werden ignoriert (mit Konsolenwarnung).

### i2v keyframes (ComfyUI-UX)

Die Node übersetzt die ComfyUI-Inputs in das dokumentierte `keyframes`-Schema:

| was angeschlossen | keyframe_times | gesendet |
|---|---|---|
| 1 Bild | – | `"<base64>"` (Startframe) |
| 2 Bilder *oder* `images`+`end_image` | – | `["<a>", "<b>"]` (Start+Ende) |
| 3–10 Bilder | – | `["<a>", ...]` (gleichmäßig verteilt — `duration` muss gesetzt sein) |
| n Bilder | n Sekunden | `[[t1, "<a>"], ...]` (Storyboard — Werte aufsteigend) |

### metadata-Output (Debug)

Der `STRING`-Output enthält alles zum Lauf — gedacht für eine *Show Any*-Node zum Debuggen. Ungekürzt:

```
=== FLUX 3 VIDEO ===
endpoint       : POST https://api.bfl.ai/v1/flux-3-video
task_id        : e696c5eb-e97a-484f-9976-d7a5722658b2
polling_url    : https://api.bfl.ai/v1/get_result?id=e696c5eb-…

--- REQUEST (an die API gesendet) ---
mode           : i2v
prompt         : a seed grows into a tree through the seasons
duration       : 10
resolution     : hd
generate_audio : True
keyframes      : [[0, <base64, 412 KB>], [10, <base64, 398 KB>]]

--- RESPONSE (von der API) ---
sample         : https://delivery.bfl.ai/results/…
```

Base64-Bilder/-Videos/-Bundles werden als Größenangabe dargestellt statt als megabytelange Zeichenkette.

## Warteschlange / Timeout

Die BFL-Warteschlange kann bei Videos deutlich über 15 Minuten laufen. ComfyUI selbst hat **kein**
Ausführungs-Timeout — es wartet beliebig lange. Begrenzt wird nur durch `timeout_minutes`
(Default **45**, bis 240 einstellbar).

Läuft die Zeit ab, bricht **nur die Node** ab; der Job läuft bei BFL weiter und kostet trotzdem
Credits. Die Fehlermeldung enthält deshalb die `polling_url`, mit der sich das Ergebnis nachträglich
abrufen lässt.

Während des Wartens meldet die Konsole jede Minute den Status, damit ein langer Lauf nicht wie ein
Hänger aussieht. Der Poll-Abstand wächst mit der Wartezeit (2 s bis max. 10 s) — bei 25 Minuten sind
das 201 statt 750 Requests.

**Netzwerk-Aussetzer:** Das Polling überlebt sie. `requests` versucht von sich aus keinen einzigen
Retry, sodass eine einzelne gekappte Keep-Alive-Verbindung (`RemoteDisconnected`) einen bereits
bezahlten Job vernichtet hätte. Jetzt gilt: bis zu 5 automatische Retries pro Request (mit Backoff)
und darüber hinaus bis zu 10 Fehlversuche in Folge, bevor aufgegeben wird. Retried werden nur GETs —
ein wiederholter POST könnte den Job ein zweites Mal einreichen und doppelt abrechnen. Auch der
Download des fertigen Assets wird bis zu 4× versucht, da die signierte Ergebnis-URL abläuft.

## Parallele Läufe

Die Node ist **async** (`async def generate`). ComfyUI erkennt Coroutine-`FUNCTION`s, parkt eine
wartende Node als `PENDING` und führt währenddessen andere Nodes aus. Mehrere Flux-Nodes in einem
Graph generieren deshalb **gleichzeitig**, nicht nacheinander — die Wartezeit ist die des langsamsten
Clips, nicht die Summe aller.

Damit das hält, darf nichts den Event-Loop blockieren: Das Polling nutzt `await asyncio.sleep()`, und
alle blockierenden Teile (HTTP-Requests, Base64-Encoding von Bildern/Videos) laufen über
`asyncio.to_thread()`. Ein einzelnes `time.sleep()` an der falschen Stelle würde den gesamten Graph
wieder serialisieren.
