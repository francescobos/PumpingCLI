#!/usr/bin/env python3
"""
Workout Audio Trainer con Database SQLite
Riproduce musica in sottofondo con mpv e scandisce le sessioni di allenamento
tramite sintesi vocale neurale (Gemini TTS o edge-tts) o macOS nativa (say).
Archivia e traccia tutte le frasi, gli hash, i file generati e le statistiche
di utilizzo in un database SQLite locale ('workout_cache.db').
"""

import os
import sys
import time
import json
import socket
import random
import sqlite3
import hashlib
import argparse
import subprocess
import shutil
import re
import base64
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

# Voci neurali predefinite in italiano
NEURAL_VOICES = {
    "diego": "it-IT-DiegoNeural",
    "elsa": "it-IT-ElsaNeural",
    "isabella": "it-IT-IsabellaNeural",
    "giuseppe": "it-IT-GiuseppeMultilingualNeural",
}
DEFAULT_NEURAL_VOICE = "it-IT-ElsaNeural"

# Voci Gemini TTS (catalogo completo: GET /v1beta/voices)
GEMINI_VOICES = {
    "chiara": "it-it-csagent-1",
    "sofia": "it-it-tutor-7",
    "laura": "Laomedeia",
}
DEFAULT_GEMINI_VOICE = "chiara"
GEMINI_MODEL = "gemini-3.8-flash-tts"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_GEMINI_STYLE = "personal trainer italiano, energico e motivante, ritmo deciso"

# Limiti per la generazione audio via rete
TTS_TIMEOUT = 30
MAX_CONSECUTIVE_FAILURES = 2

# Rate limit Gemini (Tier 1: 10 richieste al minuto)
GEMINI_MIN_INTERVAL = 6.5
MAX_RATE_LIMIT_WAITS = 3
MAX_RATE_LIMIT_WAIT_SECONDS = 120

CACHE_DIR = Path(".voice_cache")
DB_PATH = Path("workout_cache.db")


