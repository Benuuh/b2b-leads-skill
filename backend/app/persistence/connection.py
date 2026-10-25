"""Preserve libpq URL options in Python connections and PostgreSQL tools."""
import os

import psycopg
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import make_url


def parameters(url):
    url = make_url(url) if isinstance(url, str) else url
    params = conninfo_to_dict(url.set(drivername="postgresql").render_as_string(hide_password=False))
    params.setdefault("connect_timeout", "5")
    return params


def environment(url):
    mapping = {item.keyword.decode(): item.envvar.decode() for item in psycopg.pq.Conninfo.get_defaults() if item.envvar}
    result = dict(os.environ)
    for key, value in parameters(url).items():
        if key not in mapping:
            raise ValueError("PostgreSQL client parameter has no environment equivalent: " + key)
        result[mapping[key]] = value
    return result
