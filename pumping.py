#!/usr/bin/env python3
"""
Workout Audio Trainer con Database SQLite
Riproduce musica in sottofondo con mpv e scandisce le sessioni di allenamento
tramite sintesi vocale neurale HD (edge-tts) o macOS nativa (say).
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
from pathlib import Path

# Voci neurali predefinite in italiano
NEURAL_VOICES = {
    "diego": "it-IT-DiegoNeural",
    "elsa": "it-IT-ElsaNeural",
    "isabella": "it-IT-IsabellaNeural",
    "giuseppe": "it-IT-GiuseppeMultilingualNeural",
}
DEFAULT_NEURAL_VOICE = "it-IT-ElsaNeural"

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
            for f in CACHE_DIR.glob("*.mp3"):
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


def pregenerate_neural_cues(schedule, voice: str, db: VoiceDB):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    unique_messages = list(dict.fromkeys(step["message"] for step in schedule))
    missing = []

    for msg in unique_messages:
        sample = db.get_sample(msg, voice)
        if not sample:
            h = db.compute_hash(msg, voice)
            safe_prefix = "".join(c if c.isalnum() else "_" for c in msg[:18]).strip("_")
            target_file = CACHE_DIR / f"{safe_prefix}_{h}.mp3"
            missing.append((msg, target_file))

    if not missing:
        print(f"✨ Tutte le frasi vocali ({len(unique_messages)}) sono già indicizzate nel DB e archiviate.")
        return

    print(f"🎙️  Generazione audio neurale per {len(missing)} nuova/e frase/i (voce: {voice})...")
    for i, (msg, target_file) in enumerate(missing, 1):
        print(f"   [{i}/{len(missing)}] Genero: \"{msg}\"")
        try:
            cmd = ["edge-tts", "--voice", voice, "--text", msg, "--write-media", str(target_file)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0 and target_file.exists():
                db.save_sample(msg, voice, target_file)
            else:
                print(f"   [!] Errore nella generazione audio con edge-tts: {res.stderr}")
                if target_file.exists():
                    target_file.unlink()
        except Exception as e:
            print(f"   [!] Eccezione: {e}")
            if target_file.exists():
                target_file.unlink()

    print("✅ Voci archiviate nel DB SQLite con successo.\n")


def play_cue(text: str, voice: str, engine: str, db: VoiceDB):
    if engine == "edge":
        sample = db.get_sample(text, voice)
        if sample:
            subprocess.run(["afplay", str(sample["file_path"])])
            db.record_play(text, voice)
            return

    # Fallback macOS say
    cmd = ["say"]
    mac_voice = "Alice" if engine == "edge" else voice
    if mac_voice:
        cmd.extend(["-v", mac_voice])
    cmd.append(text)
    subprocess.run(cmd)


class MpvController:
    def __init__(self, music_files, normal_volume=100, duck_volume=15):
        self.music_files = [str(f) for f in music_files]
        self.normal_volume = normal_volume
        self.duck_volume = duck_volume
        self.sock_path = f"/tmp/workout_mpv_{os.getpid()}_{int(time.time())}.sock"
        self.proc = None
        self.sock = None

    def start(self):
        if not self.music_files:
            raise RuntimeError("Nessun file audio specificato.")

        if os.path.exists(self.sock_path):
            os.remove(self.sock_path)

        playlist = list(self.music_files)
        random.shuffle(playlist)

        cmd = [
            "mpv",
            "--no-video",
            "--really-quiet",
            f"--input-ipc-server={self.sock_path}",
            f"--volume={self.normal_volume}",
            "--loop-playlist=inf",
            "--"
        ] + playlist

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


class TVScreen:
    """
    Gestisce il rendering a tutto schermo pulito per TV (nessun flickering).
    """
    def __init__(self):
        # Entra nel buffer alternativo del terminale e nasconde il cursore
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()

    def close(self):
        # Ripristina buffer normale e cursore
        sys.stdout.write("\033[?25h\033[?1049l\n")
        sys.stdout.flush()

    def render(self, step_idx, total_steps, current_msg, next_msg, step_rem, step_dur, total_elapsed, total_time, music_name, voice_name):
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

        total_rem = max(0, total_time - total_elapsed)

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
        # Barra di avanzamento
        bar_str = f"[{accent_color}{bar}{reset}] {bold}{pct}%{reset}"
        lines.append(bar_str.center(w + 10))
        lines.append("")

        if next_msg:
            lines.append(f"  {dim}Prossimo:{reset}  {bold}{next_msg}{reset}")
        else:
            lines.append(f"  {bold}🎉 Ultima fase dell'allenamento!{reset}")

        lines.append("─" * w)
        info_footer = (
            f" ⏱️  Trascorso: {bold}{format_seconds(total_elapsed)}{reset}  "
            f"│  Rimanente: {bold}{format_seconds(total_rem)}{reset}  "
            f"│  Totale: {bold}{format_seconds(total_time)}{reset} "
        )
        lines.append(info_footer)
        lines.append(f" {dim}Premi Ctrl+C per interrompere{reset}")

        # Stampa usando riposizionamento cursore home (\033[H) per evitare flicking
        output_buffer = "\033[H\033[2J" + "\n".join(lines)
        sys.stdout.write(output_buffer)
        sys.stdout.flush()


def run_workout(schedule, music_files, voice, engine, normal_vol, duck_vol, test_speed, db: VoiceDB):
    total_time = sum(step["duration"] for step in schedule)
    total_steps = len(schedule)

    if engine == "edge":
        pregenerate_neural_cues(schedule, voice, db)

    player = MpvController(music_files, normal_volume=normal_vol, duck_volume=duck_vol)
    player.start()

    time_elapsed_total = 0
    music_name = Path(music_files[0]).stem if music_files else "Musica"
    voice_label = voice.replace("it-IT-", "").replace("Neural", "")

    screen = TVScreen()

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
                voice_name=voice_label
            )

            player.duck()
            play_cue(msg, voice=voice, engine=engine, db=db)
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
                    voice_name=voice_label
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
            voice_name=voice_label
        )
        time.sleep(3)

    except KeyboardInterrupt:
        pass
    finally:
        screen.close()
        player.stop()
        print("\n\n👋 Sessione terminata.")



def main():
    parser = argparse.ArgumentParser(description="Workout Audio Trainer con background music, voice coach e database SQLite.")
    parser.add_argument("--scheda", "-s", default="scheda.txt", help="Percorso del file scheda (default: scheda.txt)")
    parser.add_argument("--voice", "-v", default="elsa", help="Voce: 'elsa', 'diego', 'isabella', 'giuseppe' (default: elsa)")
    parser.add_argument("--engine", choices=["edge", "macos"], default="edge", help="Motore voce: 'edge' (neurale HD, default) o 'macos' (locale say)")
    parser.add_argument("--music", "-m", help="File o cartella mp3 specifica")
    parser.add_argument("--duck-vol", type=int, default=15, help="Volume durante gli annunci (0-100, default: 15)")
    parser.add_argument("--volume", type=int, default=100, help="Volume normale della musica (0-100, default: 100)")
    parser.add_argument("--test-speed", type=float, default=1.0, help="Moltiplicatore velocità per test (es. 10)")
    parser.add_argument("--list-samples", action="store_true", help="Mostra l'archivio delle frasi salvate nel DB SQLite")
    parser.add_argument("--clean-db", action="store_true", help="Pulisce i record e file orfani dal database e dalla cache")
    args = parser.parse_args()

    db = VoiceDB()

    if args.list_samples:
        samples = db.list_samples()
        if not samples:
            print("📭 Nessun campione audio presente nel database.")
        else:
            print("\n📋 ARCHIVIO CAMPIONI AUDIO (workout_cache.db)")
            print("-" * 80)
            print(f"{'HASH':<18} | {'VOCE':<12} | {'USI':<5} | {'TESTO'}")
            print("-" * 80)
            for h, voice, play_cnt, last_used, text in samples:
                short_voice = voice.replace("it-IT-", "").replace("Neural", "")
                print(f"{h:<18} | {short_voice:<12} | {play_cnt:<5} | {text}")
            print("-" * 80)
            print(f"Totale frasi archiviate: {len(samples)}\n")
        return

    if args.clean_db:
        rem_db, rem_files = db.clean_orphans()
        print(f"🧹 Pulizia completata: rimossi {rem_db} record orfani dal DB e {rem_files} file non utilizzati.")
        return

    scheda_path = Path(args.scheda).resolve()
    if not scheda_path.exists():
        print(f"Errore: il file di scheda '{scheda_path}' non esiste.")
        sys.exit(1)

    schedule = load_schedule(scheda_path)
    if not schedule:
        print(f"Errore: nessuna riga valida trovata in '{scheda_path}'.")
        sys.exit(1)

    selected_voice = args.voice
    if args.engine == "edge":
        selected_voice = NEURAL_VOICES.get(args.voice.lower(), args.voice)

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
        sys.exit(1)

    run_workout(
        schedule=schedule,
        music_files=music_files,
        voice=selected_voice,
        engine=args.engine,
        normal_vol=args.volume,
        duck_vol=args.duck_vol,
        test_speed=args.test_speed,
        db=db
    )


if __name__ == "__main__":
    main()
