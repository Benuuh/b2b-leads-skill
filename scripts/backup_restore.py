"""Consistent Skill database backup and restore into a NEW database/directory."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.persistence.backup import backup, restore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup")
    create.add_argument("--output", required=True)
    load = commands.add_parser("restore")
    load.add_argument("--source", required=True)
    load.add_argument("--target-database", required=True)
    load.add_argument("--target-artifacts", required=True)
    args = parser.parse_args()
    try:
        result = backup(args.output) if args.command == "backup" else restore(args.source, args.target_database, args.target_artifacts)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__,
            "message": str(error) if isinstance(error, ValueError) else "Backup/restore failed; credentials omitted"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
