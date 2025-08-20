#!/usr/bin/env python3
# gcal_trisync.py — Bidirectional sync for Google Calendars
# Features: filters, prefix-skip, safe delete, auth console/local with login_hint and port
# fromGmail handling: do not patch source; optional exclusion via config
# License: MIT

import os
import sys
import json
import yaml
import hashlib
from datetime import datetime, timedelta, timezone
from dateutil.parser import isoparse

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ['https://www.googleapis.com/auth/calendar']

def load_config(path):
    with open(path, 'r', encoding='utf-8') as f:
        if path.endswith('.yaml') or path.endswith('.yml'):
            return yaml.safe_load(f)
        return json.load(f)

def ensure_dirs():
    os.makedirs('tokens', exist_ok=True)
    os.makedirs('creds', exist_ok=True)

def get_service(credentials_file, token_file, auth_method='local', login_hint=None, port=0):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES, redirect_uri=None)
            if auth_method == 'console':
                auth_url, _ = flow.authorization_url(
                    access_type='offline',
                    include_granted_scopes='true',
                    prompt='consent',
                    login_hint=login_hint
                )
                print("\nOpen this URL in an incognito window and paste the code here:\n")
                print(auth_url)
                code = input("\nCode: ").strip()
                flow.fetch_token(code=code)
                creds = flow.credentials
            else:
                creds = flow.run_local_server(
                    port=port,
                    prompt='consent',
                    authorization_prompt_message=None,
                    login_hint=login_hint
                )
        with open(token_file, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds, cache_discovery=False)

def iso(dt: datetime):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

def canonical_event_dict(e):
    return {
        'summary': e.get('summary', ''),
        'location': e.get('location', ''),
        'description': e.get('description', ''),
        'start': e.get('start', {}),
        'end': e.get('end', {}),
    }

def compute_chain_id(origin_calendar_name, event_id):
    h = hashlib.sha1(f"{origin_calendar_name}:{event_id}".encode('utf-8')).hexdigest()
    return h

def get_private_meta(ev):
    return (ev.get('extendedProperties', {}) or {}).get('private', {}) or {}

def set_private_meta(ev, d):
    ep = ev.get('extendedProperties', {}) or {}
    priv = ep.get('private', {}) or {}
    priv.update(d)
    ep['private'] = priv
    ev['extendedProperties'] = ep

def title_with_origin(prefix_enabled, origin_name, title):
    if not prefix_enabled:
        return title or ''
    prefix = f"[{origin_name}] "
    t = title or ''
    if t.startswith(prefix):
        return t
    return prefix + t

def get_time_window(cfg):
    now = datetime.now(timezone.utc)
    tmin = now - timedelta(days=int(cfg.get('window_days_past', 30)))
    tmax = now + timedelta(days=int(cfg.get('window_days_future', 365)))
    return iso(tmin), iso(tmax)

