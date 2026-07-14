# Changelog

## Non rilasciato

### Sincronizzazione stato occupato/disponibile
- Lo stato busy/free (`transparency`) viene ora propagato: le copie ereditano
  lo stato dell'originale e le modifiche si sincronizzano in entrambe le direzioni
- Un evento "disponibile" resta disponibile su tutti i calendari, un evento
  "occupato" resta occupato ovunque

### Fix: copie duplicate sul calendario di origine
- Le chain non creano più una "copia mancante" sul proprio calendario di origine
  quando l'originale non ha i metadati trisync (eventi `fromGmail` o patch dei
  metadati fallita). Questo causava duplicati tipo `[ALMA] Evento` sul calendario
  ALMA stesso
- Pulizia automatica delle self-copy già esistenti (rilevate tramite mismatch del
  chain ID + prefisso del calendario stesso). Disattivabile con
  `cleanup_self_copies: false`
- Gli eventi `fromGmail` non vengono più aggiornati via API (l'update fallirebbe
  sempre con 403)

### Fix: nota di sync sugli eventi originali
- Il tag `sync_tag_in_description` non viene più aggiunto/propagato alla
  descrizione degli eventi originali; resta solo sulle copie

### Fix: `sync_delete` in modalità incrementale
- La cancellazione delle copie ora funziona anche con `--incremental`: per gli
  eventi origine il chain ID è deterministico, quindi le copie vengono trovate
  e cancellate direttamente sugli altri calendari (prima `_handle_deleted_events`
  era un no-op e le copie restavano per sempre)

### Affidabilità
- I sync token vengono salvati solo a fine run riuscito: un crash a metà sync
  non fa più perdere definitivamente le modifiche scaricate ma non processate
- Nuovo lock anti-sovrapposizione (`SyncLock`, flag `--lock-file`, default
  `.trisync.lock`): due run concorrenti (es. cron ogni 5 minuti con un sync
  lento in corso) non possono più creare copie duplicate; la seconda istanza
  esce con un avviso

### Sicurezza
- I file token OAuth vengono salvati con permessi `0600` (solo proprietario)

## 0.3.0

### Architettura
- Refactor in architettura modulare a pacchetto (`gcal_trisync/`) con 10 moduli
- Test suite completa: 217 test su 7 file
- CI/CD con GitHub Actions

### Sync incrementale
- Sync con token Google Calendar (`events.list` con `syncToken`)
- Primo run: sync completo con salvataggio del token
- Run successivi: solo eventi modificati, molto più veloce
- Persistenza stato in `.trisync_state.json` (configurabile)
- Flag CLI: `--incremental`, `--full-sync`, `--state-file`, `--clear-state`

### Resilienza API
- Retry automatico con backoff esponenziale + jitter su errori 429, 500, 503
- Rate limiter con algoritmo token bucket (10 req/s, burst 15)
- Decoratori `@with_retry()` e `@rate_limited()` su tutte le funzioni API
- Gestione `Retry-After` header dalla risposta Google

### Dashboard metriche
- `MetricsCollector` con statistiche per calendario
- Contatori: eventi scaricati, creati, aggiornati, cancellati, saltati, errori
- Timing: durata sync, durata fetch per calendario
- Report testuale e export JSON
- Flag CLI: `--metrics`, `--metrics-json FILE`

### Gestione OAuth robusta
- `TokenManager` con ciclo di vita completo dei token OAuth2
- Refresh proattivo dei token prima della scadenza (margine configurabile)
- Diagnostica stato token: valid, expiring_soon, expired, missing, invalid, revoked
- Gestione token corrotti o revocati con messaggi chiari
- Flag CLI: `--check-auth`, `--reauth`

## 0.2.0
- Safe delete: se l'origine manca non ricrea, elimina le copie quando `sync_delete: true`
- Handling `fromGmail`
- Filtri per parole chiave e per tipo evento
- Visibilità `private` per copie (configurabile)

## 0.1.0
- Prima release pubblica
