# Flux 3 API — ComfyUI Nodes

ComfyUI-Nodes für die BFL Flux-3-API (`flux-3-large` / `flux-3-distilled`).

## Setup

Key in `.env` eintragen:

```
BFL_API_KEY=bfl_...
BFL_BASE_URL=https://api.isr.bfl.ai
```

Der Key wird in dieser Reihenfolge gesucht: `api_key`-Feld der Node → `.env` → Umgebungsvariable `BFL_API_KEY`.

## Nodes

**Flux 3 Image (API)** — Text→Bild. Wenn der optionale `images`-Input belegt ist, schaltet die Node
automatisch auf Edit/Remix (bis zu 4 Bilder aus dem Batch). Ausgaben: `image`, `metadata`.

**Flux 3 Video (API)** — alle neun Video-Modi über das `mode`-Widget. Ausgaben: `video` (mp4, 24 fps,
mit Audiospur) und `metadata`. Direkt an `Save Video` hängen.

### Endpunkt-Auswahl (`model`)

| model | Host | Schema |
|---|---|---|
| `flux-3-large` | `api.isr.bfl.ai` | `mode`-Union, undokumentiert |
| `flux-3-distilled` | `api.isr.bfl.ai` | `mode`-Union, undokumentiert, schnell |
| `flux-3-preview-high` | **`api.bfl.ai`** | **offizielle Early-Access-API** |

**`flux-3-preview-high` ist der offiziell dokumentierte Endpunkt.** Er kann dieselben acht
Verhalten wie die mode-basierte API — nur **ohne `mode`-Feld**: Welches Input-Feld du füllst,
entscheidet, was passiert. Das `mode`-Widget der Node wählt deshalb einfach das passende Feld:

| mode | Feld bei preview-high | Bedeutung |
|---|---|---|
| `t2v` | – | nichts angehängt, reiner Text |
| `i2v` | `keyframes` (1 Bild @ frame 0) | Bild eröffnet den Clip |
| `ii2v` | `keyframes` (2 Bilder, letzter @ `duration×24`) | Morph Start→Ende, **feste Länge nötig** |
| `k2v` | `keyframes` (n Bilder + Indizes) | Storyboard |
| `ir2v` | `reference_images` (1–10) | nur das Subjekt, Bilder erscheinen nie im Bild |
| `ve2v` | `edit_video` | Shot behalten, Aussehen ändern |
| `vr2v` | `reference_video` | Cast behalten, neuer Shot |
| `f2v` | `start_video` | vom Ende weitergenerieren |

`t2v_sdedit` gibt es dort **nicht** — nur bei large/distilled.

Die Node übersetzt die Widgets automatisch (`"5s"` → `5`, `video_resolution` → `resolution`,
`audio` → `generate_audio`) und prüft die dokumentierten Grenzen **vor** dem Request:

- `duration = 2s` und `video_resolution = 192p/352p` gibt es dort nicht
- `ii2v` braucht eine feste Länge (kein `auto`)
- `ve2v`/`vr2v` vertragen bei 720p kein 15s/20s → 5s/10s oder 480p
- `keyframe_indices` müssen eindeutig und ≤ `duration×24` sein
- `reference_images`: höchstens 10
- Bilder mindestens **256×256 px** (undokumentiert, ermittelt)
- Video-Inputs höchstens 15 s (und 50 MB)
- `seed` höchstens 4294967295

Nicht unterstützte Settings (`steps`, `guidance`, `negative_prompt`, `prompt_upsampling`, `alpha`,
`conditioning_noise`) erzeugen eine Konsolenwarnung, statt still ignoriert zu werden.

Zusätzliche Widgets nur für diesen Endpunkt: `grounding` (kurzer Recherche-Schritt vor der
Generierung, Default an) und `version` (`latest` oder feste Version — nötig, damit ein Seed
wirklich reproduzierbar ist).

`.env`-Einstellung `BFL_BASE_URL` gilt nur für large/distilled; `flux-3-preview-high` hat seinen Host
fest eingebaut.

### metadata-Output (Debug)

Beide Nodes geben einen `STRING` mit **allem** aus, was zum Lauf gehört — gedacht für eine
*Show Any*-Node zum Debuggen. Ungekürzt, kein Discord-Format:

