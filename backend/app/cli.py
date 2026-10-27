"""Thin JSON command adapter for the single-user acquisition service."""
import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from app.errors import AppError
from app.schemas import ResearchInput, SuppressionInput


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 6 * 1024 * 1024:
        raise AppError("INPUT_TOO_LARGE", "输入 JSON 超过 6 MiB")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise AppError("INVALID_INPUT", "输入 JSON 必须是对象")
    return value


def parser():
    result = argparse.ArgumentParser(description="Skill 本机获客：直接调用 Python，无需 API 或登录")
    result.add_argument("--root", type=Path, help="项目目录（由 Skill 包装入口传入）")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("list", help="列出已保存批次")
    commands.add_parser("capabilities", help="离线读取支持的配置版本、字段、运算符与限额")
    validate = commands.add_parser("validate-input", help="离线逐条预检候选，不写库、不联网")
    validate.add_argument("--spec-json", required=True, type=Path)
    validate.add_argument("--input-json", required=True, type=Path)
    for command in ("create", "append", "process", "status", "finish", "export", "reassess",
                    "extend-research", "supplement", "advance"):
        sub = commands.add_parser(command)
        sub.add_argument("--run", required=True)
        if command == "create":
            sub.add_argument("--spec-json", required=True, type=Path)
        if command in {"append", "finish", "advance"}:
            sub.add_argument("--input-json", required=command in {"append", "advance"}, type=Path)
        if command in {"append", "advance"}:
            sub.add_argument("--chunk", help="同一批输入重试必须使用相同 chunk；默认使用内容哈希")
        if command in {"process", "finish", "advance"}:
            sub.add_argument("--max-jobs", type=int, default=20)
            sub.add_argument("--max-seconds", type=int, default=60)
            sub.add_argument("--resume", action="store_true")
            sub.add_argument("--operation-key", help="显式重放同一次处理调用")
            sub.add_argument("--workers", type=int, help="受支持版本的有界并发研究 worker 数（1-4）")
        if command == "status":
            sub.add_argument("--action", choices=("pause", "resume", "cancel", "retry"))
        if command == "reassess":
            sub.add_argument("--company-id", action="append", required=True, help="保存证据重评；可重复，最多 20 家")
            sub.add_argument("--operation-key", required=True, help="显式重放同一次保存证据重评")
        if command == "supplement":
            sub.add_argument("--company-id", required=True, help="为已保留企业追加增量证据")
            sub.add_argument("--input-json", required=True, type=Path)
            sub.add_argument("--operation-key", required=True, help="显式重放同一次增量补证")
        if command == "extend-research":
            sub.add_argument("--hours", type=int, required=True, help="新的累计研究时限（小时），只能增加，最多 72 小时")
            sub.add_argument("--operation-key", required=True, help="显式重放同一次时限延长")
        if command == "export":
            sub.add_argument("--format", choices=("xlsx", "csv"), default="xlsx")
            sub.add_argument("--export-key", help="保留具名导出快照")
    suppressions = commands.add_parser("suppressions", help="管理全局抑制名单")
    actions = suppressions.add_subparsers(dest="suppression_action", required=True)
    actions.add_parser("list")
    add = actions.add_parser("add")
    add.add_argument("--type", choices=("domain", "company", "email", "phone"), required=True)
    add.add_argument("--value", required=True)
    add.add_argument("--reason", default="用户排除")
    remove = actions.add_parser("remove")
    remove.add_argument("--id", required=True)
    delete = commands.add_parser("delete-company")
    delete.add_argument("--id", required=True)
    maintenance = commands.add_parser("maintenance", help="默认只预览过期数据清理")
    maintenance.add_argument("--apply", action="store_true")
    return result


