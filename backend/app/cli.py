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
    stage = commands.add_parser("stage-draft", help="离线把观察证据整理成可预检候选，不写库、不联网")
    stage.add_argument("--spec-json", required=True, type=Path)
    stage.add_argument("--artifact-json", required=True, type=Path)
    stage.add_argument("--candidate-ids", required=True, help="逗号分隔的候选标识，最多 20 个")
    stage.add_argument("--as-of", required=True, help="带时区的对照时间")
    stage.add_argument("--out", type=Path, help="写出可提交子集 JSON")
    for command in ("create", "append", "process", "status", "finish", "export", "reassess",
                    "extend-research", "supplement", "advance"):
        sub = commands.add_parser(command)
        sub.add_argument("--run", required=True)
        if command == "create":
            sub.add_argument("--spec-json", required=True, type=Path)
        if command in {"append", "finish", "advance"}:
            sub.add_argument("--input-json", required=command == "append", type=Path)
        if command == "advance":
            sub.add_argument("--artifact-json", type=Path, help="观察证据工件；给定时先离线整理再提交")
            sub.add_argument("--candidate-ids", help="与 --artifact-json 配套，逗号分隔，最多 20 个")
            sub.add_argument("--as-of", help="与 --artifact-json 配套的带时区对照时间")
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
    live = commands.add_parser("live", help="生成本机实时成果页面并前台查看；仅绑定 127.0.0.1")
    live.add_argument("--run", required=True, help="指定批次的 run 标识")
    live.add_argument("--port", type=int, default=0, help="本机端口，默认由系统分配")
    live.add_argument("--idle-timeout", type=int, default=1800, help="无活动自动退出秒数（30-86400）")
    live.add_argument("--open", action="store_true", help="启动后由用户主动打开默认浏览器")
    live.add_argument("--rebuild-only", action="store_true", help="只重建页面数据，不启动查看服务")
    suppressions = commands.add_parser("suppressions", help="管理全局抑制名单")
    actions = suppressions.add_subparsers(dest="suppression_action", required=True)
    actions.add_parser("list")
    add = actions.add_parser("add")
    add.add_argument("--type", choices=("domain", "company", "email", "phone"), required=True)
    add.add_argument("--value", required=True)
    add.add_argument("--reason", default="用户排除")
    remove = actions.add_parser("remove")
    remove.add_argument("--id", required=True)
    discover = commands.add_parser("discover", help="从公开数据源批量拉取未核实候选；不进合格计数（§3.1）")
    discover.add_argument("--run", required=True)
    discover.add_argument("--source", choices=("eu_ted", "eurostat_comext", "google_places", "all"),
                          default="all")
    discover.add_argument("--limit", type=int, default=50)
    discover.add_argument("--list", action="store_true", help="查看未核实池（分页）")
    discover.add_argument("--cursor", help="--list 的下一页游标")
    discover.add_argument("--reject", help="拒绝未核实候选（按 id）")
    discover.add_argument("--verify", help="核实通过未核实候选（按 id）")
    discover.add_argument("--explain", help="解密原始记录信封（按 id）")
    discover.add_argument("--yes", action="store_true", help="人确认解密信封")
    discover.add_argument("--reason", help="--reject 的原因；必填，空串报错")
    discover.add_argument("--legacy-search", action="store_true",
                          help="降级模式：跳过所有 adapter，回到人工发现流程（§3.6）")
    commands.add_parser("sources", help="各数据源可用性与凭据状态")
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
    if args.command == "stage-draft":
        from app.evidence_stage import draft
        ids = [item.strip() for item in args.candidate_ids.split(',') if item.strip()]
        staged = draft(read_json(args.artifact_json), read_json(args.spec_json), ids, as_of=args.as_of)
        if args.out:
            args.out.write_text(json.dumps(staged['submittable'], ensure_ascii=False, indent=1),
                                encoding='utf-8')
        return {'version': staged['version'], 'chunk_id': staged['chunk_id'],
                'submittable': len(staged['submittable']['candidates']),
                'errors': staged['errors'], 'out': str(args.out) if args.out else None}
    # 密钥启动校验（spec 2.6 §3.5）：除 capabilities 与两个纯离线命令外，
    # 任何命令在动数据库之前先确认密钥就绪；宁可起不来，不要跑一半崩。
    from app.secrets import validate_startup_secrets
    validate_startup_secrets()
    from app.runtime import prepare
    prepared = prepare()
    # Replaces the retired worker's periodic retention. The service throttles
    # this maintenance; manual maintenance remains an explicit preview/apply.
    if args.command != "maintenance":
        runs.maintenance(apply=True, periodic=True)
    if args.command == "prepare":
        return prepared
    if args.command == "live":
        from app.live import viewer
        from app.live.projection import refresh
        rebuilt = refresh(args.run, force=True)
        if rebuilt.get("error"):
            raise AppError(rebuilt["error"], "无法生成成果页面；请检查批次与受管理工件目录", 409)
        if args.rebuild_only:
            return {"run_id": args.run, "rebuilt": rebuilt}
        if not 30 <= args.idle_timeout <= 86400:
            raise AppError("VALIDATION_ERROR", "空闲超时必须在 30 至 86400 秒之间")
        # Blocks for the viewing session; the startup line is printed by the server.
        try:
            viewer.serve(args.run, port=args.port, idle_timeout=args.idle_timeout, open_browser=args.open)
        except OSError:
            raise AppError("PORT_UNAVAILABLE", "本机端口不可用；请更换 --port 或省略该参数由系统分配", 409)
        return None
    if args.command == "list":
        return {"items": runs.list_runs()}
    if args.command == "sources":
        from app.discovery import service
        return service.sources_status()
    if args.command == "discover":
        from app.discovery import service
        if args.legacy_search:
            return service.enable_legacy(args.run)
        if args.list:
            return service.list_candidates(args.run, source=args.source if args.source != 'all' else None,
                                           status=None, cursor=args.cursor, limit=args.limit)
        if args.reject:
            return service.reject_candidate(args.run, args.reject, args.reason)
        if args.verify:
            return service.verify_candidate(args.run, args.verify)
        if args.explain:
            return service.explain_envelope(args.run, args.explain, confirmed=args.yes)
        if args.source == "all":
            return service.discover_all(args.run, limit=args.limit)
        return service.discover(args.run, args.source, limit=args.limit)
    if args.command == "create":
        return runs.create(args.run, read_json(args.spec_json))
    if args.command == "append":
        body = ResearchInput.model_validate(read_json(args.input_json)).model_dump()
        return runs.append(args.run, chunk_id=args.chunk, **body)
    if args.command == "advance":
        payload = read_json(args.input_json) if args.input_json else None
        chunk = args.chunk
        staging = None
        if args.artifact_json:
            from datetime import datetime, timezone
            from app.evidence_stage import draft
            ids = [item.strip() for item in (args.candidate_ids or '').split(',') if item.strip()]
            if not ids:
                raise AppError("VALIDATION_ERROR", "--artifact-json 需要 --candidate-ids")
            as_of = args.as_of or datetime.now(timezone.utc).isoformat()
            frozen = runs.status(args.run).get('spec') or {}
            staged = draft(read_json(args.artifact_json), frozen, ids, as_of=as_of)
            payload = staged['submittable']
            chunk = chunk or staged['chunk_id']
            staging = {'version': staged['version'],
                       'submittable': len(payload['candidates']),
                       'errors': staged['errors']}
        if payload is None:
            raise AppError("VALIDATION_ERROR", "advance 需要 --input-json 或 --artifact-json")
        result = runs.advance(args.run, payload, chunk_id=chunk, max_jobs=args.max_jobs,
                              max_seconds=args.max_seconds, workers=args.workers,
                              resume=args.resume)
        if staging and isinstance(result, dict):
            result = {**result, 'staging': staging}
        return result
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
    try:
        from app.release import versions
        version = versions()
    except Exception:
        version = {}
    stamp = 'b2b-leads Skill {skill} · 核心 {core} · 规则 {rule} · release {release}'.format(
        skill=version.get('skill_version', 'unknown'), core=version.get('core_version', 'unknown'),
        rule=version.get('rule_version', 'unknown'),
        release=str(version.get('release_id') or 'development')[:12])
    print(stamp, file=sys.stderr)
    args = parser().parse_args(values)
    try:
        if args.root:
            # The wrapper normally sets this before importing config. Reject a
            # conflicting root rather than operating on another checkout.
            from app.config import ROOT
            if args.root.resolve() != ROOT:
                raise AppError("PROJECT_MISMATCH", "项目目录与已载入的配置不一致")
        result = dispatch(args)
        if result is not None:
            payload = dict(result) if isinstance(result, dict) else result
            if isinstance(payload, dict):
                payload.setdefault('versions', version)
            print(json.dumps(payload, ensure_ascii=False, default=str))
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
    print(json.dumps({"error": failure, "versions": version}, ensure_ascii=False))
    return 1