```
=== FLUX 3 ===
endpoint       : POST /v1/flux-3-distilled
task_id        : e696c5eb-e97a-484f-9976-d7a5722658b2
polling_url    : https://api.isr.bfl.ai/v1/get_result?id=e696c5eb-…

--- REQUEST (an die API gesendet) ---
mode           : i2i
prompt         : make it green
seed           : 7
aspect_ratio   : 1:1
image_size     : 256sq
input_image    : <base64, 29 KB>

--- RESPONSE (von der API) ---
sample         : https://delivery.isr.bfl.ai/results/…
duration       : 2.36
prompt         : make it green
seed           : 7

seed (genutzt) : 7
```

Der Request-Block zeigt exakt die Felder, die an die API gingen — Base64-Bilder/-Videos werden dabei
als Größenangabe dargestellt statt als megabytelange Zeichenkette. Der Response-Block enthält u. a. die
Ergebnis-URL, den tatsächlich verwendeten Seed und (bei `prompt_upsampling`) den umgeschriebenen Prompt.

## Warteschlange / Timeout

Die BFL-Warteschlange kann bei Videos deutlich über 15 Minuten laufen. ComfyUI selbst hat **kein**
Ausführungs-Timeout — es wartet beliebig lange. Begrenzt wird nur durch `timeout_minutes`
(Default **45**, bis 240 einstellbar).

Läuft die Zeit ab, bricht **nur die Node** ab; der Job läuft bei BFL weiter und kostet trotzdem
Credits. Die Fehlermeldung enthält deshalb die `polling_url`, mit der sich das Ergebnis nachträglich
abholen lässt.

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

Beide Nodes sind **async** (`async def generate`). ComfyUI erkennt Coroutine-`FUNCTION`s, parkt eine
wartende Node als `PENDING` und führt währenddessen andere Nodes aus. Mehrere Flux-Nodes in einem
Graph generieren deshalb **gleichzeitig**, nicht nacheinander — die Wartezeit ist die des langsamsten
Clips, nicht die Summe aller.

Damit das hält, darf nichts den Event-Loop blockieren: Das Polling nutzt `await asyncio.sleep()`, und
alle blockierenden Teile (HTTP-Requests, Base64-Encoding von Bildern/Videos) laufen über
`asyncio.to_thread()`. Ein einzelnes `time.sleep()` an der falschen Stelle würde den gesamten Graph
wieder serialisieren.

## Discord-Webhook: nur die Task-ID

Trägst du in `discord_webhook_url` (oder in der `.env` als `DISCORD_WEBHOOK_URL`) einen Discord-Webhook
ein, postet die Node nach der Generierung **ausschließlich die nackte Task-ID** in den Kanal:

```
e696c5eb-e97a-484f-9976-d7a5722658b2
```

Kein Markdown, kein Prefix — damit ein Bot/Script sie direkt parsen und daraus das Video holen kann
(`GET https://api.isr.bfl.ai/v1/get_result?id=<TASK_ID>` mit Header `x-key`, das Video liegt in
`result.sample`). Damit umgeht der Text-Weg Discords 2000-Zeichen-Limit komplett.

Achtung: Die Ergebnis-URL in `result.sample` ist zeitlich befristet (signierte URL mit `se=`-Ablauf).
Das Script sollte das Video also zeitnah abholen, nicht Tage später.

Fehler werden nicht verschluckt: Lehnt Discord etwas ab, steht der Klartext-Grund in der
ComfyUI-Konsole. Der Webhook-Token wird beim Loggen ausgeblendet, und ein fehlgeschlagener Post bricht
die Generierung nicht ab (die hat ja schon Credits gekostet).

| mode | braucht | macht |
|---|---|---|
| `t2v` | – | Text → Video |
| `i2v` | `image` (1) | Bild als Startframe → Video |
| `ii2v` | `image` + `end_image` | Start- **und** Endbild → Video dazwischen |
| `ir2v` | `image` (Batch) | Referenzbilder → Video |
| `k2v` | `image` (Batch) + `keyframe_indices` | Keyframes an festen Frame-Positionen → Video |
| `f2v` | `video` | Clip aus seinen ersten Frames fortsetzen |
| `ve2v` | `video` | Video editieren → Video |
| `vr2v` | `video` | Video als Referenz → Video |
| `t2v_sdedit` | `video` | Clip neu verrauschen & denoisen — **ohne Audio**, dafür `start_step` |

