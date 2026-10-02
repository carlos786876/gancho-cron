# Worker de TikTok (ver .github/workflows/tiktok-scrape.yml). Entrada: client_payload del evento = {run, handle, limit}.
# Sube a Gemini solo el medio de los videos que Gancho analizará: >= MIN_VIEWS vistas o
# >= VIRAL_X veces la mediana de la cuenta (aprox. del Puntaje viral de Gancho).
# ponytail: umbrales fijos; leerlos de Gancho si se cambian seguido en Configuración.
import json, os, statistics, subprocess, sys, tempfile, time, urllib.request

MIN_VIEWS, VIRAL_X = 100_000, 3  # igual que "mínimo de vistas para analizar" en Configuración
MAX_MEDIA = 40  # tope de subidas por run (los más recientes primero): cabe en el timeout de 20 min
# El payload se lee del evento (no de una variable: el log de un repo público mostraría el env) y
# el @ se enmascara para que la lista de competidores no quede pública en los logs.
p = json.load(open(os.environ["GITHUB_EVENT_PATH"], encoding="utf8"))["client_payload"]
run, handle, limit = p["run"], p["handle"], int(p["limit"])
print(f"::add-mask::{handle}")


def post(body):
    req = urllib.request.Request(
        os.environ["GANCHO_URL"].rstrip("/") + "/api/worker/tiktok",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["GANCHO_TOKEN"]},
    )
    print(urllib.request.urlopen(req, timeout=60).read().decode())


def ytdlp(*args, timeout=600):
    return subprocess.run(["yt-dlp", "--impersonate", "chrome", *args], capture_output=True, text=True, timeout=timeout)


def groq_transcript(path):
    """Segmentos de Groq Whisper (gratis) o None: sin clave, archivo > 25 MB o error -> Gancho transcribe con Gemini."""
    key = os.environ.get("GROQ_API_KEY")
    if not key or os.path.getsize(path) > 25 * 1024 * 1024:
        return None
    r = subprocess.run(
        ["curl", "-s", "--max-time", "120", "https://api.groq.com/openai/v1/audio/transcriptions",
         "-H", "Authorization: Bearer " + key, "-F", f"file=@{path}",
         "-F", "model=whisper-large-v3-turbo", "-F", "response_format=verbose_json"],
        capture_output=True, text=True,
    )
    try:
        segs = json.loads(r.stdout)["segments"]
    except Exception:
        print("groq", r.stdout[-200:], file=sys.stderr)
        return None
    # Whisper inventa texto en música/silencio: esos tramos se descartan (igual que en Gancho).
    return [{"startSec": s["start"], "endSec": max(s["start"], s["end"]), "text": s["text"].strip()}
            for s in segs if s.get("no_speech_prob", 0) <= 0.6 and s["text"].strip()]


def upload_media(client, url, item):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "v.mp4")
        r = ytdlp("-f", "b[ext=mp4]/b", "-o", path, url, timeout=300)
        if not os.path.exists(path):
            raise RuntimeError(r.stderr[-300:])
        transcript = groq_transcript(path)
        if transcript is not None:
            item["transcript"] = transcript
        # El medio sigue yendo a Gemini: el Análisis visual de los Virales lo necesita.
        f = client.files.upload(file=path, config={"mime_type": "video/mp4"})
        for _ in range(30):  # Gemini procesa asíncrono: esperar ACTIVE
            state = str(getattr(f, "state", "")).upper()
            if "ACTIVE" in state:
                return f.uri
            if "FAILED" in state:
                raise RuntimeError("Gemini FAILED")
            time.sleep(2)
            f = client.files.get(name=f.name)
        raise RuntimeError("Gemini no terminó de procesar")


def list_videos():
    # TikTok corta a veces la conexión (sobre todo al paginar): se reintenta y se conserva lo que
    # alcanzó a llegar aunque yt-dlp termine con error.
    err = ""
    for attempt in range(3):
        out = ytdlp("--flat-playlist", "-j", "--playlist-end", str(limit), f"https://www.tiktok.com/@{handle}")
        entries = [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]
        err = (out.stderr or out.stdout or "sin salida")[-500:]
        print(f"intento {attempt + 1}: {len(entries)} videos; yt-dlp: {err[-200:]!r}", file=sys.stderr)
        if entries:
            return entries
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(err)


try:
    entries = list_videos()
    median = statistics.median([e.get("view_count") or 0 for e in entries])
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    keep = ("id", "webpage_url", "url", "timestamp", "title", "description", "duration", "view_count", "like_count", "comment_count")
    items = []
    for e in entries:
        item = {k: e.get(k) for k in keep}
        views = e.get("view_count") or 0
        uploaded = sum("mediaUrl" in i for i in items)
        if uploaded < MAX_MEDIA and (views >= MIN_VIEWS or (median > 0 and views >= VIRAL_X * median)):
            try:
                item["mediaUrl"] = upload_media(client, e.get("webpage_url") or e.get("url"), item)
            except Exception as ex:  # sin medio: Gancho reintenta la transcripción o la marca fallida
                print("medio", e.get("id"), ex, file=sys.stderr)
        items.append(item)
    print(f"{handle}: {len(items)} videos, {sum('mediaUrl' in i for i in items)} con medio, {sum('transcript' in i for i in items)} con transcript")
    post({"run": run, "items": items})
except Exception as ex:
    post({"run": run, "error": str(ex)[:500]})
    raise
