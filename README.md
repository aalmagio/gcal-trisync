# gcal-trisync

Sincronizza **bidirezionalmente** (due o tre) Google Calendar, con:
- **prefisso d’origine** nel titolo (`[ALMA]`, `[ASSO]`, …);
- **metadati** per evitare loop;
- **cancellazione sicura** (se l’evento sparisce nell’origine, elimina le copie e non le ricrea);
- **filtri** (parole chiave, tipi evento come `fromGmail`);
- **visibilità private** per le copie (configurabile);
- **OAuth locale** via browser o console con `--login-hint`.

> Non è un prodotto Google. Usa le API ufficiali di Google Calendar lato client e conserva i token **solo in locale**.

---

## Requisiti
- Python **3.10+**
- Google Calendar API abilitata su **Google Cloud Console**
- Credenziali OAuth **“Desktop app”** (una per ogni account da sincronizzare)

```bash
pip install -r requirements.txt
