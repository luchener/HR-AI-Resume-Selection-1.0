# -*- coding: utf-8 -*-
"""数据保留期自动清理（简历 / 岗位原文）。

策略：超过 config.RETENTION_DAYS 天的简历原文、岗位原文自动删除。**归档（打分结果）不删**
—— 只标记 source_purged，报告页据此提示“原文已过保留期清除”，候选人打分与报告快照继续可用。

默认开启（config.RETENTION_ENABLED 默认 on，与系统内《隐私政策》“简历原文最长保留 30 天”
的对外承诺一致）。**自动触发只在 ENV=production 生效**，手动清理不受限。

开启状态下的第一次运行会一次性删掉所有超期数据，不可逆。上线前先看影响面：

    cd apps/backend && python -m retention --dry-run

临时关闭：在 .env 里写 RETENTION_ENABLED=off。
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import config
import resume_original
import store

logger = logging.getLogger("retention")

_SENTINEL = os.path.join(config.DATA_DIR, ".last_retention")
_CHECK_INTERVAL_SECONDS = 300.0  # 进程内节流：5 分钟内不重复看哨兵
_SENTINEL_INTERVAL_SECONDS = 86400.0  # 磁盘哨兵：每天最多真正清理一次
_LAST_CHECK = 0.0


def enabled() -> bool:
    """
    是否启用**自动**清理。

    只在生产环境（ENV=production）执行。

    注意：`apps/backend/.env` 里 ENV 就是 production（本地也不例外），所以这道护栏
    只能挡住「ENV=local」的开发机，挡不住本机跑测试。真正兜住本地数据的，是每个测试
    模块的数据目录隔离——2026-09-29 本地 data/ 下 83 份简历 + 63 个岗位，就是被
    test_notifications / test_quota 这两个没隔离 store 目录的模块在请求里触发自动清理删空的。

    手动清理不受限制——`python -m retention` / `--dry-run` 是操作者的显式动作，
    在任何环境都应该能跑。
    """
    if not bool(config.RETENTION_ENABLED) or int(config.RETENTION_DAYS or 0) <= 0:
        return False
    return str(getattr(config, "ENV", "local")).strip().lower() == "production"


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cutoff(days: int | None = None) -> datetime:
    window = int(days if days is not None else config.RETENTION_DAYS)
    return datetime.now(timezone.utc) - timedelta(days=window)


def _age(path: str, record: dict) -> datetime:
    """过期判定依据：优先记录里的 created_at，缺失时退回文件 mtime。"""
    created = _parse_iso(record.get("created_at"))
    if created is not None:
        return created
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


def _scan(directory: str, days: int) -> list[tuple[str, str]]:
    """返回 [(记录 ID, 路径)]，只包含已超期的记录。"""
    cutoff = _cutoff(days)
    hits: list[tuple[str, str]] = []
    if not os.path.isdir(directory):
        return hits
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json") or filename.startswith("_"):
            continue
        path = os.path.join(directory, filename)
        try:
            record = store._read_json(path) or {}
        except Exception:
            continue
        if _age(path, record) < cutoff:
            hits.append((filename[: -len(".json")], path))
    return hits


def scan_expired(days: int | None = None) -> dict:
    """只扫描不删除，返回影响面（供 --dry-run 与测试使用）。"""
    window = int(days if days is not None else config.RETENTION_DAYS)
    return {
        "days": window,
        "cutoff": _cutoff(window).isoformat(),
        "resumes": _scan(store.RESUMES_DIR, window),
        "jobs": _scan(store.JOBS_DIR, window),
        # 孤儿原件：简历记录已不存在（账号删除等路径留下的文件）
        "original_orphans": len(resume_original.orphans()),
    }


def purge(days: int | None = None, dry_run: bool = False) -> dict:
    """执行清理，返回统计。dry_run=True 时只统计不落盘。"""
    found = scan_expired(days)
    resume_ids = {resume_id for resume_id, _ in found["resumes"]}
    report = {
        "days": found["days"],
        "cutoff": found["cutoff"],
        "dry_run": bool(dry_run),
        "resumes": len(found["resumes"]),
        "jobs": len(found["jobs"]),
        "archives_marked": 0,
        "original_orphans": found.get("original_orphans", 0),
        "originals_swept": 0,
    }

    # 归档先标记（必须在删原文之前：标记需要 resume_id 还能对上）
    report["archives_marked"] = store.mark_archives_source_purged(
        resume_ids, dry_run=dry_run
    )
    if dry_run:
        return report

    for resume_id, _path in found["resumes"]:
        store.delete_resume(resume_id)  # user_id 为空 = 保留期清理路径，不校验归属
    for job_id, _path in found["jobs"]:
        store.delete_job(job_id)

    # 原始文件：随记录删除的那部分由 store.delete_resume 处理，这里只回收孤儿文件
    report["originals_swept"] = resume_original.sweep()

    if report["resumes"] or report["jobs"]:
        logger.info(
            "[retention] 清理超期原文（%s 天）：简历 %s 条 / 岗位 %s 条 / 标记归档 %s 条",
            report["days"],
            report["resumes"],
            report["jobs"],
            report["archives_marked"],
        )
    return report


def _due(now: float | None = None) -> bool:
    """磁盘哨兵：每天最多真正清理一次。"""
    global _LAST_CHECK
    now = time.time() if now is None else now
    if now - _LAST_CHECK < _CHECK_INTERVAL_SECONDS:
        return False
    _LAST_CHECK = now
    try:
        last = os.path.getmtime(_SENTINEL)
    except OSError:
        return True
    return now - last >= _SENTINEL_INTERVAL_SECONDS


def _touch_sentinel() -> None:
    try:
        os.makedirs(os.path.dirname(_SENTINEL), exist_ok=True)
        with open(_SENTINEL, "w", encoding="utf-8") as fh:
            fh.write(datetime.now(timezone.utc).isoformat())
    except OSError:
        logger.warning("[retention] 哨兵写入失败", exc_info=True)


def maybe_purge(now: float | None = None) -> dict | None:
    """懒清理入口。未启用 / 未到期 / 出错都静默返回，绝不影响请求。"""
    if not enabled() or not _due(now):
        return None
    try:
        report = purge()
    except Exception as exc:
        logger.warning("[retention] skipped due to error: %s", exc)
        return None
    _touch_sentinel()
    return report


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="简历/JD 原文保留期清理")
    parser.add_argument("--dry-run", action="store_true", help="只报告影响面，不删除")
    parser.add_argument("--days", type=int, default=None, help="覆盖保留天数（默认取配置）")
    parser.add_argument("--list", type=int, default=10, help="列出前 N 条待清理记录")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    window = args.days if args.days is not None else config.RETENTION_DAYS
    found = scan_expired(window)
    print(f"保留期：{window} 天 截止时间：{found['cutoff']}")
    print(f"开关：RETENTION_ENABLED={'on' if config.RETENTION_ENABLED else 'off'}")
    print(f"超期简历原文：{len(found['resumes'])} 条")
    for resume_id, path in found["resumes"][: max(0, args.list)]:
        print(f"  - {resume_id}  {os.path.basename(path)}")
    print(f"超期岗位原文：{len(found['jobs'])} 条")
    for job_id, path in found["jobs"][: max(0, args.list)]:
        print(f"  - {job_id}  {os.path.basename(path)}")

    if args.dry_run:
        report = purge(window, dry_run=True)
        print(f"[dry-run] 会标记归档 {report['archives_marked']} 条，其余不动。")
        return 0

    report = purge(window)
    print(f"已清理：简历 {report['resumes']} 条 / 岗位 {report['jobs']} 条 / "
          f"标记归档 {report['archives_marked']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
