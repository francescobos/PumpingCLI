# PumpingCLI 🏋️‍♂️

Un personal trainer da terminale con timer a caratteri giganti, musica di sottofondo con audio ducking e voce guida neurale.

Metti una o più tracce musicali in una cartella, scrivi la scheda degli esercizi in un banale file di testo e lancia lo script. PumpingCLI suona la musica in sottofondo, abbassa il volume in stile DJ (audio ducking) quando è il momento di cambiare esercizio, ti dà le istruzioni con una voce neurale naturale in italiano e mostra a tutto schermo un timer a caratteri giganti leggibile dal divano o dal tappetino.

Nessun abbonamento, nessuna app mobile con notifiche o paywall, nessuna dipendenza da servizi cloud a pagamento.

---

## Come appare sullo schermo

Il rendering usa il buffer alternativo del terminale per evitare qualsiasi sfarfallio e adatta i blocchi ASCII per essere visibili da diversi metri di distanza:

```text
     🏋️‍♂️  WORKOUT TRAINER  •  Coach: Elsa  •  Traccia: Workout Music 2025    
────────────────────────────────────────────────────────────────────────────────
 FASE 2 DI 7  │  🔥 ESERCIZIO IN CORSO

  Ok! Jumping Jack! Diamo ritmo!

                        ████    ████      ██  ██  ██████                        
                       ██  ██  ██  ██  ▀  ██  ██  ██                            
                       ██  ██  ██  ██     ██████  █████                         
                       ██  ██  ██  ██  ▄      ██      ██                        
                        ████    ████          ██  █████                         

            [██████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 25%             

  Prossimo:  Pausa di 30 secondi! Respira e preparati.
────────────────────────────────────────────────────────────────────────────────
 📊  Totale workout: [████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 20%
 ⏱️  Trascorso: 02:00  │  Rimanente: 08:00  │  Totale: 10:00 
 🕒  Inizio: 18:30      │  Fine stimata: 18:40
 Premi Ctrl+C per interrompere
```

- **Verde brillante:** fase attiva di esercizio.
- **Ciano:** pause e momenti di recupero (riconosciuti automaticamente da parole come *pausa*, *recupero*, *riposo*).
- **Giallo:** conto alla rovescia negli ultimi 5 secondi per preparare il cambio.

---

## Caratteristiche

1. **Audio Ducking Hardware / Locale:**
   La musica di sottofondo viene riprodotta tramite `mpv` (controllato via IPC socket). Quando la voce guida parla, la musica sfuma istantaneamente al 15% del volume, per poi risalire dolcemente al 100% a frase conclusa.
2. **Sintesi Vocale Neurale con riserva automatica:**
   Tre motori, scelti con `--engine` o con `TTS_ENGINE` nel file `.env`:
   - **Gemini TTS** (consigliato): API ufficiale Google, piano gratuito con una API key di AI Studio. Voci preimpostate: *Chiara*, *Sofia*, *Laura*, oppure qualunque ID del catalogo Gemini. Lo stile di lettura si regola con `--style` o `GEMINI_STYLE`.
   - **Edge TTS**: voci neurali Microsoft in italiano (*Elsa*, *Diego*, *Isabella*, *Giuseppe*), senza registrazione. È un servizio non ufficiale e può smettere di rispondere senza preavviso.
   - **macOS `say`**: sintesi locale, sempre disponibile.

   Con Gemini, se la generazione fallisce due volte di fila lo script passa a Edge TTS e poi a `say`. Ogni chiamata di rete ha un timeout di 30 secondi.
3. **Archivio Hash & SQLite (`workout_cache.db`):**
   Ogni frase vocale viene generata una sola volta e salvata nella cache locale con hash SHA-256 univoco. I successivi avvii sono immediati e funzionano anche offline. Un database SQLite tiene traccia di testi, file audio e statistiche di utilizzo.
4. **Gestione tracce musicali intelligente & FlowLoop:**
   Cerca in automatico i file `.mp3` o `.m4a` nella cartella. Seleziona una traccia in modo casuale e avvia la riproduzione da un punto casuale (offset dinamico), evitando di ascoltare sempre gli stessi minuti iniziali e ripetendo in loop seamless continuo.