def list_events(svc, calendar_id, time_min, time_max):
    events = []
    page_token = None
    while True:
        resp = svc.events().list(calendarId=calendar_id,
                                 timeMin=time_min,
                                 timeMax=time_max,
                                 singleEvents=True,
                                 orderBy='startTime',
                                 pageToken=page_token,
                                 maxResults=2500).execute()
        events.extend(resp.get('items', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return events

def find_event_by_chain(svc, calendar_id, chain_id):
    resp = svc.events().list(
        calendarId=calendar_id,
        privateExtendedProperty=f"trisync_chain_id={chain_id}",
        maxResults=2,
        singleEvents=True
    ).execute()
    items = resp.get('items', [])
    return items[0] if items else None

def create_copy(svc, target_calendar_id, source_event, cfg, origin_name, chain_id):
    clone = {
        'summary': title_with_origin(cfg.get('prefix_origin_in_title', True),
                                     origin_name,
                                     source_event.get('summary', '')),
        'location': source_event.get('location', ''),
        'description': add_sync_note(source_event.get('description', ''),
                                     cfg.get('sync_tag_in_description', '')),
        'start': source_event.get('start', {}),
        'end': source_event.get('end', {}),
        #'visibility': source_event.get('visibility', 'default'),
        'visibility': source_event.get('visibility', 'default'),  # placeholder; verrà sovrascritto da chi chiama
        'reminders': source_event.get('reminders', {'useDefault': True}),
    }
    set_private_meta(clone, {
        'trisync': '1',
        'trisync_chain_id': chain_id,
        'trisync_origin': origin_name,
    })
    # NOTA: la visibilità finale verrà forzata dal chiamante in base alla config
    return svc.events().insert(calendarId=target_calendar_id, body=clone).execute()

def add_sync_note(desc, note):
    desc = desc or ''
    note = note or ''
    if note and note not in desc:
        if desc.strip():
            return desc + "\n\n" + note
        else:
            return note
    return desc

def update_if_diff(svc, calendar_id, existing, source_like, cfg):
    changed = False
    ex = canonical_event_dict(existing)
    sr = canonical_event_dict(source_like)
    desired = dict(existing)
    for k in ['location', 'description', 'start', 'end']:
        if ex.get(k) != sr.get(k):
            desired[k] = sr.get(k)
            changed = True
    desired['description'] = add_sync_note(desired.get('description', ''),
                                           cfg.get('sync_tag_in_description', ''))
    if changed:
        try:
            return svc.events().update(
                calendarId=calendar_id,
                eventId=existing['id'],
                body=desired
            ).execute(), True
        except HttpError as e:
            print(f"Update failed on {calendar_id}: {e}", file=sys.stderr)
            return existing, False
    return existing, False

def should_skip_event(ev, cfg, known_prefixes):
    title = (ev.get('summary') or '').strip()
    # Skip by keyword
    for kw in cfg.get('ignore_if_summary_contains', []):
        if kw and kw.lower() in title.lower():
            return True
    # Skip by type (fromGmail etc.)
    ignore_types = set((cfg.get('ignore_event_types') or []))
    ev_type = (ev.get('eventType') or '').strip()
    if ev_type and ev_type in ignore_types:
        return True
    # Skip if already prefixed
    if cfg.get('skip_if_title_has_known_prefix', True):
        for px in known_prefixes:
            if title.startswith(px):
                return True
    return False

def perform_safe_delete(calendars, chain_id, items, cfg):
    """
    Return True if origin is missing: in that case DO NOT recreate anything.
    If sync_delete=true then delete copies as well.
    """
    origin_name = None
    for (name, ev) in items:
        priv = get_private_meta(ev)
        if 'trisync_origin' in priv:
            origin_name = priv['trisync_origin']
            break
    if not origin_name:
        return False
    origin_present = any(name == origin_name for (name, _ev) in items)
    if origin_present:
        return False
    # Origin missing
    if not cfg.get('sync_delete', False):
        return True
    # Delete synced copies
    for (name, ev) in items:
        if name == origin_name:
            continue
        try:
            svc = next(c for c in calendars if c['name']==name)['service']
            cal_id = next(c for c in calendars if c['name']==name)['calendar_id']
            priv = get_private_meta(ev)
            if priv.get('trisync') == '1' and priv.get('trisync_chain_id') == chain_id:
                svc.events().delete(calendarId=cal_id, eventId=ev['id']).execute()
                print(f"[chain {chain_id[:6]}] Deleted in {name} (origin missing)")
        except HttpError as e:
            print(f"Delete failed on {name}: {e}", file=sys.stderr)
    return True

def desired_copy_visibility_for(cal_cfg, global_cfg):
    # Per-calendario: se presente usa quella
    v = cal_cfg.get('copy_visibility')
    if v in ('default', 'private', 'public', 'confidential'):
        return v
    # Globale: se presente
    v = (global_cfg or {}).get('default_copy_visibility')
    if v in ('default', 'private', 'public', 'confidential'):
        return v
    # Fallback: private
    return 'private'

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sync three Google Calendars with safe delete.")
    parser.add_argument('--config', required=True, help='Path to config.yaml or config.json')
    parser.add_argument('--auth', choices=['local','console'], default='local')
    parser.add_argument('--login-hint', dest='login_hint', default=None)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()

    cfg = load_config(args.config)
    ensure_dirs()
    time_min, time_max = get_time_window(cfg)

    calendars = []
    for c in cfg['calendars']:
        svc = get_service(c['credentials_file'], c['token_file'],
                          auth_method=args.auth,
                          login_hint=args.login_hint,
                          port=args.port)
        calendars.append({**c, 'service': svc})

    known_prefixes = [f"[{c['name']}] " for c in calendars]

    cal_events = {}
    for c in calendars:
        evs = list_events(c['service'], c['calendar_id'], time_min, time_max)
        cal_events[c['name']] = evs
        print(f"{c['name']}: found {len(evs)} events.")

    chain_map = {}
    unsynced = []

    for c in calendars:
        name = c['name']
        for ev in cal_events[name]:
            priv = get_private_meta(ev)
            if priv.get('trisync') == '1' and 'trisync_chain_id' in priv:
                chain_id = priv['trisync_chain_id']
                chain_map.setdefault(chain_id, []).append((name, ev))
            else:
                if should_skip_event(ev, cfg, known_prefixes):
                    continue
                unsynced.append((name, ev))

    for (src_name, ev) in unsynced:
        chain_id = compute_chain_id(src_name, ev['id'])
        try:
            svc_src = next(c for c in calendars if c['name'] == src_name)['service']
            cal_id_src = next(c for c in calendars if c['name'] == src_name)['calendar_id']
            if (ev.get('eventType') or '') != 'fromGmail':
                set_private_meta(ev, {
                    'trisync': '1',
                    'trisync_chain_id': chain_id,
                    'trisync_origin': src_name
                })
                svc_src.events().patch(
                    calendarId=cal_id_src,
                    eventId=ev['id'],
                    body={'extendedProperties': {'private': get_private_meta(ev)}}
                ).execute()
            else:
                print(f"Note: 'fromGmail' event on {src_name}, skipping patch")
        except Exception as e:
            print(f"Warning: cannot save meta on {src_name}: {e}", file=sys.stderr)

        for c in calendars:
            if c['name'] == src_name:
                continue
            found = find_event_by_chain(c['service'], c['calendar_id'], chain_id)
            if not found:
                try:
                    #create_copy(c['service'], c['calendar_id'], ev, cfg, src_name, chain_id)
                    #print(f"Created in {c['name']} -> copy of '{ev.get('summary','')}'")
                    copied = create_copy(c['service'], c['calendar_id'], ev, cfg, src_name, chain_id)
                    # Forza visibilità della copia secondo config
                    vis = desired_copy_visibility_for(c, cfg)
                    if copied.get('visibility') != vis:
                        copied['visibility'] = vis
                        copied = c['service'].events().update(
                            calendarId=c['calendar_id'],
                            eventId=copied['id'],
                            body=copied
                        ).execute()
                    print(f"Creato in {c['name']} -> copia di '{(ev.get('summary','') or '')[:40]}'")
                except HttpError as e:
                    print(f"Insert failed on {c['name']}: {e}", file=sys.stderr)

        chain_map.setdefault(chain_id, []).append((src_name, ev))

    for chain_id, items in chain_map.items():
        refreshed = []
        for (name, ev) in items:
            svc = next(c for c in calendars if c['name']==name)['service']
            cal_id = next(c for c in calendars if c['name']==name)['calendar_id']
            try:
                ev_ref = svc.events().get(calendarId=cal_id, eventId=ev['id']).execute()
            except HttpError:
                ev_ref = ev
            refreshed.append((name, ev_ref))
        items = refreshed

        if perform_safe_delete(calendars, chain_id, items, cfg):
            continue

        if items:
            def upd_ts(ev):
                return isoparse(ev.get('updated', '1970-01-01T00:00:00Z'))
            source_name, source_event = max(items, key=lambda t: upd_ts(t[1]))
            model = canonical_event_dict(source_event)
            for c in calendars:
                target_ev = find_event_by_chain(c['service'], c['calendar_id'], chain_id)
                if target_ev is None:
                    try:
                        """create_copy(c['service'], c['calendar_id'], source_event,
                                    cfg,
                                    get_private_meta(source_event).get('trisync_origin', source_name),
                                    chain_id)
                        print(f"[chain {chain_id[:6]}] Created in {c['name']}")
                        """
                        copied = create_copy(c['service'], c['calendar_id'], source_event, cfg, get_private_meta(source_event).get('trisync_origin', source_name), chain_id)
                        vis = desired_copy_visibility_for(c, cfg)
                        if copied.get('visibility') != vis:
                            copied['visibility'] = vis
                            copied = c['service'].events().update(
                                calendarId=c['calendar_id'],
                                eventId=copied['id'],
                                body=copied
                            ).execute()
                        print(f"[chain {chain_id[:6]}] Creato mancante in {c['name']}")
                    except HttpError as e:
                        print(f"Insert failed on {c['name']}: {e}", file=sys.stderr)
                else:
                    _, changed = updated_ev, changed = update_if_diff(c['service'], c['calendar_id'], target_ev, model, cfg)
                    # Enforce visibilità configurata anche dopo update
                    vis = desired_copy_visibility_for(c, cfg)
                    if updated_ev.get('visibility') != vis:
                        updated_ev['visibility'] = vis
                        updated_ev = c['service'].events().update(
                            calendarId=c['calendar_id'],
                            eventId=updated_ev['id'],
                            body=updated_ev
                        ).execute()
                        changed = True
                    if changed:
                        print(f"[chain {chain_id[:6]}] Aggiornato in {c['name']}")
    print("Done.")

if __name__ == '__main__':
    main()