class VoiceDB:
    """
    Gestione del database SQLite per l'archivio audio dei comandi vocali.
    """
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS voice_samples (
                    hash TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    voice TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size_bytes INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    play_count INTEGER DEFAULT 0
                )
            """)
            conn.commit()

    @staticmethod
    def compute_hash(text: str, voice: str) -> str:
        clean_text = " ".join(text.strip().split())
        return hashlib.sha256(f"{voice}:{clean_text}".encode("utf-8")).hexdigest()[:16]

    def get_sample(self, text: str, voice: str):
        h = self.compute_hash(text, voice)
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT hash, text, voice, file_path, file_size_bytes, play_count
                FROM voice_samples
                WHERE hash = ?
            """, (h,))
            row = cur.fetchone()
            if row:
                path = Path(row[3])
                if path.exists() and path.stat().st_size > 0:
                    return {
                        "hash": row[0],
                        "text": row[1],
                        "voice": row[2],
                        "file_path": path,
                        "file_size": row[4],
                        "play_count": row[5]
                    }
        return None

    def save_sample(self, text: str, voice: str, file_path: Path):
        h = self.compute_hash(text, voice)
        size = file_path.stat().st_size if file_path.exists() else 0
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO voice_samples (hash, text, voice, file_path, file_size_bytes, last_used_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(hash) DO UPDATE SET
                    file_path = excluded.file_path,
                    file_size_bytes = excluded.file_size_bytes,
                    last_used_at = CURRENT_TIMESTAMP
            """, (h, text.strip(), voice, str(file_path), size))
            conn.commit()

    def record_play(self, text: str, voice: str):
        h = self.compute_hash(text, voice)
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE voice_samples
                SET play_count = play_count + 1,
                    last_used_at = CURRENT_TIMESTAMP
                WHERE hash = ?
            """, (h,))
            conn.commit()

    def list_samples(self):
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT hash, voice, play_count, last_used_at, text
                FROM voice_samples
                ORDER BY last_used_at DESC
            """)
            return cur.fetchall()

    def clean_orphans(self):
        """Rimuove record con file mancanti e file non registrati."""
        removed_db = 0
        removed_files = 0

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT hash, file_path FROM voice_samples")
            rows = cur.fetchall()
            valid_paths = set()

            for h, fp in rows:
                p = Path(fp)
                if not p.exists():
                    conn.execute("DELETE FROM voice_samples WHERE hash = ?", (h,))
                    removed_db += 1
                else:
                    valid_paths.add(p.resolve())
            conn.commit()

        if CACHE_DIR.exists():
            for f in [*CACHE_DIR.glob("*.mp3"), *CACHE_DIR.glob("*.wav")]:
                if f.resolve() not in valid_paths:
                    f.unlink()
                    removed_files += 1

        return removed_db, removed_files


def parse_time(time_str: str) -> int:
    s = time_str.strip().lower()
    if not s:
        return 0

    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])

    total_seconds = 0
    import re
    minutes_match = re.search(r"(\d+)\s*m", s)
    seconds_match = re.search(r"(\d+)\s*s", s)

    if minutes_match:
        total_seconds += int(minutes_match.group(1)) * 60
    if seconds_match:
        total_seconds += int(seconds_match.group(1))

    if not minutes_match and not seconds_match:
        if s.isdigit():
            total_seconds = int(s)
        else:
            raise ValueError(f"Formato tempo non valido: '{time_str}'")

    return total_seconds


def load_schedule(filepath: Path):
    if not filepath.exists():
        raise FileNotFoundError(f"File scheda non trovato: {filepath}")

    schedule = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if "|" not in line:
                print(f"[AVVISO] Riga {line_num} ignorata (manca il separatore '|'): {line}")
                continue

            time_part, message_part = line.split("|", 1)
            duration = parse_time(time_part)
            message = message_part.strip()
            if message:
                schedule.append({
                    "duration": duration,
                    "raw_time": time_part.strip(),
                    "message": message,
                    "line": line_num
                })

    return schedule


def find_edge_tts_bin() -> str:
    """
    Risolve il percorso dell'eseguibile edge-tts.
    Cerca nell'ordine:
    1. PATH di sistema
    2. Stessa directory dell'interprete Python corrente (sys.executable)
    3. Virtual environment locale .venv rispetto allo script o alla working directory
    """
    bin_path = shutil.which("edge-tts")
    if bin_path:
        return bin_path

    py_dir = Path(sys.executable).parent
    candidate = py_dir / "edge-tts"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)

    script_dir = Path(__file__).resolve().parent
    for base in [script_dir, Path.cwd()]:
        candidate = base / ".venv" / "bin" / "edge-tts"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return "edge-tts"


class TTSProvider:
    """
    Motore di sintesi con la sua chiave di cache nel DB.
    'generate(text, target_file)' scrive l'audio o solleva un'eccezione.
    """
    def __init__(self, name: str, voice_key: str, ext: str, generate):
        self.name = name
        self.voice_key = voice_key
        self.ext = ext
        self.generate = generate


class RateLimitError(RuntimeError):
    """Il servizio chiede di riprovare dopo 'retry_after' secondi (HTTP 429)."""
    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


def wait_countdown(seconds: float, reason: str):
    remaining = int(seconds + 0.999)
    while remaining > 0:
        print(f"\r   ⏳ {reason}: riprovo tra {remaining:3d}s ", end="", flush=True)
        time.sleep(1)
        remaining -= 1
    print("\r" + " " * 80 + "\r", end="", flush=True)


def generate_with_retry(provider: TTSProvider, text: str, target_file: Path):
    """Su un 429 aspetta il tempo indicato dal servizio e riprova la stessa frase."""
    for attempt in range(MAX_RATE_LIMIT_WAITS + 1):
        try:
            return provider.generate(text, target_file)
        except RateLimitError as e:
            if attempt == MAX_RATE_LIMIT_WAITS or e.retry_after > MAX_RATE_LIMIT_WAIT_SECONDS:
                raise
            wait_countdown(e.retry_after + 1, f"{provider.name}: limite di richieste raggiunto")


def make_edge_provider(voice: str) -> TTSProvider:
    edge_bin = find_edge_tts_bin()

    def generate(text: str, target_file: Path):
        cmd = [edge_bin, "--voice", voice, "--text", text, "--write-media", str(target_file)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=TTS_TIMEOUT)
        if res.returncode != 0 or not target_file.exists() or target_file.stat().st_size == 0:
            last_line = res.stderr.strip().splitlines()[-1] if res.stderr.strip() else f"exit {res.returncode}"
            raise RuntimeError(last_line[:200])

    return TTSProvider("edge-tts", voice, "mp3", generate)


def make_gemini_provider(voice_id: str, style: str, api_key: str) -> TTSProvider:
    # Lo stile cambia l'intonazione: entra nella chiave di cache
    voice_key = f"gemini:{voice_id}"
    if style:
        voice_key += ":" + hashlib.sha256(style.encode("utf-8")).hexdigest()[:6]
    last_request = [0.0]

    def generate(text: str, target_file: Path):
        # Distanzia le richieste per restare sotto il limite al minuto
        wait = GEMINI_MIN_INTERVAL - (time.time() - last_request[0])
        if wait > 0:
            time.sleep(wait)
        last_request[0] = time.time()

        content = {"type": "text", "text": text}
        if style:
            content["annotations"] = [{"type": "speech_metadata", "style": style}]
        body = {
            "model": GEMINI_MODEL,
            "input": [{"type": "user_input", "content": [content]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": voice_id}]},
        }
        req = urllib.request.Request(
            GEMINI_ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=TTS_TIMEOUT) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            match = re.search(r'"message":\s*"([^"]+)"', detail)
            message = f"HTTP {e.code}: {match.group(1) if match else detail[:200]}"
            if e.code == 429:
                delay = (re.search(r"retry in ([\d.]+)\s*s", detail)
                         or re.search(r'"retryDelay":\s*"([\d.]+)s"', detail))
                retry_after = float(delay.group(1)) if delay else float(e.headers.get("Retry-After") or 60)
                raise RateLimitError(message, retry_after)
            raise RuntimeError(message)

        audio = next(
            (part["data"] for step in resp.get("steps", []) for part in step.get("content", [])
             if part.get("type") == "audio" and part.get("data")),
            None,
        )
        if not audio:
            raise RuntimeError(f"nessun audio nella risposta (status: {resp.get('status')})")
        target_file.write_bytes(base64.b64decode(audio))

    return TTSProvider("Gemini", voice_key, "wav", generate)


def pregenerate_cues(schedule, providers, db: VoiceDB):
    """
    Genera le frasi mancanti provando i provider in ordine.
    Un provider che fallisce MAX_CONSECUTIVE_FAILURES volte di fila viene escluso.
    """
    if not providers:
        return

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    unique_messages = list(dict.fromkeys(step["message"] for step in schedule))
    missing = [msg for msg in unique_messages
               if not any(db.get_sample(msg, p.voice_key) for p in providers)]

    if not missing:
        print(f"✨ Tutte le frasi vocali ({len(unique_messages)}) sono già indicizzate nel DB e archiviate.")
        return

    active = list(providers)
    failures = {p.name: 0 for p in providers}
    print(f"🎙️  Generazione audio per {len(missing)} nuova/e frase/i (motore: {active[0].name})...")

    for i, msg in enumerate(missing, 1):
        print(f"   [{i}/{len(missing)}] Genero: \"{msg}\"")
        for p in list(active):
            h = db.compute_hash(msg, p.voice_key)
            safe_prefix = "".join(c if c.isalnum() else "_" for c in msg[:18]).strip("_")
            target_file = CACHE_DIR / f"{safe_prefix}_{h}.{p.ext}"
            try:
                generate_with_retry(p, msg, target_file)
                db.save_sample(msg, p.voice_key, target_file)
                failures[p.name] = 0
                break
            except FileNotFoundError:
                print(f"   [!] Eseguibile {p.name} non trovato nel PATH né in .venv.")
                failures[p.name] = MAX_CONSECUTIVE_FAILURES
            except subprocess.TimeoutExpired:
                print(f"   [!] {p.name}: nessuna risposta entro {TTS_TIMEOUT}s.")
                failures[p.name] += 1
            except Exception as e:
                print(f"   [!] {p.name}: {e}")
                failures[p.name] += 1

            if target_file.exists():
                target_file.unlink()
            if failures[p.name] >= MAX_CONSECUTIVE_FAILURES:
                active.remove(p)
                fallback = active[0].name if active else "la voce di sistema (say)"
                print(f"   [!] {p.name} non raggiungibile: d'ora in poi uso {fallback}.")

        if not active:
            print("   [!] Nessun motore neurale disponibile: le frasi mancanti useranno say.\n")
            return

    print("✅ Voci archiviate nel DB SQLite.\n")


def play_cue(text: str, providers, db: VoiceDB, mac_voice: str = None):
    for p in providers:
        sample = db.get_sample(text, p.voice_key)
        if sample:
            subprocess.run(["afplay", str(sample["file_path"])])
            db.record_play(text, p.voice_key)
            return

    # Fallback macOS say
    cmd = ["say"]
    if mac_voice:
        cmd.extend(["-v", mac_voice])
    cmd.append(text)
    subprocess.run(cmd)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def fetch_youtube_info(url: str) -> dict:
    cmd = ["yt-dlp", "--dump-json", "--no-playlist", url]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def download_youtube_audio(url: str, output_dir: Path, custom_title: str = None) -> Path:
    if not shutil.which("yt-dlp"):
        print("❌ Errore: 'yt-dlp' non è installato sul sistema.")
        print("   Puoi installarlo su macOS con: brew install yt-dlp ffmpeg")
        sys.exit(1)

    print(f"\n🔍 Analisi metadati YouTube: {url} ...")
    try:
        info = fetch_youtube_info(url)
    except subprocess.CalledProcessError as e:
        print(f"❌ Errore durante il recupero dei metadati da YouTube: {e}")
        if e.stderr:
            print(f"   Dettagli: {e.stderr.strip()}")
        sys.exit(1)

    title = custom_title or info.get("title", "Workout Music")
    uploader = info.get("uploader") or info.get("channel") or "Unknown"
    duration = int(info.get("duration") or 0)
    yt_id = info.get("id", "")

    safe_title = sanitize_filename(title)
    mp3_filename = f"{safe_title}.mp3"
    mp3_path = output_dir / mp3_filename

    print(f"🎵 Titolo: {title}")
    print(f"👤 Canale/Artista: {uploader}")
    print(f"⏱️  Durata: {format_seconds(duration)}")
    print(f"⬇️  Scaricamento audio e conversione in MP3 alta qualità...")

    output_template = str(output_dir / f"{safe_title}.%(ext)s")
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-metadata",
        "--no-playlist",
        "-o", output_template,
        url
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Errore durante il download con yt-dlp: {e}")
        sys.exit(1)

    if not mp3_path.exists():
        candidates = list(output_dir.glob(f"*{yt_id}*.mp3")) or list(output_dir.glob(f"{safe_title}*.mp3"))
        if candidates:
            mp3_path = candidates[0]
        else:
            print(f"⚠️  Download completato ma file MP3 non trovato con il nome atteso: {mp3_filename}")
            sys.exit(1)

    print(f"\n✅ Traccia salvata e pronta all'uso!")
    print(f"   File: {mp3_path}")
    print(f"   Puoi avviare l'allenamento con: ./pumping.py\n")
    return mp3_path


def get_audio_duration(filepath: Path) -> int:
    """Restituisce la durata del file audio in secondi usando ffprobe o afinfo."""
    filepath = Path(filepath)
    if shutil.which("ffprobe"):
        try:
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)],
                capture_output=True,
                text=True,
                timeout=5
            )
            if res.returncode == 0 and res.stdout.strip():
                return int(float(res.stdout.strip()))
        except Exception:
            pass

    if shutil.which("afinfo"):
        try:
            res = subprocess.run(["afinfo", str(filepath)], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                match = re.search(r"estimated duration:\s*([\d.]+)\s*sec", res.stdout)
                if match:
                    return int(float(match.group(1)))
        except Exception:
            pass

    return 0


class MpvController:
    def __init__(self, track_file, start_seconds: int = 0, normal_volume: int = 100, duck_volume: int = 15):
        if isinstance(track_file, (list, tuple)):
            self.track_file = Path(track_file[0]).resolve()
        else:
            self.track_file = Path(track_file).resolve()
        self.start_seconds = start_seconds
        self.normal_volume = normal_volume
        self.duck_volume = duck_volume
        self.sock_path = f"/tmp/workout_mpv_{os.getpid()}_{int(time.time())}.sock"
        self.proc = None
        self.sock = None

    def start(self):
        if not self.track_file.exists():
            raise RuntimeError(f"File audio non trovato: {self.track_file}")

        if os.path.exists(self.sock_path):
            os.remove(self.sock_path)

        cmd = [
            "mpv",
            "--no-video",
            "--really-quiet",
            f"--input-ipc-server={self.sock_path}",
            f"--volume={self.normal_volume}",
            "--loop-file=inf",
        ]
        if self.start_seconds > 0:
            cmd.append(f"--start={self.start_seconds}")

        cmd.extend(["--", str(self.track_file)])

        self.proc = subprocess.Popen(cmd)

        for _ in range(50):
            if os.path.exists(self.sock_path):
                break
            time.sleep(0.1)

        if not os.path.exists(self.sock_path):
            raise RuntimeError("Impossibile connettersi al socket IPC di mpv.")

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)

    def send_cmd(self, command_list):
        if not self.sock:
            return
        payload = json.dumps({"command": command_list}) + "\n"
        try:
            self.sock.sendall(payload.encode("utf-8"))
        except Exception:
            pass

    def set_volume(self, vol: int):
        self.send_cmd(["set_property", "volume", max(0, min(100, vol))])

    def fade_volume(self, target_vol: int, steps: int = 5, duration: float = 0.25):
        step_delay = duration / steps
        if target_vol < self.normal_volume:
            start_vol, end_vol = self.normal_volume, target_vol
        else:
            start_vol, end_vol = self.duck_volume, target_vol

        for i in range(1, steps + 1):
            cur = int(start_vol + (end_vol - start_vol) * (i / steps))
            self.set_volume(cur)
            time.sleep(step_delay)

    def duck(self):
        self.fade_volume(self.duck_volume, steps=4, duration=0.2)

    def unduck(self):
        self.fade_volume(self.normal_volume, steps=5, duration=0.35)

    def stop(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

        try:
            if self.proc:
                self.proc.terminate()
                self.proc.wait(timeout=1.5)
        except Exception:
            if self.proc:
                self.proc.kill()

        if os.path.exists(self.sock_path):
            try:
                os.remove(self.sock_path)
            except Exception:
                pass


def format_seconds(seconds: int) -> str:
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:d}:{m:02d}:{s:02d}"
    m = seconds // 60
    s = seconds % 60
    return f"{m:02d}:{s:02d}"


BIG_DIGITS = {
    "0": ["  ████  ", " ██  ██ ", " ██  ██ ", " ██  ██ ", "  ████  "],
    "1": ["   ██   ", "  ███   ", "   ██   ", "   ██   ", "  ████  "],
    "2": [" █████  ", "     ██ ", "  ████  ", " ██     ", " ██████ "],
    "3": [" █████  ", "     ██ ", "  ████  ", "     ██ ", " █████  "],
    "4": [" ██  ██ ", " ██  ██ ", " ██████ ", "     ██ ", "     ██ "],
    "5": [" ██████ ", " ██     ", " █████  ", "     ██ ", " █████  "],
    "6": ["  ████  ", " ██     ", " █████  ", " ██  ██ ", "  ████  "],
    "7": [" ██████ ", "     ██ ", "    ██  ", "   ██   ", "   ██   "],
    "8": ["  ████  ", " ██  ██ ", "  ████  ", " ██  ██ ", "  ████  "],
    "9": ["  ████  ", " ██  ██ ", "  █████ ", "     ██ ", "  ████  "],
    ":": ["   ", " ▀ ", "   ", " ▄ ", "   "],
    " ": ["  ", "  ", "  ", "  ", "  "],
}


def render_big_time(time_str: str) -> list:
    """Restituisce le 5 righe di testo per l'orario in formato gigante."""
    lines = ["", "", "", "", ""]
    for ch in time_str:
        glyph = BIG_DIGITS.get(ch, BIG_DIGITS[" "])
        for row in range(5):
            lines[row] += glyph[row]
    return lines


