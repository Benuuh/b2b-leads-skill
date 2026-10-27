"""Prepare only PostgreSQL and the isolated Skill schema; no server process."""
import os
import subprocess

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from app.config import ROOT, settings
from app.errors import AppError
from app.persistence.connection import parameters


def _connect(url):
    return psycopg.connect(**parameters(url), autocommit=True)


def _start_workspace_postgres(url):
    if os.name != "nt" or url.host not in {"localhost", "127.0.0.1", "::1"} or url.port != 55432:
        return False
    binary = ROOT / ".runtime" / "pgsql" / "bin" / "pg_ctl.exe"
    data = ROOT / ".runtime" / "pgdata"
    if not binary.is_file() or not (data / "PG_VERSION").is_file():
        return False
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW}
    status = subprocess.run([str(binary), "-D", str(data), "status"], capture_output=True, **flags)
    if status.returncode == 0:
        return False
    result = subprocess.run(
        [str(binary), "-D", str(data), "-l", str(ROOT / ".runtime" / "postgres.log"), "start", "-w"],
        # PostgreSQL can retain pg_ctl's pipe handles after pg_ctl exits.
        # File handles let this command finish while the database keeps running.
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=60, **flags,
    )
    return result.returncode == 0


def prepare():
    settings.validate()
    target = make_url(settings.database_url)
    if target.database in {None, "postgres", "template0", "template1"}:
        raise AppError("UNSAFE_DATABASE", "Skill 必须使用独立业务数据库，不可使用 postgres 或模板库")
    admin = target.set(database="postgres")
    try:
        connection = _connect(admin)
    except psycopg.OperationalError:
        if not _start_workspace_postgres(target):
            raise AppError("DATABASE_UNAVAILABLE", "PostgreSQL 不可用；请检查本地数据库或 SCA_DATABASE_URL", 503)
        connection = _connect(admin)
    with connection:
        connection.execute("SELECT pg_advisory_lock(hashtext(%s))", ("sca-skill-create:" + target.database,))
        try:
            if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target.database,)).fetchone():
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target.database)))
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("sca-skill-create:" + target.database,))
    from app.persistence import initialize
    schema = initialize()
    from app.release import release_identity
    return {"ready": True, "runtime": "direct_python", "database": target.database,
            "api_required": False, "worker_required": False,
            "schema_revision": schema['schema_revision'], "release": release_identity(),
            "artifact_root": str(settings.artifact_root.resolve())}