`keyframe_indices` wird als kommagetrennte Liste eingegeben (z. B. `0, 24, 48`) und muss genau so
viele Werte haben wie Bilder am `image`-Input hängen. Bei 24 fps entspricht `24` also Sekunde 1.

`seed`, `steps` und `guidance` auf `0` lassen heißt: Feld wird nicht gesendet, die API nimmt ihren
eigenen Default (bei `seed` also zufällig).

**Seed-Bereich:** Das Widget geht bis **4294967295** (2³²−1), nicht bis 2⁶⁴−1. Grund:
`flux-3-preview-high` lehnt alles darüber mit `422` ab — bei ComfyUIs `randomize` und einem
64-Bit-Maximum würde also praktisch jeder Lauf scheitern. `flux-3-large`/`distilled` akzeptieren
jeden Integer, daher ist dieser Bereich für alle Endpunkte sicher.

## API-Schema

Flux 3 ist in der öffentlichen `openapi.json` **nicht** enthalten. Das folgende Schema wurde gegen die
Live-API ermittelt (Pydantic-Validierungsfehler, ohne Generierungen auszulösen) — Stand 2026-07-11.

`POST /v1/flux-3-large` bzw. `/v1/flux-3-distilled`, Header `x-key: <API-KEY>`.
Der Body ist eine **discriminated union über das Feld `mode`**:
`t2i`, `i2i`, `t2v`, `t2v_sdedit`, `i2v`, `ii2v`, `ir2v`, `k2v`, `f2v`, `ve2v`, `vr2v`.
Beide Modelle akzeptieren dieselben elf Modi und dieselben Felder.

> **Änderung am 2026-07-12:** Die API hat Modi umbenannt und ergänzt. `i2v_ref` heißt jetzt `ir2v`,
> `v2v` wurde in `ve2v` (editieren) und `vr2v` (referenzieren) aufgeteilt. Neu sind `ii2v`
> (Start-/Endbild) und `k2v` (Keyframes). Das frühere Limit von „1 bis 4 Referenzbildern" bei `i2i`
> und `ir2v` ist weggefallen. Bei den Video-Modi kam `9:21` als Seitenverhältnis dazu.

Antwort: `{id, polling_url, cost, ...}` → dann `GET /v1/get_result?id=…` pollen, bis
`status == "Ready"`; das Asset liegt in `result.sample` als URL.
Status-Werte: `Pending`, `Reasoning`, `Generating`, `Ready`, `Error`, `Request Moderated`,
`Content Moderated`, `Task not found`.

### Felder in allen Modi

| Feld | Typ | Anmerkung |
|---|---|---|
| `prompt` | str | **Pflicht** in jedem Modus |
| `negative_prompt` | str | |
| `seed` | int | |
| `steps` | int | 1–50 |
| `guidance` | float | |
| `alpha` | float | undokumentiert, keine Grenzen |
| `debug` | bool | undokumentiert |
| `prompt_upsampling` | bool | |
| `webhook_url` | url | |
| `webhook_secret` | str | |

Zusätzlich nur bei `i2v` und `f2v`: `conditioning_noise` (float, ≥ 0).

**Nicht existierende Felder**, die kursieren: `audio_guidance`, `prepend_input_frame`, `reference`,
`checkpoint`. Die API ignoriert sie stillschweigend (`extra="ignore"`) — sie haben keinerlei Wirkung.

### Bild-Modi (`t2i`, `i2i`)

| Feld | Typ | Werte |
|---|---|---|
| `aspect_ratio` | literal | `1:2` `9:21` `9:16` `2:3` `3:4` `4:5` `5:7` `1:1` `7:5` `5:4` `4:3` `3:2` `16:9` `21:9` `2:1` |
| `image_size` | literal | `256sq` `384sq` `512sq` `768sq` `1024sq` `2048sq` `4096sq` |
| `input_image` | str \| list[str] | **Pflicht bei `i2i`**; base64 oder URL |
| `conditioning_noise` | float | bei `i2i` vorhanden |

### Video-Modi (`t2v`, `i2v`, `i2v_ref`, `f2v`, `v2v`, `t2v_sdedit`)