5. **Download da YouTube integrato:**
   Permette di scaricare qualsiasi DJ mix o traccia da YouTube tramite URL, convertendolo automaticamente in MP3 ad alta qualità con metadati incorporati, pronto per essere utilizzato come sottofondo musicale.
6. **Dashboard Full-Screen:**
   Timer gigante in caratteri ASCII `██`, barra di avanzamento grafica e anteprima dell'esercizio successivo visibile anche a distanza.

---

## Requisiti

- **macOS** (o Linux con supporto audio)
- **Python 3.9+**
- **mpv** (per il controllo del volume in tempo reale e playback)
- **yt-dlp** e **ffmpeg** (opzionali, per il download audio da YouTube)
  ```bash
  brew install mpv yt-dlp ffmpeg
  ```

---

## Installazione

1. Clona il repository:
   ```bash
   git clone https://github.com/francescobos/PumpingCLI.git
   cd PumpingCLI
   ```

2. Installa la libreria per la sintesi vocale neurale:
   ```bash
   pip install -r requirements.txt
   ```

3. Copia il file di configurazione e inserisci la tua API key di [Google AI Studio](https://aistudio.google.com/apikey) in `GEMINI_API_KEY`:
   ```bash
   cp .env.example .env
   ```
   Il file `.env` è escluso da git. Senza API key lo script userà Edge TTS.

---

## Configurazione (`.env`)

`.env.example` contiene tutte le opzioni: quelle per Gemini sono attive, le altre sono commentate. Per cambiare impostazione basta togliere il `#` dalla riga che ti serve e commentare quella attiva.

| Variabile | Valori | Default se assente |
|---|---|---|
| `TTS_ENGINE` | `gemini`, `edge`, `macos` | `edge` |
| `GEMINI_API_KEY` | API key di Google AI Studio | nessuna: si passa a Edge TTS |
| `GEMINI_VOICE` | `chiara`, `sofia`, `laura` o un ID del catalogo Gemini | `chiara` |
| `GEMINI_STYLE` | testo libero sull'intonazione; vuoto = nessuno stile | `personal trainer italiano, energico e motivante, ritmo deciso` |
| `EDGE_VOICE` | `elsa`, `isabella`, `diego`, `giuseppe` | `elsa` |

**Voci Gemini preimpostate**

| Alias | ID Gemini | Carattere |
|---|---|---|
| `chiara` | `it-it-csagent-1` | 36 anni, nitida e decisa |
| `sofia` | `it-it-tutor-7` | 25 anni, sicura e chiara |
| `laura` | `Laomedeia` | energica, tono alto |

Il catalogo completo (oltre 2.000 voci, di cui 53 native italiane) si legge con `GET https://generativelanguage.googleapis.com/v1beta/voices`; ogni ID si può usare direttamente in `GEMINI_VOICE` o con `--voice`.

**Regole da sapere**
- Le opzioni da riga di comando (`--engine`, `--voice`, `--style`) hanno la precedenza sul `.env` e valgono solo per quella sessione.
- Ogni motore ha la sua voce: `--voice` cambia quella del motore attivo.
- `EDGE_VOICE` serve anche come riserva: se Gemini fallisce due volte di fila, lo script passa a Edge TTS e poi a `say`.
- Gemini ha un limite di richieste al minuto (10 nel Tier 1): lo script distanzia le chiamate di 6,5 secondi e, se riceve comunque un errore 429, aspetta il tempo indicato dal servizio e riprova la stessa frase. Con 25 frasi nuove la prima generazione richiede circa 3 minuti; dopo, la cache rende l'avvio immediato.
- Lo stile fa parte della chiave di cache: se cambi `GEMINI_STYLE`, al primo avvio le frasi vengono rigenerate.
- I commenti vanno su righe a parte: `GEMINI_VOICE=sofia # commento` non è supportato.
- Con `TTS_ENGINE=macos` la voce si sceglie solo con `--voice` (es. `--voice Alice`).

---

## Utilizzo

### 1. Prepara la scheda (`scheda.txt`)
Crea un file di testo con il formato `durata | messaggio`:

```text
# Riscaldamento
2m   | Ok! Ora iniziamo il riscaldamento... saltelli sul posto!

# Lavoro
1m   | Ok! Jumping Jack! Diamo ritmo!
30s  | Pausa di 30 secondi! Respira e preparati per i piegamenti.
1m   | Piegamenti sulle braccia! Forza, schiena dritta!
30s  | Recupero di 30 secondi! Sciogli le braccia.
1m   | Crunch a terra! Contrai bene l'addome!

# Chiusura
1m   | Grandissimo allenamento completato! Defaticamento e stretching.
```

*I tempi possono essere espressi in minuti (`2m`), secondi (`45s`), combinati (`1m30s`) o formato orario (`1:30`).*

### 2. Aggiungi la tua musica
Metti uno o più file `.mp3` o `.m4a` nella cartella del progetto (oppure specifica un percorso con il flag `-m`).

### 3. Avvia l'allenamento
```bash
./pumping.py
```

Per la resa migliore a schermo intero:
- Metti la finestra del terminale a tutto schermo (`Cmd + Ctrl + F`).
- Se necessario, aumenta la dimensione del font con `Cmd + +`.

---

## Opzioni da riga di comando

```text
usage: pumping.py [-h] [--scheda SCHEDA] [--voice VOICE]
                  [--engine {gemini,edge,macos}] [--style STYLE]
                  [--music MUSIC] [--duck-vol DUCK_VOL] [--volume VOLUME]
                  [--test-speed TEST_SPEED] [--list-samples] [--clean-db]
                  [--download-music URL] [--title TITLE] [--from-start]
                  [--start SECONDI]

options:
  -h, --help            Mostra questo messaggio di aiuto ed esce
  --scheda, -s SCHEDA   Percorso del file scheda (default: scheda.txt)
  --voice, -v VOICE     Voce: gemini 'chiara', 'sofia', 'laura' o un ID del catalogo;
                        edge 'elsa', 'diego', 'isabella', 'giuseppe' (default da .env)
  --engine {gemini,edge,macos}
                        Motore voce (default: TTS_ENGINE in .env, altrimenti edge)
  --style STYLE         Stile di lettura per Gemini (default: GEMINI_STYLE in .env)
  --music, -m MUSIC     File o cartella mp3 specifica
  --duck-vol DUCK_VOL   Volume della musica durante gli annunci (0-100, default: 15)
  --volume VOLUME       Volume normale della musica (0-100, default: 100)
  --test-speed SPEED    Moltiplicatore velocità per collaudo rapido (es. 10)
  --list-samples        Mostra l'archivio delle frasi salvate nel DB SQLite
  --clean-db            Pulisce record e file orfani dal database e dalla cache
  --download-music, -y URL
                        Scarica audio da YouTube (URL), converte in MP3 e salva come sottofondo
  --title TITLE         Titolo personalizzato per il brano scaricato con --download-music
  --from-start          Avvia la traccia dall'inizio (00:00) invece che da un punto casuale
  --start SECONDI       Punto di inizio riproduzione specifico in secondi
```

### Esempi pratici

- **Scaricare un video/mix da YouTube per il workout:**
  ```bash
  ./pumping.py --download-music "https://www.youtube.com/watch?v=..."
  ```
  *(oppure con titolo personalizzato: `./pumping.py -y "https://..." --title "Mix Allenamento Intenso"`)*

- **Avviare l'allenamento con selezione casuale del brano e del punto di avvio:**
  ```bash
  ./pumping.py
  ```

- **Avviare l'allenamento partendo dal minuto 00:00 (senza punto casuale):**
  ```bash
  ./pumping.py --from-start
  ```

- **Cambiare voce o motore per una sola sessione:**
  ```bash
  ./pumping.py --voice sofia
  ./pumping.py --engine edge --voice diego
  ./pumping.py --style "calmo e rilassato, ritmo lento"
  ```


---

## Licenza

Distribuito sotto licenza [MIT](LICENSE).
