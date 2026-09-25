# PumpingCLI 🏋️‍♂️

Un personal trainer da terminale progettato per lo schermo della TV.

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
 ⏱️  Trascorso: 02:00  │  Rimanente: 08:00  │  Totale: 10:00 
 Premi Ctrl+C per interrompere
```

- **Verde brillante:** fase attiva di esercizio.
- **Ciano:** pause e momenti di recupero (riconosciuti automaticamente da parole come *pausa*, *recupero*, *riposo*).
- **Giallo:** conto alla rovescia negli ultimi 5 secondi per preparare il cambio.

---

## Caratteristiche

1. **Audio Ducking Hardware / Locale:**
   La musica di sottofondo viene riprodotta tramite `mpv` (controllato via IPC socket). Quando la voce guida parla, la musica sfuma istantaneamente al 15% del volume, per poi risalire dolcemente al 100% a frase conclusa.
2. **Sintesi Vocale Neurale HD (Gratuita):**
   Utilizza le voci neurali Edge TTS (modelli Microsoft Azure in italiano: *Elsa*, *Diego*, *Isabella*, *Giuseppe*). Nessuna registrazione richiesta e zero costi. In alternativa è possibile usare la sintesi nativa macOS (`say`).
3. **Archivio Hash & SQLite (`workout_cache.db`):**
   Ogni frase vocale viene generata una sola volta e salvata nella cache locale con hash SHA-256 univoco. I successivi avvii sono immediati e funzionano anche offline. Un database SQLite tiene traccia di testi, file audio e statistiche di utilizzo.
4. **Gestione tracce musicali:**
   Cerca in automatico i file `.mp3` o `.m4a` nella cartella. Se sono presenti più brani, li riproduce in modalità shuffle continuo.
5. **Dashboard TV Full-Screen:**
   Timer gigante in caratteri ASCII `██`, barra di avanzamento grafica e anteprima dell'esercizio successivo.

---

## Requisiti

- **macOS** (o Linux con supporto audio)
- **Python 3.9+**
- **mpv** (per il controllo del volume in tempo reale)
  ```bash
  brew install mpv
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

Per la resa migliore sulla TV:
- Trasmetti lo schermo via AirPlay o HDMI.
- Metti la finestra del terminale a tutto schermo (`Cmd + Ctrl + F`).
- Se necessario, aumenta la dimensione del font con `Cmd + +`.

---

## Opzioni da riga di comando

```text
usage: pumping.py [-h] [--scheda SCHEDA] [--voice VOICE] [--engine {edge,macos}]
                  [--music MUSIC] [--duck-vol DUCK_VOL] [--volume VOLUME]
                  [--test-speed TEST_SPEED] [--list-samples] [--clean-db]

options:
  -h, --help            Mostra questo messaggio di aiuto ed esce
  --scheda, -s SCHEDA   Percorso del file scheda (default: scheda.txt)
  --voice, -v VOICE     Voce: 'elsa', 'diego', 'isabella', 'giuseppe' (default: elsa)
  --engine {edge,macos} Motore voce: 'edge' (neurale HD) o 'macos' (locale say)
  --music, -m MUSIC     File o cartella mp3 specifica
  --duck-vol DUCK_VOL   Volume della musica durante gli annunci (0-100, default: 15)
  --volume VOLUME       Volume normale della musica (0-100, default: 100)
  --test-speed SPEED    Moltiplicatore velocità per collaudo rapido (es. 10)
  --list-samples        Mostra l'archivio delle frasi salvate nel DB SQLite
  --clean-db            Pulisce record e file orfani dal database e dalla cache
```

---

## Licenza

Distribuito sotto licenza [MIT](LICENSE).