def dispatch(args):
    from app import runs

    if args.command == "capabilities":
        from app.capabilities import capabilities
        return capabilities()
    if args.command == "validate-input":
        return runs.validate_input(read_json(args.spec_json), read_json(args.input_json))
    from app.runtime import prepare
    prepared = prepare()
    # Replaces the retired worker's periodic retention. The service throttles
    # this maintenance; manual maintenance remains an explicit preview/apply.
    if args.command != "maintenance":
        runs.maintenance(apply=True, periodic=True)
    if args.command == "prepare":
        return prepared
    if args.command == "list":
        return {"items": runs.list_runs()}
    if args.command == "create":
        return runs.create(args.run, read_json(args.spec_json))
    if args.command == "append":
        body = ResearchInput.model_validate(read_json(args.input_json)).model_dump()
        return runs.append(args.run, chunk_id=args.chunk, **body)
    if args.command == "advance":
        return runs.advance(args.run, read_json(args.input_json), chunk_id=args.chunk,
                            max_jobs=args.max_jobs, max_seconds=args.max_seconds,
                            workers=args.workers, resume=args.resume)
    if args.command == "status":
        return runs.status(args.run, action=args.action)
    if args.command == "reassess":
        return runs.reassess(args.run, company_ids=args.company_id, operation_key=args.operation_key)
    if args.command == "supplement":
        return runs.supplement(args.run, args.company_id, read_json(args.input_json),
                               operation_key=args.operation_key)
    if args.command == "extend-research":
        return runs.extend_research(args.run, hours=args.hours, operation_key=args.operation_key)
    if args.command in {"process", "finish"}:
        options = dict(max_jobs=args.max_jobs, max_seconds=args.max_seconds,
                       resume=args.resume, operation_key=args.operation_key, workers=args.workers)
        if args.command == "process":
            return runs.process(args.run, **options)
        body = ResearchInput.model_validate(read_json(args.input_json) if args.input_json else {}).model_dump()
        if body["candidates"]:
            raise AppError("INVALID_INPUT", "finish 只接收检查点与工具计数；候选请先 append")
        return runs.finish(args.run, checkpoint=body["checkpoint"],
                           external_tool_usage=body["external_tool_usage"], **options)
    if args.command == "export":
        return runs.export(args.run, format=args.format, export_key=args.export_key)
    if args.command == "suppressions":
        if args.suppression_action == "list":
            return {"items": runs.suppression_list()}
        if args.suppression_action == "remove":
            return runs.suppression_remove(args.id)
        body = SuppressionInput(type=args.type, value=args.value, reason=args.reason)
        return runs.suppression_add(**body.model_dump())
    if args.command == "delete-company":
        return runs.delete_company(args.id)
    return runs.maintenance(apply=args.apply)


def main(argv=None):
    # Machine-readable JSON and Chinese argparse text use one encoding even on
    # Windows hosts whose inherited console/pipe encoding is cp1252.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            reconfigure(encoding='utf-8')
    values = list(sys.argv[1:] if argv is None else argv)
    if any(value == "--url" or value.startswith("--url=") for value in values):
        print(json.dumps({"error": {"code": "REMOVED_OPTION", "message": "--url 已移除；Skill 现直接调用 Python 核心，无需 API 地址"}}, ensure_ascii=False))
        return 2
    args = parser().parse_args(values)
    try:
        if args.root:
            # The wrapper normally sets this before importing config. Reject a
            # conflicting root rather than operating on another checkout.
            from app.config import ROOT
            if args.root.resolve() != ROOT:
                raise AppError("PROJECT_MISMATCH", "项目目录与已载入的配置不一致")
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except AppError as error:
        failure = error.body()
    except ValidationError as error:
        failure = {"code": "VALIDATION_ERROR", "message": "输入字段或格式错误", "fields": [
            {"path": list(item["loc"]), "type": item["type"]} for item in error.errors()
        ]}
    except (OSError, json.JSONDecodeError):
        failure = {"code": "INVALID_INPUT", "message": "无法读取输入 JSON 或本地文件"}
    except KeyboardInterrupt:
        failure = {"code": "INTERRUPTED", "message": "进度已保存在数据库；使用原 run 与操作键重试，休息前请 pause"}
    except Exception as error:
        # Exception text can contain credentials, SQL parameters or contact data.
        failure = {"code": "CORE_ERROR", "message": "操作未完成；保留原输入并使用相同 run 重试", "exception_type": type(error).__name__}
    print(json.dumps({"error": failure}, ensure_ascii=False))
    return 1
