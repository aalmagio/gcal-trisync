gcal_trisync — Sync bidirezionale tra 3 Google Calendar (senza costi)

1) Preparazione
- Scarica questa cartella su PC o VPS (Plesk ok).
- Python 3.10+
- `pip install -r requirements.txt`
- Metti i 3 file OAuth in `creds/` (uno per ogni account).

2) Configurazione
- Modifica `config.yaml` con i percorsi dei JSON OAuth e i calendarId (usa 'primary' se è il calendario principale).

3) Primo avvio
- `python gcal_trisync.py --config config.yaml`
- Autorizza l'accesso per ciascun account quando si apre il browser.
- I token verranno salvati in `tokens/`

4) Cron (ogni 15 min)
*/15 * * * * /usr/bin/python3 /percorso/gcal_trisync/gcal_trisync.py --config /percorso/gcal_trisync/config.yaml >> /percorso/gcal_trisync/sync.log 2>&1

Note tecniche
- Prefisso nel titolo: [PERS]/[WORK]/[ASSOC]
- Metadati privati per evitare loop: trisync=1, trisync_chain_id, trisync_origin
- L'evento più aggiornato (campo 'updated') vince e viene propagato agli altri due.
- Di default non elimina le copie quando cancelli un evento (si può aggiungere dopo con cautela).


Aggiornamenti:
- Filtri per titolo: modifica `ignore_if_summary_contains` in config.yaml (es. ['compleanno']).
- Skip eventi con prefissi noti nel titolo (evita ping-pong): `skip_if_title_has_known_prefix: true`.
- Cancellazione sicura attiva: se l'evento scompare nel calendario origine, le copie vengono eliminate.
