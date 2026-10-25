"""Short, atomic host reservations shared by all CLI processes and runs.

The network adapter receives ``limit`` as an injected callback. A request keeps
only an expiring token while on the wire, never an open database transaction.
"""
from datetime import timedelta
import math
from urllib.parse import urlsplit

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert

from . import transaction, uid
from .models import HostLimit, RobotsCache

LEASE_SECONDS = 90  # Above the adapter's 30-second physical request deadline.
ROBOTS_CACHE_SECONDS = 3600


def _robots(event, payload):
    origin = payload['origin']
    parsed = urlsplit(origin)
    if (not isinstance(origin, str) or len(origin) > 512 or parsed.scheme not in {'http', 'https'}
            or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('Invalid robots cache origin')
    with transaction() as session:
        now = session.scalar(text('SELECT clock_timestamp()'))
        if event == 'robots_get':
            row = session.get(RobotsCache, origin)
            if row is None:
                return None
            if row.expires_at <= now:
                session.delete(row)
                return None
            return {'status': row.status, 'rules_text': row.rules_text,
                    'ttl_seconds': min(ROBOTS_CACHE_SECONDS, (row.expires_at - now).total_seconds())}
        status, rules = payload.get('status'), payload.get('rules_text')
        if status not in {200, 404, 410} or (status == 200 and (
                not isinstance(rules, str) or len(rules) > 256 * 1024)):
            raise ValueError('Unverified robots response cannot be cached')
        if status != 200:
            rules = None
        values = dict(origin=origin, status=status, rules_text=rules, fetched_at=now,
                      expires_at=now + timedelta(seconds=ROBOTS_CACHE_SECONDS))
        session.execute(delete(RobotsCache).where(RobotsCache.expires_at <= now))
        session.execute(insert(RobotsCache).values(**values).on_conflict_do_update(
            index_elements=['origin'], set_={key: value for key, value in values.items() if key != 'origin'}))
        crawl_delay = max(0, float(payload.get('crawl_delay', 0)))
        if not math.isfinite(crawl_delay):
            raise ValueError('Invalid robots crawl delay')
        # Another process may have read a cache miss while robots were in flight.
        # Make the newly learned delay apply to its pending claim as well.
        host = session.get(HostLimit, parsed.hostname, with_for_update=True)
        if host and host.last_started_at and crawl_delay:
            due = host.last_started_at + timedelta(seconds=crawl_delay)
            host.next_allowed_at = max(host.next_allowed_at or now, due)
        return {'cached': True}


def limit(event, payload):
    if event in {'robots_get', 'robots_put'}:
        return _robots(event, payload)
    host = payload['host']
    if not isinstance(host, str) or not 1 <= len(host) <= 253:
        raise ValueError('Invalid rate-limit host')
    if event not in {'claim', 'dispatch', 'release'}:
        raise ValueError('Invalid rate-limit event')
    with transaction() as session:
        session.execute(insert(HostLimit).values(host=host).on_conflict_do_nothing(index_elements=['host']))
        row = session.get(HostLimit, host, with_for_update=True)
        # Use database wall time after acquiring the row lock, across processes.
        now = session.scalar(text('SELECT clock_timestamp()'))
        if event == 'release':
            seconds = payload.get('retry_after_s')
            if seconds is not None:
                until = now + timedelta(seconds=max(1, float(seconds)))
                row.blocked_until = max(row.blocked_until or now, until)
            if row.lease_token == payload.get('token'):
                row.lease_token = row.lease_until = None
            return {'released': True}
        if event == 'claim':
            delay = max(1, float(payload.get('delay', 1)))
            if not math.isfinite(delay):
                raise ValueError('Invalid host delay')
            # robots.txt may reveal a larger crawl-delay after its own fetch.
            # Apply the current delay to the previous physical dispatch too.
            revised = row.last_started_at + timedelta(seconds=delay) if row.last_started_at else None
            due = max([now, *[value for value in (row.next_allowed_at, row.blocked_until, row.lease_until, revised) if value]])
            if due > now:
                return {'wait_seconds': (due - now).total_seconds()}
            row.lease_token = uid('host')
            row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            return {'token': row.lease_token}
        if row.lease_token != payload.get('token') or not row.lease_until or row.lease_until <= now:
            return {'wait_seconds': LEASE_SECONDS, 'revoked': True}
        if row.blocked_until and row.blocked_until > now:
            return {'wait_seconds': (row.blocked_until - now).total_seconds()}
        delay = max(1, float(payload.get('delay', 1)))
        if not math.isfinite(delay):
            raise ValueError('Invalid host delay')
        # Anchor spacing immediately before physical dispatch, after observation
        # persistence. A crash never clears this already committed allowance.
        row.next_allowed_at = now + timedelta(seconds=delay)
        row.last_started_at = now
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        return {'ready': True}
