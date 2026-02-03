"""
Google Calendar API wrapper for gcal_trisync.

This module handles all interactions with the Google Calendar API,
including authentication and CRUD operations.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .retry import with_retry, rate_limited, default_rate_limiter

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar']

# HTTP 410 Gone indicates sync token is invalid/expired
SYNC_TOKEN_EXPIRED_STATUS = 410


@dataclass
class SyncResult:
    """
    Result of a calendar sync operation.

    Attributes:
        events: List of events (all or changed)
        next_sync_token: Token for next incremental sync
        is_full_sync: Whether this was a full sync
        deleted_event_ids: IDs of deleted events (incremental only)
    """
    events: list[dict[str, Any]]
    next_sync_token: Optional[str] = None
    is_full_sync: bool = False
    deleted_event_ids: list[str] = None

    def __post_init__(self):
        if self.deleted_event_ids is None:
            self.deleted_event_ids = []


def ensure_dirs() -> None:
    """Create necessary directories for tokens and credentials."""
    os.makedirs('tokens', exist_ok=True)
    os.makedirs('creds', exist_ok=True)


def get_service(
    credentials_file: str,
    token_file: str,
    auth_method: str = 'local',
    login_hint: Optional[str] = None,
    port: int = 0
) -> Any:
    """
    Initialize and return Google Calendar API service.

    Args:
        credentials_file: Path to OAuth credentials JSON
        token_file: Path to store/retrieve access token
        auth_method: 'local' for browser flow, 'console' for manual code entry
        login_hint: Email to pre-fill in login
        port: Port for local OAuth server (0 for auto-select)

    Returns:
        Google Calendar API service object
    """
    creds = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_file, SCOPES, redirect_uri=None
            )

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


@with_retry()
def list_events(
    service: Any,
    calendar_id: str,
    time_min: str,
    time_max: str
) -> list[dict[str, Any]]:
    """
    List all events in a calendar within the time window.

    Handles pagination automatically to retrieve all events.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar ID to query
        time_min: Start of time window (ISO format)
        time_max: End of time window (ISO format)

    Returns:
        List of event dictionaries
    """
    events: list[dict[str, Any]] = []
    page_token: Optional[str] = None

    while True:
        default_rate_limiter.acquire()
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime',
            pageToken=page_token,
            maxResults=2500
        ).execute()

        events.extend(resp.get('items', []))
        page_token = resp.get('nextPageToken')

        if not page_token:
            break

    return events


@with_retry()
def list_events_full_sync(
    service: Any,
    calendar_id: str,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None
) -> SyncResult:
    """
    Perform a full sync of all events in a calendar.

    Returns all events and a sync token for future incremental syncs.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar ID to query
        time_min: Optional start of time window (ISO format)
        time_max: Optional end of time window (ISO format)

    Returns:
        SyncResult with all events and next sync token
    """
    events: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    next_sync_token: Optional[str] = None

    while True:
        params = {
            'calendarId': calendar_id,
            'singleEvents': True,
            'maxResults': 2500,
            'pageToken': page_token,
        }

        # Add time bounds if specified
        if time_min:
            params['timeMin'] = time_min
        if time_max:
            params['timeMax'] = time_max
        if time_min or time_max:
            params['orderBy'] = 'startTime'

        default_rate_limiter.acquire()
        resp = service.events().list(**params).execute()

        events.extend(resp.get('items', []))
        page_token = resp.get('nextPageToken')
        next_sync_token = resp.get('nextSyncToken')

        if not page_token:
            break

    logger.info(f"Full sync: retrieved {len(events)} events")

    return SyncResult(
        events=events,
        next_sync_token=next_sync_token,
        is_full_sync=True,
        deleted_event_ids=[]
    )


@with_retry()
def list_events_incremental(
    service: Any,
    calendar_id: str,
    sync_token: str
) -> SyncResult:
    """
    Perform an incremental sync using a sync token.

    Returns only events that have changed since the token was issued.
    Deleted events are returned with status='cancelled'.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar ID to query
        sync_token: Token from previous sync

    Returns:
        SyncResult with changed events and new sync token

    Raises:
        HttpError: If sync token is invalid (410 Gone) or other API error
    """
    events: list[dict[str, Any]] = []
    deleted_ids: list[str] = []
    page_token: Optional[str] = None
    next_sync_token: Optional[str] = None

    while True:
        params = {
            'calendarId': calendar_id,
            'syncToken': sync_token,
            'maxResults': 2500,
            'showDeleted': True,  # Include deleted events
        }

        if page_token:
            params['pageToken'] = page_token
            # Remove syncToken when using pageToken
            del params['syncToken']

        default_rate_limiter.acquire()
        resp = service.events().list(**params).execute()

        for event in resp.get('items', []):
            # Deleted events have status='cancelled'
            if event.get('status') == 'cancelled':
                deleted_ids.append(event['id'])
            else:
                events.append(event)

        page_token = resp.get('nextPageToken')
        next_sync_token = resp.get('nextSyncToken')

        if not page_token:
            break

    logger.info(
        f"Incremental sync: {len(events)} changed, "
        f"{len(deleted_ids)} deleted"
    )

    return SyncResult(
        events=events,
        next_sync_token=next_sync_token,
        is_full_sync=False,
        deleted_event_ids=deleted_ids
    )


def sync_events(
    service: Any,
    calendar_id: str,
    sync_token: Optional[str] = None,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None
) -> SyncResult:
    """
    Smart sync: incremental if token available, full otherwise.

    Automatically falls back to full sync if token is expired (410 Gone).

    Args:
        service: Google Calendar API service
        calendar_id: Calendar ID to query
        sync_token: Optional token from previous sync
        time_min: Start of time window for full sync (ISO format)
        time_max: End of time window for full sync (ISO format)

    Returns:
        SyncResult with events and new sync token
    """
    if sync_token:
        try:
            return list_events_incremental(service, calendar_id, sync_token)
        except HttpError as e:
            if e.resp.status == SYNC_TOKEN_EXPIRED_STATUS:
                logger.warning(
                    f"Sync token expired for {calendar_id}, "
                    "performing full sync"
                )
            else:
                raise

    # Full sync (no token or token expired)
    return list_events_full_sync(service, calendar_id, time_min, time_max)


@with_retry()
@rate_limited()
def find_event_by_chain(
    service: Any,
    calendar_id: str,
    chain_id: str
) -> Optional[dict[str, Any]]:
    """
    Find event by chain ID in private extended properties.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar to search
        chain_id: Chain ID to find

    Returns:
        Event dictionary if found, None otherwise
    """
    resp = service.events().list(
        calendarId=calendar_id,
        privateExtendedProperty=f"trisync_chain_id={chain_id}",
        maxResults=2,
        singleEvents=True
    ).execute()

    items = resp.get('items', [])
    return items[0] if items else None


@with_retry()
@rate_limited()
def create_event(
    service: Any,
    calendar_id: str,
    event_body: dict[str, Any]
) -> dict[str, Any]:
    """
    Create a new event in a calendar.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar to create event in
        event_body: Event data dictionary

    Returns:
        Created event dictionary

    Raises:
        HttpError: If API call fails
    """
    return service.events().insert(
        calendarId=calendar_id,
        body=event_body
    ).execute()


@with_retry()
@rate_limited()
def update_event(
    service: Any,
    calendar_id: str,
    event_id: str,
    event_body: dict[str, Any]
) -> dict[str, Any]:
    """
    Update an existing event.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar containing the event
        event_id: ID of event to update
        event_body: Updated event data

    Returns:
        Updated event dictionary

    Raises:
        HttpError: If API call fails
    """
    return service.events().update(
        calendarId=calendar_id,
        eventId=event_id,
        body=event_body
    ).execute()


@with_retry()
@rate_limited()
def patch_event(
    service: Any,
    calendar_id: str,
    event_id: str,
    patch_body: dict[str, Any]
) -> dict[str, Any]:
    """
    Patch specific fields of an event.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar containing the event
        event_id: ID of event to patch
        patch_body: Fields to update

    Returns:
        Updated event dictionary

    Raises:
        HttpError: If API call fails
    """
    return service.events().patch(
        calendarId=calendar_id,
        eventId=event_id,
        body=patch_body
    ).execute()


@with_retry()
@rate_limited()
def delete_event(
    service: Any,
    calendar_id: str,
    event_id: str
) -> None:
    """
    Delete an event from a calendar.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar containing the event
        event_id: ID of event to delete

    Raises:
        HttpError: If API call fails
    """
    service.events().delete(
        calendarId=calendar_id,
        eventId=event_id
    ).execute()


@with_retry()
@rate_limited()
def get_event(
    service: Any,
    calendar_id: str,
    event_id: str
) -> dict[str, Any]:
    """
    Get a single event by ID.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar containing the event
        event_id: ID of event to retrieve

    Returns:
        Event dictionary

    Raises:
        HttpError: If event not found or API call fails
    """
    return service.events().get(
        calendarId=calendar_id,
        eventId=event_id
    ).execute()