class TerminalScreen:
    """
    Gestisce il rendering a tutto schermo pulito nel terminale (nessun flickering).
    """
    def __init__(self):
        # Entra nel buffer alternativo del terminale e nasconde il cursore
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()

    def close(self):
        # Ripristina buffer normale e cursore
        sys.stdout.write("\033[?25h\033[?1049l\n")
        sys.stdout.flush()

    def render(self, step_idx, total_steps, current_msg, next_msg, step_rem, step_dur, total_elapsed, total_time, music_name, voice_name, start_wall_time=None):
        import shutil
        cols, rows = shutil.get_terminal_size((80, 24))
        w = min(cols, 90)

        # Determina colore/tipo fase
        is_rest = any(wrd in current_msg.lower() for wrd in ["pausa", "recupero", "riposo", "respira"])
        if is_rest:
            accent_color = "\033[1;36m"  # Ciano brillante
            tag = "☕ PAUSA & RECUPERO"
        else:
            accent_color = "\033[1;32m"  # Verde brillante
            tag = "🔥 ESERCIZIO IN CORSO"

        reset = "\033[0m"
        bold = "\033[1m"
        dim = "\033[2m"
        yellow = "\033[1;33m"

        # Timer gigante
        time_str = format_seconds(step_rem)
        big_lines = render_big_time(time_str)

        # Progress bar esercizio
        bar_len = min(40, max(15, w - 30))
        progress = (step_dur - step_rem) / step_dur if step_dur > 0 else 1.0
        filled = int(progress * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        pct = int(progress * 100)

        # Progress bar totale workout
        total_rem = max(0, total_time - total_elapsed)
        total_progress = min(1.0, max(0.0, total_elapsed / total_time)) if total_time > 0 else 1.0
        total_pct = int(total_progress * 100)
        total_bar_len = min(40, max(15, w - 30))
        total_filled = int(total_progress * total_bar_len)
        total_bar = "█" * total_filled + "░" * (total_bar_len - total_filled)

        # Costruzione frame
        lines = []
        lines.append("")
        lines.append(f"{bold}🏋️‍♂️  WORKOUT TRAINER{reset}  {dim}•  Coach: {voice_name}  •  Traccia: {music_name[:24]}{reset}".center(w + 15))
        lines.append("─" * w)
        lines.append(f" {dim}FASE {step_idx} DI {total_steps}{reset}  │  {accent_color}{bold}{tag}{reset}")
        lines.append("")
        lines.append(f"  {bold}{accent_color}{current_msg}{reset}")
        lines.append("")

        # Stampa cifre giganti (colorate di giallo negli ultimi 5s)
        time_color = yellow if step_rem <= 5 else accent_color
        for bline in big_lines:
            lines.append(f"{time_color}{bold}{bline.center(w)}{reset}")

        lines.append("")
        # Barra di avanzamento esercizio
        bar_str = f"[{accent_color}{bar}{reset}] {bold}{pct}%{reset}"
        lines.append(bar_str.center(w + 10))
        lines.append("")

        if next_msg:
            lines.append(f"  {dim}Prossimo:{reset}  {bold}{next_msg}{reset}")
        else:
            lines.append(f"  {bold}🎉 Ultima fase dell'allenamento!{reset}")

        lines.append("─" * w)
        # Barra di avanzamento totale dell'allenamento
        total_bar_str = f" 📊  Totale workout: [{accent_color}{total_bar}{reset}] {bold}{total_pct}%{reset}"
        lines.append(total_bar_str)

        info_time = (
            f" ⏱️  Trascorso: {bold}{format_seconds(total_elapsed)}{reset}  "
            f"│  Rimanente: {bold}{format_seconds(total_rem)}{reset}  "
            f"│  Totale: {bold}{format_seconds(total_time)}{reset}"
        )
        lines.append(info_time)

        if start_wall_time:
            start_str = start_wall_time.strftime("%H:%M")
            end_wall_time = datetime.now() + timedelta(seconds=total_rem)
            end_str = end_wall_time.strftime("%H:%M")
            info_clock = (
                f" 🕒  Inizio: {bold}{start_str}{reset}      "
                f"│  Fine stimata: {bold}{end_str}{reset}"
            )
            lines.append(info_clock)

        lines.append(f" {dim}Premi Ctrl+C per interrompere{reset}")

        # Stampa usando riposizionamento cursore home (\033[H) per evitare flicking
        output_buffer = "\033[H\033[2J" + "\n".join(lines)
        sys.stdout.write(output_buffer)
        sys.stdout.flush()


def run_workout(schedule, music_files, providers, voice_label, mac_voice=None, normal_vol=100, duck_vol=15, test_speed=1.0, db: VoiceDB = None, start_offset: int = 0):
    total_time = sum(step["duration"] for step in schedule)
    total_steps = len(schedule)

    pregenerate_cues(schedule, providers, db)

    if isinstance(music_files, (list, tuple)):
        chosen_track = Path(music_files[0])
    else:
        chosen_track = Path(music_files)

    player = MpvController(chosen_track, start_seconds=start_offset, normal_volume=normal_vol, duck_volume=duck_vol)
    player.start()

    time_elapsed_total = 0
    start_wall_time = datetime.now()
    music_name = chosen_track.stem
    if len(music_name) > 24:
        music_name = music_name[:21] + "..."

    screen = TerminalScreen()

    try:
        for idx, step in enumerate(schedule, 1):
            duration = step["duration"]
            msg = step["message"]
            next_msg = schedule[idx]["message"] if idx < total_steps else None

            # Esegui l'annuncio iniziale del blocco (audio ducking)
            screen.render(
                step_idx=idx,
                total_steps=total_steps,
                current_msg=msg,
                next_msg=next_msg,
                step_rem=duration,
                step_dur=duration,
                total_elapsed=time_elapsed_total,
                total_time=total_time,
                music_name=music_name,
                voice_name=voice_label,
                start_wall_time=start_wall_time
            )

            player.duck()
            play_cue(msg, providers, db, mac_voice=mac_voice)
            player.unduck()

            # Countdown dell'esercizio
            remaining = duration
            while remaining > 0:
                screen.render(
                    step_idx=idx,
                    total_steps=total_steps,
                    current_msg=msg,
                    next_msg=next_msg,
                    step_rem=remaining,
                    step_dur=duration,
                    total_elapsed=time_elapsed_total,
                    total_time=total_time,
                    music_name=music_name,
                    voice_name=voice_label,
                    start_wall_time=start_wall_time
                )

                sleep_chunk = min(1.0, remaining) / test_speed
                time.sleep(sleep_chunk)
                remaining -= 1
                time_elapsed_total += 1

        # Schermata finale
        screen.render(
            step_idx=total_steps,
            total_steps=total_steps,
            current_msg="ALLENAMENTO COMPLETATO! GRANDISSIMO LAVORO!",
            next_msg=None,
            step_rem=0,
            step_dur=1,
            total_elapsed=total_time,
            total_time=total_time,
            music_name=music_name,
            voice_name=voice_label,
            start_wall_time=start_wall_time
        )
        time.sleep(3)

    except KeyboardInterrupt:
        pass
    finally:
        screen.close()
        player.stop()
        print("\n\n👋 Sessione terminata.")

def load_dotenv(path: Path):
    """Carica le variabili da un file .env senza sovrascrivere quelle già impostate."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def build_providers(engine: str, voice_arg: str = None, style_arg: str = None):
    """
    Restituisce (provider in ordine di priorità, etichetta voce, voce per say).
    Con gemini, edge-tts resta come riserva; say è sempre l'ultima risorsa.
    """
    edge_alias = os.environ.get("EDGE_VOICE") or "elsa"

    if engine == "macos":
        return [], voice_arg or "say", voice_arg

    if engine == "edge":
        alias = voice_arg or edge_alias
        return [make_edge_provider(NEURAL_VOICES.get(alias.lower(), alias))], alias, "Alice"

    alias = voice_arg or os.environ.get("GEMINI_VOICE") or DEFAULT_GEMINI_VOICE
    edge_fallback = make_edge_provider(NEURAL_VOICES.get(edge_alias.lower(), edge_alias))
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("⚠️  GEMINI_API_KEY non impostata (.env): uso edge-tts.")
        return [edge_fallback], edge_alias, "Alice"

    style = style_arg if style_arg is not None else os.environ.get("GEMINI_STYLE", DEFAULT_GEMINI_STYLE)
    gemini = make_gemini_provider(GEMINI_VOICES.get(alias.lower(), alias), style, api_key)
    return [gemini, edge_fallback], alias, "Alice"


def main():
    load_dotenv(Path(__file__).resolve().parent / ".env")

    parser = argparse.ArgumentParser(description="Workout Audio Trainer con background music, voice coach e database SQLite.")
    parser.add_argument("--scheda", "-s", default="scheda.txt", help="Percorso del file scheda (default: scheda.txt)")
    parser.add_argument("--voice", "-v", help="Voce: gemini 'chiara', 'sofia', 'laura' o un ID del catalogo; edge 'elsa', 'diego', 'isabella', 'giuseppe' (default da .env)")
    parser.add_argument("--engine", choices=["gemini", "edge", "macos"], help="Motore voce: 'gemini', 'edge' o 'macos' (default: TTS_ENGINE in .env, altrimenti edge)")
    parser.add_argument("--style", help="Stile di lettura per Gemini (default: GEMINI_STYLE in .env)")
    parser.add_argument("--music", "-m", help="File o cartella mp3 specifica")
    parser.add_argument("--duck-vol", type=int, default=15, help="Volume durante gli annunci (0-100, default: 15)")
    parser.add_argument("--volume", type=int, default=100, help="Volume normale della musica (0-100, default: 100)")
    parser.add_argument("--test-speed", type=float, default=1.0, help="Moltiplicatore velocità per test (es. 10)")
    parser.add_argument("--list-samples", action="store_true", help="Mostra l'archivio delle frasi salvate nel DB SQLite")
    parser.add_argument("--clean-db", action="store_true", help="Pulisce i record e file orfani dal database e dalla cache")
    parser.add_argument("--download-music", "-y", metavar="URL", help="Scarica audio da YouTube (URL), converte in MP3 e salva come sottofondo")
    parser.add_argument("--title", help="Titolo personalizzato per il brano scaricato con --download-music")
    parser.add_argument("--from-start", action="store_true", help="Avvia la traccia dall'inizio (00:00) invece che da un punto casuale")
    parser.add_argument("--start", type=int, metavar="SECONDI", help="Punto di inizio riproduzione specifico in secondi")
    args = parser.parse_args()

    db = VoiceDB()

    if args.list_samples:
        samples = db.list_samples()
        if not samples:
            print("📭 Nessun campione audio presente nel database.")
        else:
            print("\n📋 ARCHIVIO CAMPIONI AUDIO (workout_cache.db)")
            print("-" * 80)
            print(f"{'HASH':<18} | {'VOCE':<20} | {'USI':<5} | {'TESTO'}")
            print("-" * 80)
            for h, voice, play_cnt, last_used, text in samples:
                short_voice = voice.replace("it-IT-", "").replace("Neural", "").split(":")
                short_voice = short_voice[1] if short_voice[0] == "gemini" else short_voice[0]
                print(f"{h:<18} | {short_voice:<20} | {play_cnt:<5} | {text}")
            print("-" * 80)
            print(f"Totale frasi archiviate: {len(samples)}\n")
        return

    if args.clean_db:
        rem_db, rem_files = db.clean_orphans()
        print(f"🧹 Pulizia completata: rimossi {rem_db} record orfani dal DB e {rem_files} file non utilizzati.")
        return

    if args.download_music:
        dest_dir = Path(args.music).resolve() if (args.music and Path(args.music).is_dir()) else Path(".").resolve()
        download_youtube_audio(args.download_music, dest_dir, custom_title=args.title)
        return

    scheda_path = Path(args.scheda).resolve()
    if not scheda_path.exists():
        print(f"Errore: il file di scheda '{scheda_path}' non esiste.")
        sys.exit(1)

    schedule = load_schedule(scheda_path)
    if not schedule:
        print(f"Errore: nessuna riga valida trovata in '{scheda_path}'.")
        sys.exit(1)

    engine = args.engine or os.environ.get("TTS_ENGINE", "edge").lower()
    if engine not in ("gemini", "edge", "macos"):
        print(f"Errore: TTS_ENGINE '{engine}' non valido (usa gemini, edge o macos).")
        sys.exit(1)
    providers, voice_label, mac_voice = build_providers(engine, args.voice, args.style)

    # Ricerca tracce musicali
    music_files = []
    if args.music:
        p = Path(args.music).resolve()
        if p.is_dir():
            music_files = list(p.glob("*.mp3")) + list(p.glob("*.m4a"))
        elif p.is_file():
            music_files = [p]
    else:
        cwd = Path(".").resolve()
        music_files = list(cwd.glob("*.mp3")) + list(cwd.glob("*.m4a"))

    if not music_files:
        print("Errore: nessun file audio (.mp3 o .m4a) trovato nella cartella corrente o specificata con --music.")
        print("💡 Puoi scaricare un brano da YouTube con: ./pumping.py --download-music <URL>")
        sys.exit(1)

    # Scelta casuale del brano tra quelli disponibili (come in FlowLoop)
    chosen_track = random.choice(music_files)
    duration = get_audio_duration(chosen_track)

    # Scelta casuale del punto di avvio (come in FlowLoop)
    if args.start is not None:
        start_offset = max(0, min(args.start, duration - 10 if duration > 10 else 0))
    elif args.from_start or duration <= 60:
        start_offset = 0
    else:
        max_start = max(0, duration - 45)
        start_offset = random.randint(0, max_start)

    print(f"🎵 Sottofondo musicale: {chosen_track.name}")
    if len(music_files) > 1:
        print(f"🎲 Traccia scelta casualmente tra {len(music_files)} brani disponibili.")
    if duration > 0:
        offset_info = format_seconds(start_offset) if start_offset > 0 else "00:00 (dall'inizio)"
        print(f"⏱️  Durata traccia: {format_seconds(duration)} | Punto di avvio: {offset_info}")

    run_workout(
        schedule=schedule,
        music_files=chosen_track,
        providers=providers,
        voice_label=voice_label,
        mac_voice=mac_voice,
        normal_vol=args.volume,
        duck_vol=args.duck_vol,
        test_speed=args.test_speed,
        db=db,
        start_offset=start_offset
    )


if __name__ == "__main__":
    main()

