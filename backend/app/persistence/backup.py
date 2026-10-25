"""Consistent Skill database and referenced artifact backup/isolated restore."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from app.config import ROOT, settings
from app.persistence.connection import parameters, environment


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def connection(database=None):
    url = make_url(settings.database_url)
    return psycopg.connect(**parameters(url.set(database=database or url.database)))


def _environment(database=None):
    url = make_url(settings.database_url)
    return environment(url.set(database=database or url.database))


def _binary(name):
    path = Path(os.getenv("PG_BIN_DIR", ROOT / ".runtime" / "pgsql" / "bin")) / (name + ".exe" if os.name == "nt" else name)
    if path.is_file():
        return str(path)
    value = shutil.which(name)
    if not value:
        raise ValueError("Missing PostgreSQL client: " + name)
    return value


def inventory(conn):
    names = [item[0] for item in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    counts, hashes, refs = {}, {}, set()
    for table in names:
        values = [row[0] for row in conn.execute(sql.SQL("SELECT to_jsonb(t)::text FROM {} AS t ORDER BY to_jsonb(t)::text").format(sql.Identifier(table)))]
        counts[table] = len(values)
        hashes[table] = hashlib.sha256("\n".join(values).encode()).hexdigest()
    for table, columns in (("skill_snapshots", ("artifact_ref",)), ("skill_exports", ("artifact_ref", "report_ref"))):
        if table not in names:
            continue
        for column in columns:
            for (ref,) in conn.execute(sql.SQL("SELECT {} FROM {} WHERE {} IS NOT NULL").format(
                sql.Identifier(column), sql.Identifier(table), sql.Identifier(column))):
                if Path(ref).name != ref:
                    raise ValueError("Non-opaque artifact reference")
                refs.add(ref)
    return counts, hashes, refs


def artifact_expectations(conn):
    """Read original checksums from the same database snapshot as the dump."""
    expected = {}
    names = {item[0] for item in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    def add(key, checksum=None, report=None):
        if not key:
            return
        if Path(key).name != key or '/' in key or '\\' in key:
            raise ValueError('Non-opaque artifact reference')
        item = expected.setdefault(key, {'sha256': None, 'reports': []})
        if checksum:
            if not re.fullmatch(r'[a-f0-9]{64}', checksum):
                raise ValueError('Invalid original artifact hash')
            if item['sha256'] and item['sha256'] != checksum:
                raise ValueError('Conflicting original artifact hashes')
            item['sha256'] = checksum
        if report is not None:
            item['reports'].append(report)
        if not checksum and report is None:
            raise ValueError('Missing original artifact hash')
    if 'skill_snapshots' in names:
        for key, checksum in conn.execute('SELECT artifact_ref, artifact_hash FROM skill_snapshots WHERE artifact_ref IS NOT NULL'):
            add(key, checksum)
    if 'skill_exports' in names:
        has_report_hash = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='skill_exports' AND column_name='report_hash'").fetchone()
        report_hash = sql.Identifier('report_hash') if has_report_hash else sql.SQL('NULL')
        query = sql.SQL('SELECT artifact_ref, artifact_hash, report_ref, {}, manifest_json FROM skill_exports').format(report_hash)
        for key, checksum, report, report_checksum, manifest in conn.execute(query):
            add(key, checksum)
            # Old report rows have no byte checksum. Their saved JSON manifest
            # is still an independent original against which to check content.
            add(report, report_checksum, manifest)
    return expected


def verify_artifact(path, expectation):
    actual = digest(path)
    if expectation['sha256'] and actual != expectation['sha256']:
        raise ValueError('Artifact original hash mismatch')
    for manifest in expectation['reports']:
        try:
            report = json.loads(Path(path).read_text(encoding='utf-8'))
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Artifact report integrity check failed') from exc
        if report != manifest:
            raise ValueError('Artifact report differs from original database manifest')
    return actual


def backup(output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Backup destination already exists")
    output.mkdir(parents=True)
    (output / "artifacts").mkdir()
    with connection() as conn:
        # Acquire before the repeatable-read snapshot, so a concurrent revoke
        # cannot unlink files still referenced by that snapshot.
        conn.autocommit = True
        conn.execute("SELECT pg_advisory_lock(7334003)")
        conn.autocommit = False
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        counts, hashes, refs = inventory(conn)
        expected = artifact_expectations(conn)
        if refs != set(expected):
            raise ValueError('Artifact references lack original integrity data')
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run([_binary("pg_dump"), "--format=custom", "--no-owner", "--no-acl",
            "--snapshot=" + snapshot, "--file=" + str(output / "database.dump")],
            env=_environment(), capture_output=True, timeout=120, **flags)
        if result.returncode:
            raise ValueError("PostgreSQL dump failed; incomplete backup retained")
        artifacts = []
        from app.persistence import artifact_path
        for key in sorted(refs):
            shutil.copy2(artifact_path(key), output / "artifacts" / key)
            checksum = verify_artifact(output / 'artifacts' / key, expected[key])
            artifacts.append({"key": key, "sha256": checksum})
    manifest = {"format": "skill-v2-backup-1", "table_counts": counts, "table_hashes": hashes,
                "database_sha256": digest(output / "database.dump"), "artifacts": artifacts}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "passed", "backup": str(output), "artifact_count": len(artifacts)}


def restore(source, target_database, target_artifacts):
    source, target = Path(source).resolve(), Path(target_artifacts).resolve()
    current = make_url(settings.database_url).database
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", target_database) or target_database in {current, "postgres", "template0", "template1"}:
        raise ValueError("Restore must target a new isolated database")
    if target.exists():
        raise ValueError("Restore artifact directory must not exist")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "skill-v2-backup-1" or digest(source / "database.dump") != manifest["database_sha256"]:
        raise ValueError("Backup format or database hash mismatch")
    for item in manifest["artifacts"]:
        key = item['key']
        if not key or Path(key).name != key or '/' in key or '\\' in key or digest(source / "artifacts" / key) != item["sha256"]:
            raise ValueError("Artifact integrity check failed")
    with connection("postgres") as conn:
        conn.autocommit = True
        if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target_database,)).fetchone():
            raise ValueError("Restore database already exists")
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_database)))
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run([_binary("pg_restore"), "--no-owner", "--no-acl", "--exit-on-error", "--dbname=" + target_database,
        str(source / "database.dump")], env=_environment(target_database), capture_output=True, timeout=120, **flags)
    if result.returncode:
        raise ValueError("Restore failed; isolated database retained for investigation")
    target.mkdir(parents=True)
    for item in manifest["artifacts"]:
        shutil.copy2(source / "artifacts" / item["key"], target / item["key"])
    with connection(target_database) as conn:
        counts, hashes, refs = inventory(conn)
        expected = artifact_expectations(conn)
    if counts != manifest["table_counts"] or hashes != manifest["table_hashes"] or refs != {item["key"] for item in manifest["artifacts"]}:
        raise ValueError("Restored counts, data hashes or artifact references differ")
    if refs != set(expected):
        raise ValueError('Restored artifact references lack original integrity data')
    for item in manifest['artifacts']:
        # The manifest's copy-time digest alone cannot detect corruption that
        # predates the backup. Recheck the original hashes in the restored DB,
        # and verify the bytes after the final copy as well.
        actual = verify_artifact(target / item['key'], expected[item['key']])
        if actual != item['sha256']:
            raise ValueError('Restored artifact integrity check failed')
    return {"status": "passed", "target_database": target_database, "artifact_count": len(refs),
            "table_counts_match": True, "table_hashes_match": True, 'original_artifact_hashes_match': True}
