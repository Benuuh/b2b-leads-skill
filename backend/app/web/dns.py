"""Bounded, cached DNS/MX lookup; never validates an individual mailbox."""
from functools import lru_cache

@lru_cache(maxsize=4096)
def lookup_mx(domain):
    """Free DNS only, bounded to 2 seconds per domain, cached within this worker."""
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.timeout = 1.0
        resolver.lifetime = 2.0
        from .observation import observe_lookup
        with observe_lookup('mx'):
            records = list(resolver.resolve(domain, "MX", lifetime=2.0))
        # RFC 7505 null MX explicitly says that the domain accepts no mail.
        return "valid" if any(str(r.exchange).strip(".") for r in records) else "invalid"
    except ImportError:
        return "unknown"
    except Exception as exc:
        return "invalid" if type(exc).__name__ in {"NXDOMAIN", "NoAnswer"} else "unknown"
