gcal_trisync — Sync bidirezionale tra 2-3 Google Calendar (senza costi)

1) Preparazione
- Scarica questa cartella su PC o VPS.
- Python 3.10+
- pip install -r requirements.txt
- Metti i file OAuth (Desktop app) in creds/ (uno per ogni account).

2) Configurazione
- Copia examples/config.yaml in config.yaml
- Modifica con i percorsi dei JSON OAuth e i calendarId
  (usa 'primary' se e' il calendario principale).
- Vedi examples/config.yaml per tutte le opzioni disponibili.

3) Primo avvio
- python -m gcal_trisync --config config.yaml
- Autorizza l'accesso per ciascun account quando si apre il browser.
- I token verranno salvati in tokens/
- Su server senza browser: --auth console

4) Uso quotidiano (sync incrementale, consigliato)
- python -m gcal_trisync --config config.yaml --incremental
- Il primo run scarica tutto, i successivi solo le modifiche.

5) Cron (ogni 5 min con sync incrementale)
*/5 * * * * cd /percorso/gcal-trisync && /percorso/.venv/bin/python -m gcal_trisync --config config.yaml --incremental >> sync.log 2>&1

6) Comandi utili
- Dry run (simula senza modifiche):
  python -m gcal_trisync --config config.yaml --dry-run
- Verifica stato token OAuth:
  python -m gcal_trisync --config config.yaml --check-auth
- Forza ri-autenticazione:
  python -m gcal_trisync --config config.yaml --reauth
- Mostra metriche sync:
  python -m gcal_trisync --config config.yaml --incremental --metrics
- Esporta metriche in JSON:
  python -m gcal_trisync --config config.yaml --incremental --metrics-json metrics.json
- Forza sync completo:
  python -m gcal_trisync --config config.yaml --incremental --full-sync
- Cancella stato salvato:
  python -m gcal_trisync --config config.yaml --clear-state

Note tecniche
- Prefisso nel titolo: [PERS]/[WORK]/[ASSOC]
- Metadati privati per evitare loop: trisync=1, trisync_chain_id, trisync_origin
- Chain ID calcolato con SHA-256 di (calendario + eventId)
- L'evento piu' aggiornato (campo 'updated') vince e viene propagato agli altri.
- Cancellazione sicura: se l'evento scompare nell'origine e sync_delete: true,
  le copie vengono eliminate.
- Retry automatico su errori API (429, 500, 503) con backoff esponenziale.
- Rate limiting con token bucket (10 req/s) per rispettare le quote Google.
- Refresh proattivo dei token OAuth 5 minuti prima della scadenza.

Opzioni di configurazione principali (config.yaml)
- window_days_past / window_days_future: finestra temporale
- prefix_origin_in_title: aggiunge [NOME] al titolo delle copie
- sync_tag_in_description: nota nella descrizione delle copie
- ignore_if_summary_contains: parole chiave per saltare eventi
- ignore_event_types: tipi evento da ignorare (es. fromGmail)
- skip_if_title_has_known_prefix: evita loop di sync
- sync_delete: cancella copie quando l'originale sparisce
- default_copy_visibility: visibilita' delle copie (private, public, ...)
- calendars: lista dei calendari (name, calendar_id, credentials_file, token_file)
