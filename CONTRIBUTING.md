## Setup
python -m venv .venv
. .venv/Scripts/activate   # Windows
pip install -r requirements.txt

## Test manuale
1) Crea 2-3 OAuth client (Desktop) in creds/
2) Compila config.yaml
3) Primo run: --auth console con --login-hint
4) Verifica sync, update, delete (origine → copie)

## Stile
- Python 3.10+, tipizziamo dove sensato.
- PR piccole e chiare, una feature per PR.
