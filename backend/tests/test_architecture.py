"""Guard the dependency boundaries that make each step independently usable."""
import ast

from app.config import ROOT


def imports(path):
    values = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            values.extend(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            values.append(node.module or "")
    return values


def test_processing_layers_do_not_import_database_or_old_runtime():
    root = ROOT / "backend" / "app"
    paths = [root / "pipeline.py", *root.joinpath("rules").glob("*.py"),
             *root.joinpath("web").glob("*.py"), *root.joinpath("delivery").glob("*.py")]
    forbidden = ("sqlalchemy", "psycopg", "app.persistence", "app.runs", "app.models", "app.db",
                 "app.b2b_", "app.providers", "app.auth", "fastapi", "uvicorn")
    for path in paths:
        assert not [name for name in imports(path) if name.startswith(forbidden)], path.name


def test_selection_does_not_depend_on_exporting_or_metrics():
    path = ROOT / "backend" / "app" / "delivery" / "selection.py"
    assert not {"metrics", "exporting"} & set(imports(path))
