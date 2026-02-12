# Changelog

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
