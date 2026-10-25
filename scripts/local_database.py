"""Lifecycle for this workspace's existing Windows PostgreSQL cluster only."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "start", "stop", "status"))
    args = parser.parse_args()
    runtime = ROOT / ".runtime"
    binary = runtime / "pgsql" / "bin"
    data = runtime / "pgdata"
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    if args.action == "init":
        if data.exists():
            raise ValueError("Database already initialized; use start")
        if not (binary / "initdb.exe").is_file():
            raise ValueError("Install PostgreSQL 16 binaries into .runtime/pgsql first")
        if (ROOT / ".env").exists():
            raise ValueError("Existing .env retained; configure a PostgreSQL connection explicitly")
        runtime.mkdir(exist_ok=True)
        password = secrets.token_urlsafe(32)
        temporary = runtime / "pg-password.tmp"
        temporary.write_text(password, encoding="utf-8")
        try:
            subprocess.run([str(binary / "initdb.exe"), "-D", str(data), "-U", "acq", "-A", "scram-sha-256",
                "--pwfile", str(temporary), "--encoding=UTF8", "--locale=C"], check=True, capture_output=True, **flags)
        finally:
            temporary.unlink(missing_ok=True)
        with (data / "postgresql.conf").open("a") as stream:
            stream.write("\nlisten_addresses='127.0.0.1'\nport=55432\nmax_connections=50\n")
        (ROOT / ".env").write_text("SCA_DATABASE_URL=postgresql+psycopg://acq:" + password +
            "@127.0.0.1:55432/sca_skill_v2\nSCA_FIXTURE_MODE=false\n", encoding="utf-8")
    else:
        executable = binary / "pg_ctl.exe"
        if not executable.is_file() or not (data / "PG_VERSION").is_file():
            raise ValueError("Workspace PostgreSQL is not initialized")
        command = [str(executable), "-D", str(data)]
        if args.action == "start":
            command += ["-l", str(runtime / "postgres.log"), "start", "-w"]
        elif args.action == "stop":
            command += ["stop", "-m", "fast", "-w"]
        else:
            command += ["status"]
        result = subprocess.run(command, capture_output=True, **flags)
        if result.returncode:
            print(json.dumps({"status": "unavailable", "action": args.action}))
            return result.returncode
    print(json.dumps({"status": "ok", "action": args.action}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error) if isinstance(error, ValueError) else "PostgreSQL operation failed"}))
        raise SystemExit(1)
