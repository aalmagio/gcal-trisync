## Come contribuire

### Setup ambiente di sviluppo

```bash
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows
pip install -r requirements-dev.txt
```

### Struttura del progetto

```
gcal_trisync/
  models.py    # Dataclass core (Calendar, SyncContext)
  config.py    # Caricamento e validazione config
  utils.py     # Funzioni utility (chain ID, metadati, time window)
  api.py       # Wrapper Google Calendar API
  sync.py      # Logica di sincronizzazione
  storage.py   # Persistenza stato e sync token
  retry.py     # Retry e rate limiting
  metrics.py   # Raccolta metriche
  auth.py      # Gestione OAuth2
  cli.py       # Entry point CLI
```

### Test

```bash
# Tutti i test
python -m pytest

# Con copertura
python -m pytest --cov=gcal_trisync

# Un file specifico
python -m pytest tests/test_sync.py -v
```

I test non richiedono credenziali Google: usano mock e import dinamici per isolare le dipendenze esterne.

### Stile

- Python 3.10+, type hint dove sensato
- Linting: `ruff check .`
- Type checking: `mypy gcal_trisync/`
- PR piccole e chiare, una feature per PR
- Commit message in inglese, descrittivi

### Test manuali

1. Crea 2-3 OAuth client (Desktop) in `creds/`
2. Compila `config.yaml` (vedi `examples/config.yaml`)
3. Primo run: `python -m gcal_trisync --config config.yaml`
4. Verifica sync, update, delete (origine -> copie)
5. Prova `--incremental` per il sync con token
6. Prova `--check-auth` per verificare lo stato dei token
