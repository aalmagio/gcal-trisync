# gcal-trisync

Sincronizza **bidirezionalmente** due o tre Google Calendar, mantenendo tutto in locale senza server pubblici.

> Non è un prodotto Google. Usa le API ufficiali di Google Calendar lato client e conserva i token **solo in locale**.

## Funzionalità

- **Sync bidirezionale** tra 2-3 calendari Google (anche di account diversi)
- **Prefisso d'origine** nel titolo (`[WORK]`, `[PERS]`, `[ASSOC]`, ...)
- **Metadati privati** per evitare loop di sync (chain ID con SHA-256)
- **Cancellazione sicura** — se l'evento sparisce nell'origine, elimina le copie
- **Filtri** per parole chiave e tipi evento (es. `fromGmail`)
- **Visibilità configurabile** per le copie (private, public, confidential)
- **Sync incrementale** con sync token per sync veloci dopo il primo run
- **Retry automatico** con backoff esponenziale + jitter su errori API
- **Rate limiting** con algoritmo token bucket per rispettare le quote Google
- **Dashboard metriche** con statistiche per calendario e export JSON
- **Gestione OAuth robusta** con refresh proattivo dei token e diagnostica

## Requisiti

- Python **3.10+**
- Google Calendar API abilitata su [Google Cloud Console](https://console.cloud.google.com/)
- Credenziali OAuth **"Desktop app"** (una per ogni account da sincronizzare)

## Installazione

```bash
git clone https://github.com/aalmagio/gcal-trisync.git
cd gcal-trisync
pip install -r requirements.txt
```

Per lo sviluppo (test, linting, type checking):

```bash
pip install -r requirements-dev.txt
```

## Setup iniziale

### 1. Credenziali OAuth

Per ogni account Google Calendar che vuoi sincronizzare:

1. Vai su [Google Cloud Console](https://console.cloud.google.com/) > API & Services > Credentials
2. Crea un OAuth Client ID di tipo **Desktop application**
3. Scarica il file JSON e salvalo in `creds/` (es. `creds/work_oauth_client.json`)

### 2. Configurazione

Copia l'esempio e personalizzalo:

```bash
cp examples/config.yaml config.yaml
```

Struttura minima di `config.yaml`:

```yaml
# Finestra temporale
window_days_past: 30
window_days_future: 365

# Prefisso nel titolo degli eventi sincronizzati
prefix_origin_in_title: true

# Tag nella descrizione
sync_tag_in_description: "Sincronizzato da gcal_trisync"

# Filtri
ignore_if_summary_contains:
  - "compleanno"
ignore_event_types: []
skip_if_title_has_known_prefix: true

# Cancellazione e visibilità
sync_delete: true
default_copy_visibility: private

# Calendari (minimo 2)
calendars:
  - name: WORK
    calendar_id: primary
    credentials_file: creds/work_oauth_client.json
    token_file: tokens/work.token.json

  - name: PERS
    calendar_id: primary
    credentials_file: creds/personal_oauth_client.json
    token_file: tokens/personal.token.json

  # Opzionale: terzo calendario
  # - name: ASSOC
  #   calendar_id: id_calendario@group.calendar.google.com
  #   credentials_file: creds/assoc_oauth_client.json
  #   token_file: tokens/assoc.token.json
  #   copy_visibility: public
```

### 3. Primo avvio

```bash
python -m gcal_trisync --config config.yaml
```

Si aprirà il browser per l'autorizzazione OAuth di ogni account. I token vengono salvati nella cartella `tokens/`.

Per ambienti senza browser (SSH, server headless):

```bash
python -m gcal_trisync --config config.yaml --auth console
```

## Utilizzo

### Interfaccia grafica Windows

Puoi avviare una GUI semplice per configurare i calendari e lanciare sync, dry-run, controllo autenticazione e reset dello stato:

```bash
python -m gcal_trisync.gui
```

Se il pacchetto e installato come script:

```bash
gcal-trisync-gui
```

La finestra salva `config.yaml`, usa la stessa logica della CLI e mostra il log del comando in tempo reale. Il primo avvio OAuth apre il browser come nella versione a riga di comando.

La GUI e organizzata in tab:

- `Calendari`: aggiunta, modifica e rimozione dei calendari da sincronizzare
- `Opzioni`: finestra temporale, visibilita, prefisso origine, cancellazione sicura e tipi evento da ignorare
- `Parole ignorate`: elenco modificabile delle parole chiave da saltare
- `Esecuzione`: sync manuale, dry-run, auth, reset stato e log
- `Scheduler`: installa o rimuove un'attivita di Windows Task Scheduler per eseguire il sync a intervalli regolari

Lo scheduler usa Windows Task Scheduler invece di un timer interno: il sync continua a partire anche se la GUI e chiusa.

### Creare l'eseguibile Windows

Per generare un singolo `.exe` della GUI:

```bash
python -m pip install -r requirements-dev.txt
python -m PyInstaller gcal_trisync_gui.spec
```

L'eseguibile viene creato in `dist/gcal-trisync.exe`.

### Creare l'installer Windows

L'installer usa Inno Setup e installa l'app per l'utente corrente, senza richiedere privilegi admin:

```powershell
.\scripts\build_installer.ps1
```

Se `ISCC.exe` non e nel `PATH`, passa il percorso esplicito:

```powershell
.\scripts\build_installer.ps1 -IsccPath "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
```

Il setup viene creato in `dist/installer/`.

### Sync completo (legacy)

```bash
python -m gcal_trisync --config config.yaml
```

Scarica tutti gli eventi nella finestra temporale e sincronizza. Funzionale ma lento su calendari grandi.

### Sync incrementale (consigliato)

```bash
python -m gcal_trisync --config config.yaml --incremental
```

Dopo il primo run completo, scarica solo gli eventi modificati. Molto più veloce.

```bash
# Forzare un sync completo anche con token salvati
python -m gcal_trisync --config config.yaml --incremental --full-sync

# Usare un file di stato personalizzato
python -m gcal_trisync --config config.yaml --incremental --state-file my_state.json

# Cancellare lo stato salvato
python -m gcal_trisync --config config.yaml --clear-state
```

### Dry run

```bash
python -m gcal_trisync --config config.yaml --dry-run
```

Simula il sync senza fare modifiche. Utile per verificare cosa verrebbe creato/aggiornato/cancellato.

### Metriche e monitoraggio

```bash
# Mostra report metriche a fine sync
python -m gcal_trisync --config config.yaml --incremental --metrics

# Esporta metriche in JSON
python -m gcal_trisync --config config.yaml --incremental --metrics-json metrics.json
```

Il report include per ogni calendario: eventi scaricati, creati, aggiornati, cancellati, saltati, errori e tipo di sync. I totali aggregano anche durata, chiamate API e retry.

### Gestione autenticazione

```bash
# Verifica lo stato dei token di tutti gli account
python -m gcal_trisync --config config.yaml --check-auth

# Forza la ri-autenticazione (utile se un token è stato revocato)
python -m gcal_trisync --config config.yaml --reauth
```

Lo stato dei token può essere: `valid`, `expiring_soon`, `expired`, `missing`, `invalid`, `revoked`.

### Tutte le opzioni CLI

| Flag | Descrizione |
|---|---|
| `--config FILE` | Path al file di configurazione (obbligatorio) |
| `--auth {local,console}` | Metodo di autenticazione OAuth (default: `local`) |
| `--login-hint EMAIL` | Email da pre-compilare nel login OAuth |
| `--port N` | Porta per il server OAuth locale (default: auto) |
| `--dry-run` | Simula senza modifiche |
| `--verbose`, `-v` | Log dettagliato (DEBUG) |
| `--version` | Mostra la versione |
| `--incremental` | Sync incrementale con sync token |
| `--full-sync` | Forza sync completo (con `--incremental`) |
| `--state-file FILE` | File di stato per i sync token (default: `.trisync_state.json`) |
| `--clear-state` | Cancella lo stato salvato ed esci |
| `--metrics` | Mostra report metriche dopo il sync |
| `--metrics-json FILE` | Salva metriche in file JSON |
| `--check-auth` | Verifica stato autenticazione ed esci |
| `--reauth` | Forza ri-autenticazione di tutti gli account |

## Automazione con cron

Esempio di cron job ogni 5 minuti con sync incrementale:

```cron
*/5 * * * * /usr/bin/python3 /path/to/gcal-trisync/gcal_trisync/__main__.py --config /path/to/config.yaml --incremental >> /path/to/sync.log 2>&1
```

Oppure con l'invocazione a modulo:

```cron
*/5 * * * * cd /path/to/gcal-trisync && /path/to/.venv/bin/python -m gcal_trisync --config config.yaml --incremental >> sync.log 2>&1
```

## Configurazione dettagliata

### Opzioni globali

| Chiave | Tipo | Default | Descrizione |
|---|---|---|---|
| `window_days_past` | int | `30` | Giorni nel passato da sincronizzare |
| `window_days_future` | int | `365` | Giorni nel futuro da sincronizzare |
| `prefix_origin_in_title` | bool | `true` | Aggiunge `[NOME]` al titolo delle copie |
| `sync_tag_in_description` | string | `""` | Nota aggiunta alla descrizione delle copie |
| `ignore_if_summary_contains` | list | `[]` | Parole chiave per saltare eventi (case-insensitive) |
| `ignore_event_types` | list | `[]` | Tipi evento da ignorare (es. `fromGmail`) |
| `skip_if_title_has_known_prefix` | bool | `true` | Salta eventi con prefisso di un altro calendario |
| `sync_delete` | bool | `false` | Cancella le copie quando l'originale sparisce |
| `default_copy_visibility` | string | `"private"` | Visibilità delle copie: `default`, `private`, `public`, `confidential` |

### Opzioni per calendario

| Chiave | Obbligatorio | Descrizione |
|---|---|---|
| `name` | Si | Identificativo unico (usato nei prefissi) |
| `calendar_id` | Si | ID Google Calendar (`primary` per il principale) |
| `credentials_file` | Si | Path al file OAuth client secrets |
| `token_file` | Si | Path dove salvare il token di accesso |
| `copy_visibility` | No | Override della visibilità per le copie verso questo calendario |

## Architettura

Il progetto è organizzato in moduli:

```
gcal_trisync/
  __init__.py    # Export pubblici e versione
  __main__.py    # Entry point: python -m gcal_trisync
  models.py      # Dataclass: Calendar, SyncContext
  config.py      # Caricamento e validazione config YAML/JSON
  utils.py       # Utility: chain ID, time window, prefissi, metadati
  api.py         # Wrapper Google Calendar API con retry e rate limiting
  sync.py        # Logica di sincronizzazione (full e incremental)
  storage.py     # Persistenza stato e sync token
  retry.py       # Retry con backoff esponenziale, rate limiter token bucket
  metrics.py     # Raccolta metriche per calendario, report, export JSON
  auth.py        # Gestione OAuth2: TokenManager, refresh proattivo, diagnostica
  cli.py         # Parsing argomenti e orchestrazione
```

### Come funziona il sync

1. **Fetch** — Scarica eventi da ogni calendario (full o incrementale con sync token)
2. **Chain map** — Raggruppa eventi collegati tramite `trisync_chain_id` (SHA-256 di calendario+eventId)
3. **Unsynced** — Identifica eventi nuovi non ancora sincronizzati
4. **Propagazione** — Crea copie nei calendari di destinazione con prefisso e metadati
5. **Aggiornamento** — Per le chain esistenti, propaga le modifiche (l'evento più recente vince)
6. **Cancellazione sicura** — Se l'evento origine manca e `sync_delete: true`, elimina le copie

### Resilienza API

- **Retry automatico** su errori 429 (rate limit), 500, 503 con backoff esponenziale + jitter
- **Rate limiter** con algoritmo token bucket (10 req/s, burst 15) per non superare le quote
- **Refresh token proattivo** — il token viene rinnovato 5 minuti prima della scadenza

## Test

```bash
# Tutti i test
python -m pytest

# Con copertura
python -m pytest --cov=gcal_trisync

# Verbose
python -m pytest -v

# Solo un modulo specifico
python -m pytest tests/test_sync.py
```

Suite di test: **217 test** su 7 file, copertura dei moduli core senza dipendenze esterne Google.

## Struttura file

```
gcal-trisync/
  config.yaml              # La tua configurazione (da creare)
  creds/                   # Credenziali OAuth (non committare!)
    work_oauth_client.json
    personal_oauth_client.json
  tokens/                  # Token di accesso (non committare!)
    work.token.json
    personal.token.json
  .trisync_state.json      # Stato sync token (generato automaticamente)
  examples/
    config.yaml            # Esempio di configurazione
  gcal_trisync/            # Codice sorgente
  tests/                   # Test suite
```

## Sicurezza

- I token OAuth e le credenziali sono salvati **solo in locale**
- Non committare mai `creds/`, `tokens/` o file `.token.json` nel repository
- Il progetto usa le API ufficiali Google Calendar — l'utente è responsabile della protezione delle proprie chiavi

## Riconoscimenti

Questo progetto è stato sviluppato da Alberto Almagioni con il supporto di GPT-5 Thinking (ChatGPT) per design e pair-programming.

## Licenza

MIT — vedi [LICENSE](LICENSE) per i dettagli.
