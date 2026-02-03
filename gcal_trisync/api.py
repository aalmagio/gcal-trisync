"""
Google Calendar API wrapper for gcal_trisync.

This module handles all interactions with the Google Calendar API,
including authentication and CRUD operations.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar']


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