| Feld | Typ | Werte |
|---|---|---|
| `aspect_ratio` | literal | `21:9` `16:9` `4:3` `1:1` `3:4` `9:16` `9:21` |
| `duration` | literal | `2s` `5s` `10s` `15s` `20s` |
| `video_resolution` | literal | `192p` `352p` `480p` `720p` |
| `audio` | bool | in allen Video-Modi **außer** `t2v_sdedit` |
| `input_image` | str | **Pflicht bei `i2v`** — genau **ein** Bild, eine Liste wird abgelehnt |
| `input_image` | str \| list[str] | **Pflicht bei `ir2v` und `k2v`** |
| `start_image` + `end_image` | str | **Pflicht bei `ii2v`** — je genau ein Bild |
| `keyframe_indices` | list[int] | **Pflicht bei `k2v`**, zusammen mit `input_image` |
| `input_video` | str | **Pflicht bei `f2v`, `ve2v`, `vr2v`, `t2v_sdedit`** |
| `start_step` | int | nur `t2v_sdedit` |
| `conditioning_noise` | float ≥ 0 | nur `i2v`, `ii2v`, `ir2v`, `k2v`, `f2v` (**nicht** bei `ve2v`/`vr2v`) |

**Start-/End-Frame gibt es jetzt:** `ii2v` nimmt `start_image` und `end_image` und generiert den
Übergang. (Beim ersten Schema-Probing im Juli existierte dieser Modus noch nicht — die API wurde
seither erweitert.)
| `input_video` | str | **Pflicht bei `f2v`, `v2v`, `t2v_sdedit`**; base64 mp4 |
| `start_step` | int | nur `t2v_sdedit` |

Unbekannte Felder werden von der API stillschweigend ignoriert (`extra="ignore"`).

### flux-3-preview-high (offizielles Schema)

`POST https://api.bfl.ai/v1/flux-3-preview-high`, Header `x-key`. **Kein `mode`-Feld** — das
gefüllte Input-Feld entscheidet über das Verhalten. `extra="forbid"`: unbekannte Felder werden
abgelehnt (nicht ignoriert wie bei large/distilled).

**Input-Felder** — höchstens *eines* pro Request, sonst `422`:

| Feld | Typ | |
|---|---|---|
| `keyframes` | list[{`image_url`: str, `frame_index`: int}] | Bilder pixelgenau an Frame-Positionen |
| `reference_images` | list[str], max **10** | nur das Subjekt |
| `edit_video` | str | Clip umrendern |
| `reference_video` | str | neuer Clip mit den Subjekten |
| `start_video` | str | Clip fortsetzen |
| `reference_audio` | str | **reserviert**, akzeptiert aber wirkungslos |

**Settings** — gelten immer:

| Feld | Typ | Default |
|---|---|---|
| `prompt` | str | **Pflicht** |
| `aspect_ratio` | `auto` `21:9` `16:9` `4:3` `1:1` `3:4` `9:16` `9:21` | `auto` |
| `resolution` | `480p` `720p` | `720p` |
| `duration` | `5` `10` `15` `20` (int) oder `auto` | `auto` |
| `generate_audio` | bool | `true` |
| `grounding` | bool | `true` |
| `version` | str | `latest` |
| `seed` | int 0–4294967295 | zufällig |
| `webhook_url` | url | – |

Es gibt **kein** `steps`, `guidance`, `negative_prompt`, `alpha`, `prompt_upsampling`.
Der Status durchläuft `Pending` → `Reasoning` → `Generating` → `Ready`, und die Ergebnis-URL liegt
unter `/durable/` (Gültigkeit ca. **2 Stunden**). Die `polling_url` zeigt auf `api.isr.bfl.ai` —
beide Hosts teilen sich dasselbe Backend.

**Rate limit:** 5 gleichzeitige Generierungen pro Organisation. Darüber gibt es `429` — das ist kein
Fehler zum Wiederholen in der Schleife, sondern heißt: auf einen laufenden Job warten.

**Ausgabemaße** (alle Vielfache von 32):

| Ratio | 480p | 720p |
|---|---|---|
| `21:9` | 992 × 416 | 1440 × 608 |
| `16:9` | 864 × 480 | 1280 × 704 |
| `4:3` | 736 × 544 | 1088 × 800 |
| `1:1` | 640 × 640 | 960 × 960 |
| `3:4` | 544 × 736 | 800 × 1088 |
| `9:16` | 480 × 864 | 704 × 1280 |
| `9:21` | 416 × 992 | 608 × 1440 |
