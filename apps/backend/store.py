"""
JSON 文件存储。替代 SQLAlchemy + SQLite，零数据库依赖。

存储结构：
    data/resumes/<resume_id>.json   一个简历 = 一个文件（raw + processed 合并）
    data/jobs/<job_id>.json         一个 JD = 一个文件（raw + processed 合并）

设计要点（与旧版响应结构保持兼容）：
- 统一剥掉旧版 wrapper key（旧版 experiences 存 {"experiences":[...]}，这里直接存 [...]）
- 字段名用 snake_case（与旧版 model_dump 后入库的形态一致）
- 保留拼写陷阱 compensation_and_benfits（与旧版 key 完全一致，潜在消费者靠 try/except 兼容）
"""
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from config import RESUMES_DIR, JOBS_DIR, ARCHIVES_DIR


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: str, data: dict) -> None:
    """
    原子写：先写临时文件再 os.replace，掉电 / 多 worker 并发都不会产生半写文件。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 简历 ──────────────────────────────────────────────────────────────

def save_resume(content: str, processed: dict, user_id: str, content_type: str = "md") -> str:
    """保存简历（raw + processed + user_id 合并到一个文件），返回 resume_id。"""
    resume_id = str(uuid.uuid4())
    record = {
        "resume_id": resume_id,
        "user_id": user_id,
        "content": content,
        "content_type": content_type,
        "created_at": _now_iso(),
        "processed": processed,
    }
    _write_json(os.path.join(RESUMES_DIR, f"{resume_id}.json"), record)
    return resume_id


def get_resume(resume_id: str, user_id: str = "") -> Optional[dict]:
    """
    读取简历完整记录。
    当 user_id 非空时校验归属：不匹配时返回 None（跨用户隔离）。
    """
    record = _read_json(os.path.join(RESUMES_DIR, f"{resume_id}.json"))
    if record is None:
        return None
    if user_id and record.get("user_id") != user_id:
        return None
    return record


def get_resume_view(resume_id: str, user_id: str = "") -> Optional[dict]:
    """
    构造 GET /resumes 的响应结构（与旧版 get_resume_with_processed_data 兼容）。
    当 user_id 非空时校验归属。
    """
    rec = get_resume(resume_id, user_id=user_id)
    if not rec:
        return None
    p = rec.get("processed") or {}
    return {
        "resume_id": rec["resume_id"],
        "raw_resume": {
            # 兼容字段，恒为 0；列表渲染请用 resume_id。
            "id": 0,
            "content": rec.get("content", ""),
            "content_type": rec.get("content_type", "md"),
            "created_at": rec.get("created_at"),
        },
        "processed_resume": {
            # 字段缺失或 LLM 解析失败时回退到空 dict/list，避免前端拿到 null。
            "personal_data": p.get("personal_data") or {},
            "experiences": p.get("experiences") or [],
            "projects": p.get("projects") or [],
            "skills": p.get("skills") or [],
            "research_work": p.get("research_work") or [],
            "achievements": p.get("achievements") or [],
            "education": p.get("education") or [],
            "extracted_keywords": p.get("extracted_keywords") or [],
            "processed_at": p.get("processed_at"),
        },
    }


# ── 岗位 ──────────────────────────────────────────────────────────────

def save_job(resume_id: str, content: str, processed: dict, user_id: str) -> str:
    """保存 JD，返回 job_id。"""
    job_id = str(uuid.uuid4())
    record = {
        "job_id": job_id,
        "resume_id": resume_id,
        "user_id": user_id,
        "content": content,
        "created_at": _now_iso(),
        "processed": processed,
    }
    _write_json(os.path.join(JOBS_DIR, f"{job_id}.json"), record)
    return job_id


def get_job(job_id: str, user_id: str = "") -> Optional[dict]:
    """读取 JD 完整记录。当 user_id 非空时校验归属。"""
    record = _read_json(os.path.join(JOBS_DIR, f"{job_id}.json"))
    if record is None:
        return None
    if user_id and record.get("user_id") != user_id:
        return None
    return record


def get_job_view(job_id: str, user_id: str = "") -> Optional[dict]:
    """
    构造 GET /jobs 的响应结构（与旧版 get_job_with_processed_data 兼容）。
    当 user_id 非空时校验归属。
    """
    rec = get_job(job_id, user_id=user_id)
    if not rec:
        return None
    p = rec.get("processed") or {}
    return {
        "job_id": rec["job_id"],
        "raw_job": {
            "id": 0,  # 兼容字段，恒为 0
            "resume_id": rec.get("resume_id"),
            "content": rec.get("content", ""),
            "created_at": rec.get("created_at"),
        },
        "processed_job": {
            "job_title": p.get("job_title"),
            "company_profile": p.get("company_profile") or {},
            "location": p.get("location") or {},
            "date_posted": p.get("date_posted"),
            "employment_type": p.get("employment_type"),
            "job_summary": p.get("job_summary") or "",
            "key_responsibilities": p.get("key_responsibilities") or [],
            "qualifications": p.get("qualifications") or {},
            # 注意拼写：保留旧版的 "benfits" 以兼容全链路
            "compensation_and_benfits": p.get("compensation_and_benfits"),
            "application_info": p.get("application_info") or {},
            "extracted_keywords": p.get("extracted_keywords") or [],
            "processed_at": p.get("processed_at"),
        },
    }


# ── 处理后的结构化数据规范化 ──────────────────────────────────────────
# LLM 返回的是驼峰键（jobTitle/companyProfile/...），这里转成 snake_case
# 统一存数组/对象（剥掉旧版 wrapper key），与 get_resume_view/get_job_view 对齐。

_CAMEL_RESUME_MAP = {
    # 带空格标题（prompt 标准形态）
    "Personal Data": "personal_data",
    "Experiences": "experiences",
    "Projects": "projects",
    "Skills": "skills",
    "Research Work": "research_work",
    "Achievements": "achievements",
    "Education": "education",
    "Extracted Keywords": "extracted_keywords",
    # 兼容驼峰键
    "personalData": "personal_data",
    "researchWork": "research_work",
    "extractedKeywords": "extracted_keywords",
}

_CAMEL_JOB_MAP = {
    "jobTitle": "job_title",
    "companyProfile": "company_profile",
    "location": "location",
    "datePosted": "date_posted",
    "employmentType": "employment_type",
    "jobSummary": "job_summary",
    "keyResponsibilities": "key_responsibilities",
    "qualifications": "qualifications",
    "compensationAndBenefits": "compensation_and_benfits",
    "compensationAndBenfits": "compensation_and_benfits",
    "applicationInfo": "application_info",
    "extractedKeywords": "extracted_keywords",
}


def normalize_resume_structured(raw: dict) -> dict:
    """
    把 LLM 返回的结构化简历（带空格/驼峰键）归一化成 snake_case 存储格式。
    容错：LLM 可能返回 jobTitle 也可能返回 job_title，两种都接受。
    """
    out = {}
    for raw_key, std_key in _CAMEL_RESUME_MAP.items():
        val = raw.get(raw_key)
        if val is None:
            # 兼容已经是 snake_case 的返回
            val = raw.get(std_key)
        out[std_key] = val
    out["processed_at"] = _now_iso()
    return out


def normalize_job_structured(raw: dict) -> dict:
    """把 LLM 返回的结构化 JD 归一化成存储格式。"""
    out = {}
    for raw_key, std_key in _CAMEL_JOB_MAP.items():
        if std_key in out:
            continue
        val = raw.get(raw_key)
        if val is None:
            val = raw.get(std_key)
        out[std_key] = val
    out["processed_at"] = _now_iso()
    return out


# ── 归档 ──────────────────────────────────────────────────────────────

ARCHIVE_INDEX_PATH = os.path.join(ARCHIVES_DIR, "_index.json")


def _ensure_archive_index() -> dict:
    """读取或创建全局归档索引。"""
    index = _read_json(ARCHIVE_INDEX_PATH)
    if index is None:
        index = {}
        _write_json(ARCHIVE_INDEX_PATH, index)
    return index


def _update_archive_index(user_id: str, archive_id: str, action: str) -> None:
    """
    更新归档索引。
    action: add_active | add_trashed | remove_active | remove_trashed
            | move_to_trashed | move_to_active
    """
    index = _ensure_archive_index()
    if user_id not in index:
        index[user_id] = {"active": [], "trashed": []}

    if action == "add_active":
        if archive_id not in index[user_id]["active"]:
            index[user_id]["active"].append(archive_id)
    elif action == "add_trashed":
        if archive_id not in index[user_id]["trashed"]:
            index[user_id]["trashed"].append(archive_id)
    elif action == "remove_active":
        index[user_id]["active"] = [a for a in index[user_id]["active"] if a != archive_id]
    elif action == "remove_trashed":
        index[user_id]["trashed"] = [a for a in index[user_id]["trashed"] if a != archive_id]
    elif action == "move_to_trashed":
        index[user_id]["active"] = [a for a in index[user_id]["active"] if a != archive_id]
        if archive_id not in index[user_id]["trashed"]:
            index[user_id]["trashed"].append(archive_id)
    elif action == "move_to_active":
        index[user_id]["trashed"] = [a for a in index[user_id]["trashed"] if a != archive_id]
        if archive_id not in index[user_id]["active"]:
            index[user_id]["active"].append(archive_id)

    _write_json(ARCHIVE_INDEX_PATH, index)


def save_archive(
    user_id: str,
    resume_id: str,
    job_id: str,
    candidate_name: str,
    final_score: int,
    fit_tag: str,
    recruitment_recommendation: str,
    job_title: str,
    custom_tags: list[str] | None = None,
    analysis_snapshot: dict | None = None,
    category: str = "",
    full_analysis: dict | None = None,
) -> str:
    """创建归档记录，返回 archive_id。

    category：HR 自定义 / 预设岗位分类（行政、销售、IT、营销等），
    为空时回退为 job_title，保证筛选下拉始终有意义。
    full_analysis：完整 hr_analysis 结构，供人才库详情页重新生成报告快照。
    """
    archive_id = str(uuid.uuid4())
    record = {
        "archive_id": archive_id,
        "user_id": user_id,
        "resume_id": resume_id,
        "job_id": job_id,
        "candidate_name": candidate_name,
        "final_score": final_score,
        "fit_tag": fit_tag,
        "recruitment_recommendation": recruitment_recommendation,
        "job_title": job_title,
        "category": category or job_title,
        "custom_tags": custom_tags or [],
        "analysis_snapshot": analysis_snapshot or {},
        "analysis": full_analysis or {},
        "status": "active",
        "trashed_at": None,
        "created_at": _now_iso(),
    }
    _write_json(os.path.join(ARCHIVES_DIR, f"{archive_id}.json"), record)
    _update_archive_index(user_id, archive_id, "add_active")
    return archive_id


def find_existing_archive(user_id: str, resume_id: str, job_id: str) -> str | None:
    """按 (resume_id, job_id) 查找是否已归档，返回 archive_id 或 None。"""
    index = _ensure_archive_index()
    user_data = index.get(user_id, {})
    for status_key in ("active", "trashed"):
        for aid in user_data.get(status_key, []):
            rec = _read_json(os.path.join(ARCHIVES_DIR, f"{aid}.json"))
            if rec and rec.get("resume_id") == resume_id and rec.get("job_id") == job_id:
                return aid
    return None


def get_archive(archive_id: str, user_id: str = "") -> dict | None:
    """读取归档记录。当 user_id 非空时校验归属。"""
    record = _read_json(os.path.join(ARCHIVES_DIR, f"{archive_id}.json"))
    if record is None:
        return None
    if user_id and record.get("user_id") != user_id:
        return None
    return record


def list_archives(user_id: str, status: str = "active") -> list[dict]:
    """列出用户指定状态的归档记录（按 final_score 降序）。"""
    index = _ensure_archive_index()
    user_data = index.get(user_id, {})
    archive_ids = user_data.get(status, [])

    result = []
    for aid in archive_ids:
        rec = _read_json(os.path.join(ARCHIVES_DIR, f"{aid}.json"))
        if rec and rec.get("user_id") == user_id:
            result.append(rec)

    result.sort(key=lambda x: x.get("final_score", 0) or 0, reverse=True)
    return result


def query_archives(
    user_id: str,
    name_keyword: str = "",
    job_title: str = "",
    tag: str = "",
    category: str = "",
    sort: str = "score",
) -> list[dict]:
    """按条件查询 active 归档记录。"""
    archives = list_archives(user_id, status="active")

    if name_keyword:
        keyword = name_keyword.lower()
        archives = [a for a in archives if keyword in (a.get("candidate_name") or "").lower()]
    if job_title:
        archives = [a for a in archives if job_title.lower() in (a.get("job_title") or "").lower()]
    if category:
        archives = [
            a
            for a in archives
            if category.lower() in (a.get("category") or a.get("job_title") or "").lower()
        ]
    if tag:
        archives = [
            a
            for a in archives
            if tag.lower() in [t.lower() for t in (a.get("custom_tags") or [])]
        ]

    if sort == "created":
        archives.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    else:
        archives.sort(key=lambda x: x.get("final_score", 0) or 0, reverse=True)

    return archives


def get_distinct_job_titles(user_id: str) -> list[str]:
    """获取用户归档中所有不重复的岗位名称（用于前端筛选下拉）。"""
    active = list_archives(user_id, status="active")
    titles = set()
    for a in active:
        t = (a.get("job_title") or "").strip()
        if t:
            titles.add(t)
    return sorted(titles)


def get_distinct_categories(user_id: str) -> list[str]:
    """获取用户归档中所有不重复的岗位分类（category 优先，回退 job_title）。"""
    active = list_archives(user_id, status="active")
    cats = set()
    for a in active:
        c = (a.get("category") or a.get("job_title") or "").strip()
        if c:
            cats.add(c)
    return sorted(cats)


def soft_delete_archive(archive_id: str, user_id: str) -> bool:
    """软删除：移入回收站。"""
    rec = get_archive(archive_id, user_id=user_id)
    if not rec or rec.get("status") != "active":
        return False

    rec["status"] = "trashed"
    rec["trashed_at"] = _now_iso()
    _write_json(os.path.join(ARCHIVES_DIR, f"{archive_id}.json"), rec)
    _update_archive_index(user_id, archive_id, "move_to_trashed")
    return True


def restore_archive(archive_id: str, user_id: str) -> bool:
    """从回收站恢复到人才库。"""
    rec = get_archive(archive_id, user_id=user_id)
    if not rec or rec.get("status") != "trashed":
        return False

    rec["status"] = "active"
    rec["trashed_at"] = None
    _write_json(os.path.join(ARCHIVES_DIR, f"{archive_id}.json"), rec)
    _update_archive_index(user_id, archive_id, "move_to_active")
    return True


def permanent_delete_archive(archive_id: str, user_id: str) -> bool:
    """彻底删除归档记录。"""
    rec = get_archive(archive_id, user_id=user_id)
    if not rec or rec.get("status") != "trashed":
        return False

    # 删除 JSON 明细
    json_path = os.path.join(ARCHIVES_DIR, f"{archive_id}.json")
    if os.path.exists(json_path):
        os.remove(json_path)

    _update_archive_index(user_id, archive_id, "remove_trashed")
    return True


def empty_trash(user_id: str) -> int:
    """清空回收站，返回删除数量。"""
    trashed = list_archives(user_id, status="trashed")
    count = 0
    for rec in trashed:
        if permanent_delete_archive(rec["archive_id"], user_id):
            count += 1
    return count


def update_archive_tags(archive_id: str, user_id: str, custom_tags: list[str]) -> bool:
    """更新自定义标签（仅 active 状态可操作）。"""
    rec = get_archive(archive_id, user_id=user_id)
    if not rec or rec.get("status") != "active":
        return False

    rec["custom_tags"] = custom_tags
    _write_json(os.path.join(ARCHIVES_DIR, f"{archive_id}.json"), rec)
    return True


def update_archive_category(archive_id: str, user_id: str, category: str) -> bool:
    """更新岗位分类（仅 active 状态可操作）。"""
    rec = get_archive(archive_id, user_id=user_id)
    if not rec or rec.get("status") != "active":
        return False

    rec["category"] = category or rec.get("job_title", "")
    _write_json(os.path.join(ARCHIVES_DIR, f"{archive_id}.json"), rec)
    return True
