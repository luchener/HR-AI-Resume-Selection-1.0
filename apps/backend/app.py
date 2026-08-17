"""
Flask 后端应用。极简化重构，替代旧版 FastAPI + SQLAlchemy + Agent 抽象层。

主要路由：
    GET  /ping                                  健康检查
    POST /api/v1/ai/test                        测试请求级模型配置
    POST /api/v1/resumes/upload                 上传简历（multipart）
    POST /api/v1/resumes/hr-analysis            单人/多人招聘筛选
    POST /api/v1/resumes/improve                分析简历（?stream=true 走 SSE）
    GET  /api/v1/resumes?resume_id=             获取简历
    POST /api/v1/resumes/improved-markdown      提取优化后简历 markdown
    POST /api/v1/jobs/upload                    上传 JD（JSON，手动校验 Content-Type）
    GET  /api/v1/jobs?job_id=                   获取 JD

启动：gunicorn app:app（宝塔/生产）或 python run.py（本地）。
"""
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from flask import Flask, request, jsonify, Response, stream_with_context

import config
from config import ALLOWED_ORIGINS
import store
import llm
import parser as doc_parser
from prompts import (
    PROMPT_HR_JUDGE,
    PROMPT_HR_RECRUITMENT_ANALYSIS,
)

# ── 日志（标准库，去掉复杂轮转）──────────────────────────────────────
logging.basicConfig(
    level=logging.INFO if config.ENV == "production" else logging.DEBUG,
    format="[%(asctime)s - %(name)s - %(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger("resume-matcher")
if not any(isinstance(handler, logging.FileHandler) for handler in logger.handlers):
    file_handler = logging.FileHandler(
        os.path.join(config.LOG_DIR, "backend.log"),
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("[%(asctime)s - %(name)s - %(levelname)s] %(message)s")
    )
    logger.addHandler(file_handler)
for noisy_logger in ("openai", "httpx", "httpcore"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

# ── 生产环境启动校验 ─────────────────────────────────────────────────
config.check_production()

app = Flask(__name__)
# Leave a little room for multipart headers, then enforce the exact file limit
# after Flask has parsed the uploaded part.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
MAX_RESUME_FILE_SIZE = 30 * 1024 * 1024
_HR_ANALYSIS_VERSION = "screening-v16-employment-timeline"
_HR_ANALYSIS_CACHE: dict[tuple[str, str, str, str], dict] = {}


@app.errorhandler(413)
def _handle_payload_too_large(_error):
    return _err("Uploaded file exceeds the 30 MB limit", 413, "resumes")


# ── CORS（after_request，简单可靠）──────────────────────────────────
@app.after_request
def _cors(resp: Response) -> Response:
    if request.path.startswith("/api/v1/"):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"

    origin = request.headers.get("Origin")
    if origin and origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "*"
    return resp


# ── CORS 预检：Flask 默认对未注册 OPTIONS 返回 405，
#    浏览器在跨域 + Content-Type: application/json 场景下会先发预检，
#    没有这个处理器整个跨域 POST 都会被拦截。
@app.before_request
def _preflight():
    if request.method == "OPTIONS":
        resp = Response()
        origin = request.headers.get("Origin")
        if origin and origin in ALLOWED_ORIGINS:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            req_methods = request.headers.get("Access-Control-Request-Method")
            resp.headers["Access-Control-Allow-Methods"] = req_methods or "GET,POST,OPTIONS"
            req_headers = request.headers.get("Access-Control-Request-Headers")
            resp.headers["Access-Control-Allow-Headers"] = req_headers or "*"
        return resp


# ── 统一错误类型：替代 _do_improve 的 (jsonify, status) tuple 模式 ────
class ApiError(Exception):
    """业务可主动抛的 HTTP 错误。message / status / service 用于响应构造。"""

    def __init__(self, message: str, status: int = 400, service: str = "api"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.service = service


@app.errorhandler(ApiError)
def _handle_api_error(e: ApiError):
    return _err(e.message, e.status, e.service)


# ── request_id 工具（与旧版格式兼容：服务段:uuid）────────────────────
def _request_id(service: str = "api") -> str:
    return f"{service}:{uuid.uuid4()}"


def _err(detail: str, status: int, service: str = "api"):
    """统一错误响应：{detail, request_id}"""
    return jsonify({"detail": detail, "request_id": _request_id(service)}), status


def _request_ai_config(data: dict, *, required: bool = False) -> dict | None:
    value = data.get("ai_config")
    if value is None:
        if required:
            raise ApiError("请先配置 AI 模型。", 422, "ai")
        return None
    try:
        return llm.normalize_runtime_config(value)
    except ValueError as exc:
        raise ApiError(str(exc), 422, "ai") from exc


# ════════════════════════════════════════════════════════════════════
# 健康检查
# ════════════════════════════════════════════════════════════════════
@app.get("/ping")
def ping():
    return jsonify({"message": "pong", "database": "reachable"})


@app.post("/api/v1/ai/test")
def test_ai_model():
    """Verify a browser-supplied model configuration without persisting it."""
    data = request.get_json(silent=True) or {}
    ai_config = _request_ai_config(data, required=True)
    started = time.perf_counter()
    try:
        llm.call_llm(
            "这是一次连接测试。请只回复 OK。",
            max_tokens=8,
            runtime_config=ai_config,
        )
    except Exception as exc:
        logger.warning("AI model connection test failed: %s", type(exc).__name__)
        raise ApiError(
            "AI 模型连接失败，请检查 API Key、接口地址与模型名称。",
            502,
            "ai",
        ) from exc

    return jsonify(
        {
            "request_id": _request_id("ai"),
            "data": {
                "ok": True,
                "provider": ai_config["provider"],
                "model": ai_config["model"],
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            },
        }
    )


# ════════════════════════════════════════════════════════════════════
# 简历接口
# ════════════════════════════════════════════════════════════════════
@app.post("/api/v1/resumes/upload")
def upload_resume():
    """上传 PDF/DOCX 简历，仅在本地解析文本并存储。"""
    rid = _request_id("resumes")

    f = request.files.get("file")
    if not f or not f.filename:
        return _err("No file provided", 400, "resumes")

    file_bytes = f.read()
    if len(file_bytes) > MAX_RESUME_FILE_SIZE:
        return _err("Uploaded file exceeds the 30 MB limit", 413, "resumes")
    filename = f.filename.lower()
    content_type = f.mimetype or f.content_type or ""
    # Windows clients and some security proxies upload Office files as
    # application/octet-stream. Confirm the type using extension and magic bytes.
    if filename.endswith(".pdf") and file_bytes.startswith(b"%PDF"):
        content_type = "application/pdf"
    elif filename.endswith(".docx") and file_bytes.startswith(b"PK"):
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    # 1. 提取文本
    try:
        text = doc_parser.extract_text_from_file(file_bytes, content_type)
    except ValueError as e:
        logger.warning(f"resume parse failed: {e}")
        return _err(str(e), 400, "resumes")
    except Exception as e:
        logger.error(f"resume parse error: {e}", exc_info=True)
        return _err(f"File conversion failed: {e}", 400, "resumes")

    # 上传链路不调用 LLM，避免用户等待 60-90 秒；招聘分析阶段一次性处理。
    resume_id = store.save_resume(content=text, processed={})

    return jsonify(
        {
            "message": "Resume uploaded and processed as MD successfully",
            "request_id": rid,
            "resume_id": resume_id,
            "extracted_characters": len(text),
        }
    )


@app.post("/api/v1/resumes/improve")
def improve_resume():
    """分析简历 vs JD。?stream=true 走 SSE 流式。"""
    rid = _request_id("resumes")
    stream = request.args.get("stream", "false").lower() in ("true", "1", "yes")

    data = request.get_json(silent=True) or {}
    resume_id = data.get("resume_id")
    job_id = data.get("job_id")
    if not resume_id or not job_id:
        return _err("resume_id and job_id are required", 422, "resumes")
    ai_config = _request_ai_config(data)

    if stream:
        return Response(
            stream_with_context(_improve_stream(resume_id, job_id, rid, ai_config)),
            mimetype="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    # 非流式：异常走全局 errorhandler，类型安全
    result = _do_improve(resume_id, job_id, rid, ai_config)
    return jsonify({"request_id": rid, "data": result})


def _do_improve(resume_id: str, job_id: str, rid: str, ai_config: dict | None = None) -> dict:
    """
    执行分析（核心逻辑，非流式与流式共用）。
    成功返回 dict；失败抛 ApiError，由全局 errorhandler 统一序列化。
    """
    resume = store.get_resume(resume_id)
    if not resume:
        raise ApiError(f"Resume not found: {resume_id}", 404, "resumes")
    job = store.get_job(job_id)
    if not job:
        raise ApiError(f"Job not found: {job_id}", 404, "resumes")

    try:
        prompt = PROMPT_HR_JUDGE.format(
            Job_Description=job.get("content", ""),
            raw_resume=resume.get("content", ""),
            datetime=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        analysis_result = llm.call_llm(
            prompt,
            expect_json=False,
            runtime_config=ai_config,
        )
    except ApiError:
        raise
    except Exception as e:
        logger.error(f"improve LLM call failed: {e}", exc_info=True)
        raise ApiError(f"Analysis failed: {e}", 500, "resumes") from e

    return {
        "resume_id": resume_id,
        "job_id": job_id,
        "analysis_result": analysis_result,
        "details": "Analysis completed successfully using hr_judge prompt template.",
        "commentary": "The resume has been analyzed against the job description using the hr_judge prompt template.",
    }


def _improve_stream(
    resume_id: str,
    job_id: str,
    rid: str,
    ai_config: dict | None = None,
):
    """SSE 生成器。严格照搬旧版事件格式：data: {json}\\n\\n"""
    def sse(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    try:
        yield sse({"status": "starting", "message": "Analyzing resume and job description..."})

        resume = store.get_resume(resume_id)
        if not resume:
            yield sse({"status": "error", "message": f"Resume not found: {resume_id}"})
            return
        job = store.get_job(job_id)
        if not job:
            yield sse({"status": "error", "message": f"Job not found: {job_id}"})
            return

        yield sse({"status": "parsing", "message": "Preparing analysis with hr_judge prompt..."})

        prompt = PROMPT_HR_JUDGE.format(
            Job_Description=job.get("content", ""),
            raw_resume=resume.get("content", ""),
            datetime=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        yield sse({"status": "analyzing", "message": "Running analysis with LLM..."})

        analysis_result = llm.call_llm(
            prompt,
            expect_json=False,
            runtime_config=ai_config,
        )

        final_result = {
            "resume_id": resume_id,
            "job_id": job_id,
            "analysis_result": analysis_result,
            "details": "Analysis completed successfully using hr_judge prompt template.",
            "commentary": "The resume has been analyzed against the job description using the hr_judge prompt template.",
        }
        # completed 的 result 双层包装，与非流式响应同构
        yield sse({"status": "completed", "result": {"request_id": rid, "data": final_result}})
    except Exception as e:
        logger.error(f"improve stream failed: {e}", exc_info=True)
        yield sse({"status": "error", "message": str(e)})


_AI_RISK_RULES = {
    "none": (0, 0, "无AI痕迹", "简历真实自然"),
    "light": (5, 10, "轻微AI美化", "轻度AI润色，轻微包装"),
    "medium": (15, 20, "中度AI美化", "中度AI包装，真实性一般"),
    "high": (30, 30, "重度AI/模板生成", "重度AI生成，内容可信度低"),
}


def _as_int(value, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _short_list(value, limit: int = 3, item_limit: int = 90) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:item_limit] for item in value if str(item).strip()][:limit]


def _as_text(value, default: str = "未提供") -> str:
    text = str(value or "").strip()
    return text[:120] if text else default


def _as_section(value, fields: tuple[str, ...]) -> dict:
    source = value if isinstance(value, dict) else {}
    return {field: _as_text(source.get(field)) for field in fields}


def _normalize_month_label(value, *, allow_present: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return "未提供"
    if allow_present and text.lower() in {"至今", "目前", "在职", "present", "current", "now"}:
        return "至今"

    match = re.search(
        r"(?<!\d)((?:19|20)\d{2})\s*[-./年]\s*(1[0-2]|0?[1-9])(?:\s*月)?(?!\d)",
        text,
    )
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"

    year_match = re.fullmatch(r"\s*((?:19|20)\d{2})\s*年?\s*", text)
    if year_match:
        return year_match.group(1)
    return _as_text(text)


def _month_index(value: str, current: datetime) -> int | None:
    if value == "至今":
        return current.year * 12 + current.month - 1
    match = re.fullmatch(r"((?:19|20)\d{2})-(1[0-2]|0[1-9])", value)
    if not match:
        return None
    return int(match.group(1)) * 12 + int(match.group(2)) - 1


def _month_label(index: int) -> str:
    year, zero_based_month = divmod(index, 12)
    return f"{year:04d}-{zero_based_month + 1:02d}"


def _month_duration(months: int) -> str:
    years, remainder = divmod(max(0, months), 12)
    if years and remainder:
        return f"{years} 年 {remainder} 个月"
    if years:
        return f"{years} 年"
    return f"{remainder} 个月"


def _normalize_employment_records(value) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    current = datetime.now()
    records: list[dict[str, str]] = []
    for item in value[:_EMPLOYMENT_RECORD_LIMIT]:
        if not isinstance(item, dict):
            continue
        company_name = item.get("company_name") or item.get("company")
        job_title = item.get("job_title") or item.get("position") or item.get("title")
        start_value = item.get("start_date") or item.get("start_time")
        end_value = item.get("end_date") or item.get("end_time")
        if not any((company_name, job_title, start_value, end_value)):
            continue

        start_date = _normalize_month_label(start_value)
        end_date = _normalize_month_label(end_value, allow_present=True)
        start_index = _month_index(start_date, current)
        end_index = _month_index(end_date, current)
        duration = "未提供"
        if start_index is not None and end_index is not None and start_index <= end_index:
            duration = _month_duration(end_index - start_index + 1)

        records.append(
            {
                "company_name": _as_text(company_name),
                "job_title": _as_text(job_title),
                "start_date": start_date,
                "end_date": end_date,
                "duration": duration,
            }
        )

    records.sort(
        key=lambda record: _month_index(record["start_date"], current) or -1,
        reverse=True,
    )
    return records


def _extract_employment_records(resume_content: str) -> list[dict[str, str]]:
    """Extract structured employment rows when the model omits them."""
    lines = [
        re.sub(r"[ \t\u3000]+", " ", line).strip()
        for line in str(resume_content or "").replace("\r\n", "\n").split("\n")
    ]
    lines = [line for line in lines if line]
    if not lines:
        return []

    section_start = next(
        (index for index, line in enumerate(lines) if line.rstrip("：:") in {"工作经历", "工作经验", "任职经历"}),
        -1,
    )
    section_end = len(lines)
    if section_start >= 0:
        for index in range(section_start + 1, len(lines)):
            if lines[index].rstrip("：:") in {
                "教育经历", "教育背景", "项目经历", "项目经验", "专业技能",
                "技能特长", "证书与荣誉", "荣誉奖项", "自我评价",
            }:
                section_end = index
                break
        searchable_lines = lines[section_start + 1:section_end]
    else:
        searchable_lines = lines

    extracted: list[dict[str, str]] = []
    for index, line in enumerate(searchable_lines):
        date_match = _EMPLOYMENT_DATE_RANGE_RE.search(line)
        if not date_match:
            continue

        header_candidates: list[tuple[int, str]] = []
        inline_header = line[:date_match.start()].strip(" |-—–~～·")
        if inline_header:
            header_candidates.append((index, inline_header))
        lookback_start = max(0, index - 6)
        header_candidates.extend(
            (candidate_index, searchable_lines[candidate_index])
            for candidate_index in range(index - 1, lookback_start - 1, -1)
        )

        company_name = ""
        job_title = ""
        for _candidate_index, candidate in header_candidates:
            header_match = _EMPLOYMENT_HEADER_RE.fullmatch(candidate)
            if header_match:
                company_name = header_match.group("company").strip()
                job_title = header_match.group("title").strip()
                break

        if not company_name:
            for candidate_index, candidate in header_candidates:
                company_match = _EMPLOYMENT_COMPANY_RE.fullmatch(candidate)
                if not company_match:
                    continue
                company_name = company_match.group("company").strip()
                nearby_lines = searchable_lines[candidate_index + 1:index]
                job_title = next(
                    (
                        nearby.strip()
                        for nearby in nearby_lines
                        if _looks_like_job_title(nearby)
                    ),
                    "未提供",
                )
                break

        if not company_name:
            continue
        extracted.append(
            {
                "company_name": company_name,
                "job_title": job_title or "未提供",
                "start_date": date_match.group("start"),
                "end_date": date_match.group("end"),
            }
        )

    return _normalize_employment_records(extracted)


def _looks_like_job_title(value: str) -> bool:
    text = str(value or "").strip()
    if not text or text.rstrip("：:") in {"内容", "职责", "业绩", "工作内容", "工作职责"}:
        return False
    return bool(re.search(r"工程师|经理|主管|总监|负责人|顾问|专员|助理|实习|设计师|开发|运维|销售|会计|教师", text))


def _merge_employment_records(
    extracted_records: list[dict[str, str]],
    model_records: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Prefer resume text for factual fields, then add model-only periods."""
    merged = [dict(record) for record in extracted_records]
    keyed = {
        (record["start_date"], record["end_date"]): record
        for record in merged
        if record["start_date"] != "未提供" and record["end_date"] != "未提供"
    }
    for model_record in model_records:
        key = (model_record["start_date"], model_record["end_date"])
        existing = keyed.get(key)
        if existing:
            for field in ("company_name", "job_title"):
                if existing[field] == "未提供" and model_record[field] != "未提供":
                    existing[field] = model_record[field]
            continue
        merged.append(dict(model_record))
        if key[0] != "未提供" and key[1] != "未提供":
            keyed[key] = merged[-1]
    return _normalize_employment_records(merged)


def _calculate_employment_gaps(records: list[dict[str, str]], gap_note: str = "") -> str:
    """Calculate calendar-month gaps after merging overlapping employment periods."""
    if not records:
        return "未提供"

    current = datetime.now()
    current_index = current.year * 12 + current.month - 1
    intervals: list[tuple[int, int]] = []
    incomplete = False
    for record in records:
        start_index = _month_index(record["start_date"], current)
        end_index = _month_index(record["end_date"], current)
        if (
            start_index is None
            or end_index is None
            or start_index > end_index
            or start_index > current_index
            or end_index > current_index
        ):
            incomplete = True
            continue
        intervals.append((start_index, end_index))

    if incomplete:
        summary = "部分经历缺少精确月份，无法完整核算空窗期"
        if gap_note and gap_note != "未提供":
            summary += f"；简历说明：{gap_note}"
        return summary

    intervals.sort()
    merged: list[list[int]] = []
    for start_index, end_index in intervals:
        if not merged or start_index > merged[-1][1] + 1:
            merged.append([start_index, end_index])
        else:
            merged[-1][1] = max(merged[-1][1], end_index)

    gaps: list[tuple[int, int, int, bool]] = []
    for previous, following in zip(merged, merged[1:]):
        gap_start = previous[1] + 1
        gap_end = following[0] - 1
        if gap_start <= gap_end:
            gaps.append((gap_start, gap_end, gap_end - gap_start + 1, False))

    if merged[-1][1] < current_index:
        gap_start = merged[-1][1] + 1
        gaps.append((gap_start, current_index, current_index - gap_start + 1, True))

    if gaps:
        total_months = sum(gap[2] for gap in gaps)
        details = "；".join(
            f"{_month_label(start)} 至 {_month_label(end)}（{_month_duration(months)}{'，当前' if is_current else ''}）"
            for start, end, months, is_current in gaps
        )
        summary = f"共 {len(gaps)} 段，累计 {_month_duration(total_months)}：{details}"
    else:
        summary = f"无空窗期（任职时间连续，截至 {_month_label(current_index)}）"

    if gap_note and gap_note != "未提供":
        summary += f"；简历说明：{gap_note}"
    return summary


def _list_or_default(value, default: str, limit: int = 3) -> list[str]:
    return _short_list(value, limit) or [default]


def _normalize_skill_labels(value) -> list[str]:
    label_aliases = {
        "[直接匹配]": "[符合要求]",
        "[完全匹配]": "[符合要求]",
        "[可迁移]": "[相关经验]",
        "[部分匹配]": "[相关经验]",
        "[缺失]": "[简历未体现]",
    }
    items = _short_list(value, 5)
    normalized: list[str] = []
    for item in items:
        for old_label, new_label in label_aliases.items():
            if item.startswith(old_label):
                item = new_label + item[len(old_label):]
                break
        normalized.append(item)
    return normalized


def _compact_analysis_text(value: str, max_chars: int) -> str:
    """Remove extraction noise while preserving evidence from both ends."""
    unique_lines: list[str] = []
    seen: set[str] = set()
    for raw_line in str(value or "").replace("\r\n", "\n").split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", raw_line).strip()
        if not line or line in seen:
            continue
        if _looks_like_extraction_noise(line):
            continue
        seen.add(line)
        unique_lines.append(line)

    compacted = "\n".join(unique_lines)
    if len(compacted) <= max_chars:
        return compacted
    head_size = int(max_chars * 0.78)
    tail_size = max_chars - head_size
    return f"{compacted[:head_size]}\n...[中间重复或超长内容已压缩]...\n{compacted[-tail_size:]}"


_SHORT_SKILL_TOKENS = {
    "AI", "BI", "C", "C#", "C++", "ERP", "Go", "HR", "IT", "IoT",
    "OA", "PMP", "R", "SQL", "UI", "UG", "UX",
}


def _looks_like_extraction_noise(line: str) -> bool:
    """Detect common PDF watermark/font-map fragments without dropping skills."""
    if re.search(r"[\u4e00-\u9fff]", line):
        return False
    if line in _SHORT_SKILL_TOKENS:
        return False
    if len(line) <= 2 and line.isascii():
        return True

    tokens = line.split()
    simplified = [re.sub(r"[^A-Za-z0-9+#.-]", "", token).strip(".-") for token in tokens]
    simplified = [token for token in simplified if token]
    if len(simplified) >= 3:
        short_ratio = sum(len(token) <= 3 for token in simplified) / len(simplified)
        if short_ratio >= 0.75:
            return True

    collapsed = re.sub(r"[^A-Za-z0-9]", "", line)
    if len(collapsed) >= 24:
        digit_count = sum(char.isdigit() for char in collapsed)
        letter_count = sum(char.isalpha() for char in collapsed)
        if digit_count >= 5 and letter_count >= 5 and "@" not in line:
            return True
    return False


_BASIC_SCREENING_FIELDS = (
    "highest_education", "school_name", "school_tier", "education_type", "major_match",
    "graduation_year", "fresh_graduate", "age", "gender", "work_location",
    "salary_expectation",
)
_WORK_HISTORY_FIELDS = (
    "total_years", "relevant_years", "industry_match", "company_background",
    "seniority", "team_size", "stability", "employment_gaps",
    "responsibility_match",
)
_EMPLOYMENT_RECORD_LIMIT = 12
_EMPLOYMENT_DATE_TOKEN = r"(?:19|20)\d{2}\s*[-./年]\s*(?:1[0-2]|0?[1-9])(?:\s*月)?"
_EMPLOYMENT_DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_EMPLOYMENT_DATE_TOKEN})\s*(?:-|—|–|~|～|至)\s*"
    rf"(?P<end>至今|目前|在职|present|current|{_EMPLOYMENT_DATE_TOKEN})",
    re.IGNORECASE,
)
_EMPLOYMENT_COMPANY_SUFFIX = r"(?:有限责任公司|股份有限公司|有限公司|集团公司|集团|公司|研究院|事务所|中心|工作室|厂)"
_EMPLOYMENT_HEADER_RE = re.compile(
    rf"(?P<company>.+?{_EMPLOYMENT_COMPANY_SUFFIX})\s+(?P<title>.+)"
)
_EMPLOYMENT_COMPANY_RE = re.compile(rf"(?P<company>.+?{_EMPLOYMENT_COMPANY_SUFFIX})")

_SCORE_BREAKDOWN_LIMITS = {
    "hard_requirements": 25,
    "responsibility_overlap": 25,
    "skills_projects": 25,
    "industry_background": 15,
    "evidence_bonus": 10,
}


def _normalize_score_breakdown(value) -> tuple[dict[str, int], bool]:
    source = value if isinstance(value, dict) else {}
    normalized: dict[str, int] = {}
    complete = True
    for field, maximum in _SCORE_BREAKDOWN_LIMITS.items():
        try:
            score = int(round(float(source.get(field))))
        except (TypeError, ValueError):
            score = 0
            complete = False
        normalized[field] = max(0, min(maximum, score))
    return normalized, complete


def _normalize_hr_analysis(raw: dict, job_content: str = "", resume_content: str = "") -> dict:
    """Enforce the scoring formula and all allowed deduction/grade ranges."""
    score_breakdown, has_complete_breakdown = _normalize_score_breakdown(raw.get("score_breakdown"))
    base_score = (
        sum(score_breakdown.values())
        if has_complete_breakdown
        else max(0, min(100, _as_int(raw.get("job_fit_score"))))
    )
    risk = str(raw.get("ai_risk") or "none").strip().lower()
    if risk not in _AI_RISK_RULES:
        risk = "none"

    minimum, maximum, risk_level, risk_label = _AI_RISK_RULES[risk]
    proposed = _as_int(raw.get("ai_deduction"), minimum)
    deduction = max(minimum, min(maximum, proposed))
    final_score = max(0, base_score - deduction)

    if final_score >= 90:
        grade = "S级（优质适配）"
    elif final_score >= 80:
        grade = "A级（良好适配）"
    elif final_score >= 70:
        grade = "B级（基本适配）"
    elif final_score >= 60:
        grade = "C级（适配一般）"
    else:
        grade = "D级（不适配）"

    score_recommendation = "优先面试" if final_score >= 80 else "储备观察" if final_score >= 60 else "淘汰"
    requested_recommendation = str(raw.get("recruitment_recommendation") or score_recommendation).strip()
    if requested_recommendation == "酌情考虑":
        requested_recommendation = "储备观察"
    if requested_recommendation not in {"优先面试", "储备观察", "淘汰"}:
        requested_recommendation = score_recommendation
    recommendation_rank = {"淘汰": 0, "储备观察": 1, "优先面试": 2}
    if recommendation_rank[requested_recommendation] > recommendation_rank[score_recommendation]:
        requested_recommendation = score_recommendation

    score_fit_tag = "高匹配" if final_score >= 80 else "部分匹配" if final_score >= 60 else "不匹配"
    requested_fit_tag = str(raw.get("fit_tag") or score_fit_tag).strip()
    if requested_fit_tag not in {"高匹配", "部分匹配", "不匹配"}:
        requested_fit_tag = score_fit_tag
    fit_rank = {"不匹配": 0, "部分匹配": 1, "高匹配": 2}
    if fit_rank[requested_fit_tag] > fit_rank[score_fit_tag]:
        requested_fit_tag = score_fit_tag

    basic_screening = _as_section(raw.get("basic_screening"), _BASIC_SCREENING_FIELDS)
    raw_work_history = raw.get("work_history") if isinstance(raw.get("work_history"), dict) else {}
    work_history = _as_section(raw_work_history, _WORK_HISTORY_FIELDS)
    model_employment_records = _normalize_employment_records(raw_work_history.get("employment_records"))
    extracted_employment_records = _extract_employment_records(resume_content)
    employment_records = _merge_employment_records(
        extracted_employment_records,
        model_employment_records,
    )
    work_history["employment_records"] = employment_records
    if employment_records:
        gap_note = _as_text(raw_work_history.get("employment_gap_notes"))
        work_history["employment_gaps"] = _calculate_employment_gaps(employment_records, gap_note)
    raw_skill_match = raw.get("skill_match") if isinstance(raw.get("skill_match"), dict) else {}
    skill_match = {
        "hard_skills": _normalize_skill_labels(raw_skill_match.get("hard_skills")) or ["[简历未体现] 未找到可确认的岗位核心技能证据。"],
        "project_match_points": _list_or_default(raw_skill_match.get("project_match_points"), "简历未提供可与 JD 直接对应的项目或工作成果。", 5),
        "soft_skills": _list_or_default(raw_skill_match.get("soft_skills"), "简历未提供可确认的软实力证据。", 3),
    }

    risk_points = _short_list(raw.get("risk_points"), 6)
    if not re.search(r"薪资|薪酬|工资|预算|待遇|\d+\s*[kK]", job_content):
        risk_points = [
            item for item in risk_points
            if not re.search(r"薪资|薪酬|工资|预算|待遇", item)
        ]

    summary = str(raw.get("summary") or "未提供综合判定说明。").strip()[:240]
    return {
        "candidate_name": _as_text(raw.get("candidate_name")),
        "final_score": final_score,
        "fit_grade": grade,
        "job_fit_score": base_score,
        "job_fit_percentage": base_score,
        "score_breakdown": score_breakdown,
        "ai_risk": risk,
        "ai_risk_level": risk_level,
        "ai_risk_label": risk_label,
        "ai_deduction": deduction,
        "summary": summary,
        "basic_screening": basic_screening,
        "work_history": work_history,
        "skill_match": skill_match,
        "certificates": _list_or_default(raw.get("certificates"), "简历未提供证书资质信息。", 5),
        "bonus_items": _list_or_default(raw.get("bonus_items"), "简历未提供明确加分项。", 5),
        "strengths": _list_or_default(raw.get("strengths"), "简历未提供可确认的核心优势证据。", 5),
        "weaknesses": _list_or_default(raw.get("weaknesses"), "简历未提供足够信息，无法确认关键短板。", 5),
        "risk_points": risk_points or ["未发现明显履历风险；关键事实仍建议在面试中核验。"],
        "role_specific_assessment": _list_or_default(raw.get("role_specific_assessment"), "当前岗位无额外专项判断。", 4),
        "deduction_reasons": _short_list(raw.get("deduction_reasons"), 3),
        "recruitment_recommendation": requested_recommendation,
        "fit_tag": requested_fit_tag,
    }


def _hr_analysis_markdown(result: dict) -> str:
    reasons = result["deduction_reasons"] or ["未发现需要扣分的明显 AI 包装依据"]
    skills = result["skill_match"]
    basic = result["basic_screening"]
    history = result["work_history"]
    employment_records = history.get("employment_records") or []
    employment_lines = [
        f"- {record['company_name']} / {record['job_title']}：{record['start_date']} 至 {record['end_date']}"
        for record in employment_records
    ] or ["- 工作经历明细：未提供"]
    return "\n".join(
        [
            "# HR 招聘分析报告",
            f"1. **简历最终得分 + 适配等级**：{result['final_score']} 分，{result['fit_grade']}",
            f"2. **适配标签**：{result['fit_tag']}；招聘建议：{result['recruitment_recommendation']}",
            f"3. **AI美化风险等级**：{result['ai_risk_level']}（扣 {result['ai_deduction']} 分，{result['ai_risk_label']}）",
            f"4. **核心判定简要说明**：{result['summary']}",
            "## 基础信息",
            f"- 学历：{basic['highest_education']} / {basic['school_name']} / {basic['school_tier']} / {basic['major_match']}",
            f"- 毕业与地点：{basic['graduation_year']} / {basic['work_location']}；薪资：{basic['salary_expectation']}",
            "## 工作履历",
            f"- 年限：总计 {history['total_years']}；相关岗位 {history['relevant_years']}；行业匹配 {history['industry_match']}",
            f"- 履历稳定性：{history['stability']}；职责重合度：{history['responsibility_match']}",
            *employment_lines,
            f"- 空窗期：{history['employment_gaps']}",
            "## 技能与项目匹配",
            "- 核心技能：" + "；".join(skills['hard_skills']),
            "- 项目匹配：" + "；".join(skills['project_match_points']),
            "## 优势与短板",
            "- 优势：" + "；".join(result['strengths']),
            "- 短板：" + "；".join(result['weaknesses']),
            "- 证书资质：" + "；".join(result['certificates']),
            "- 岗位专项判断：" + "；".join(result['role_specific_assessment']),
            "## 风险预警",
            "- " + "；".join(result['risk_points']),
            "- AI 风险依据：" + "；".join(reasons),
        ]
    )


def _build_raw_resume_markdown(content: str) -> str:
    """Convert locally extracted resume text into A4 Studio markdown."""
    lines = [line.strip() for line in (content or "").replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    section_names = {
        "个人简介", "个人概况", "职业概况", "自我评价", "工作经验", "工作经历",
        "项目经验", "项目经历", "教育背景", "教育经历", "专业技能", "技能特长",
        "技能", "证书与荣誉", "荣誉奖项", "培训经历", "论文发表", "作品集",
    }
    out = [f"# {lines[0]}"]
    index = 1

    # Common DOCX extraction shape: name -> 联系方式 -> contact line.
    if index < len(lines) and lines[index].rstrip("：:") in {"联系方式", "联系信息"}:
        index += 1
        if index < len(lines):
            out.extend(["", f"> {lines[index]}"])
            index += 1
    elif index < len(lines) and lines[index] not in section_names:
        out.extend(["", f"## {lines[index]}"])
        index += 1

    current_section = False
    for line in lines[index:]:
        normalized = line.rstrip("：:")
        if normalized in section_names:
            out.extend(["", f"## {normalized}"])
            current_section = True
            continue
        if not current_section:
            out.extend(["", line])
            continue
        if line.startswith(("•", "·", "●", "▪", "-", "*")):
            out.append(f"- {line.lstrip('•·●▪-* ').strip()}")
        elif "\t" in line:
            title, meta = (part.strip() for part in line.split("\t", 1))
            out.append(f"### {title} | {meta}" if meta else f"### {title}")
        else:
            out.append(line)

    return "\n".join(out).rstrip() + "\n"


def _resume_studio_markdown(resume: dict) -> str:
    processed = resume.get("processed") or {}
    if any(processed.get(key) for key in ("personal_data", "experiences", "projects", "education", "skills")):
        return _normalize_md_for_a4cv(_build_fallback_markdown(processed))
    return _normalize_md_for_a4cv(_build_raw_resume_markdown(resume.get("content", "")))


def _run_hr_analysis(resume_id: str, job_id: str, ai_config: dict | None = None) -> dict:
    config_fingerprint = llm.model_config_fingerprint(ai_config)
    cache_key = (_HR_ANALYSIS_VERSION, resume_id, job_id, config_fingerprint)
    cached = _HR_ANALYSIS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    resume = store.get_resume(resume_id)
    if not resume:
        raise ApiError(f"Resume not found: {resume_id}", 404, "resumes")
    job = store.get_job(job_id)
    if not job:
        raise ApiError(f"Job not found: {job_id}", 404, "resumes")

    prompt = PROMPT_HR_RECRUITMENT_ANALYSIS.format(
        Job_Description=_compact_analysis_text(job.get("content", ""), 6000),
        raw_resume=_compact_analysis_text(resume.get("content", ""), 8000),
        current_date=datetime.now().strftime("%Y-%m"),
    )
    started = time.perf_counter()
    try:
        raw = llm.call_llm(
            prompt,
            expect_json=True,
            max_tokens=4000,
            runtime_config=ai_config,
        )
        result = _normalize_hr_analysis(
            raw,
            job.get("content", ""),
            resume.get("content", ""),
        )
        _HR_ANALYSIS_CACHE[cache_key] = result
        logger.info(
            "HR analysis completed: candidates=1 prompt_chars=%s elapsed_ms=%s",
            len(prompt),
            int((time.perf_counter() - started) * 1000),
        )
        return result
    except ValueError as e:
        logger.warning("HR model returned invalid JSON after retry: %s", e)
        raise ApiError("AI 分析结果无法解析，请重试。", 502, "resumes") from e
    except Exception as e:
        logger.error(f"HR recruitment analysis failed: {e}", exc_info=True)
        raise ApiError("AI 分析服务暂时不可用，请检查模型配置后重试。", 503, "resumes") from e


def _run_hr_batch_analysis(
    resume_ids: list[str],
    job_id: str,
    ai_config: dict | None = None,
) -> tuple[dict[str, dict], list[dict]]:
    """Run isolated candidate analyses concurrently to preserve single-resume quality."""
    results: dict[str, dict] = {}
    failures: list[dict] = []
    pending: list[str] = []
    for resume_id in resume_ids:
        cache_key = (
            _HR_ANALYSIS_VERSION,
            resume_id,
            job_id,
            llm.model_config_fingerprint(ai_config),
        )
        cached = _HR_ANALYSIS_CACHE.get(cache_key)
        if cached is not None:
            results[resume_id] = cached
            continue
        resume = store.get_resume(resume_id)
        if not resume:
            failures.append({"resume_id": resume_id, "detail": f"Resume not found: {resume_id}"})
            continue
        pending.append(resume_id)

    if not pending:
        return results, failures

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(3, len(pending)), thread_name_prefix="hr-analysis") as executor:
        futures = {
            executor.submit(_run_hr_analysis, resume_id, job_id, ai_config): resume_id
            for resume_id in pending
        }
        for future in as_completed(futures):
            resume_id = futures[future]
            try:
                results[resume_id] = future.result()
            except ApiError as exc:
                failures.append({"resume_id": resume_id, "detail": exc.message})
            except Exception as exc:
                logger.error("Parallel HR analysis failed for %s: %s", resume_id, exc, exc_info=True)
                failures.append({"resume_id": resume_id, "detail": "AI 分析服务暂时不可用，请重试。"})

    logger.info(
        "Parallel HR analysis completed: candidates=%s succeeded=%s elapsed_ms=%s",
        len(pending),
        sum(1 for resume_id in pending if resume_id in results),
        int((time.perf_counter() - started) * 1000),
    )

    return results, failures


def _candidate_name_from_resume(resume: dict, result: dict) -> str:
    analyzed_name = str(result.get("candidate_name") or "").strip()
    if analyzed_name and analyzed_name not in {"未提供", "未知", "无法识别"}:
        return analyzed_name[:40]

    personal_data = (resume.get("processed") or {}).get("personal_data") or {}
    structured_name = str(personal_data.get("name") or "").strip()
    if not structured_name:
        first_name = str(personal_data.get("firstName") or personal_data.get("first_name") or "").strip()
        last_name = str(personal_data.get("lastName") or personal_data.get("last_name") or "").strip()
        structured_name = f"{last_name}{first_name}" if any("\u4e00" <= char <= "\u9fff" for char in last_name + first_name) else " ".join(filter(None, (first_name, last_name)))
    if structured_name:
        return structured_name[:40]

    for raw_line in str(resume.get("content") or "").splitlines()[:5]:
        line = raw_line.strip().lstrip("#").strip()
        if not line or len(line) > 40:
            continue
        if any(
            token in line
            for token in (
                "@", "：", ":", "·", "|", "，", ",", "。",
                "简历", "联系方式", "工程师", "经理", "候选人",
            )
        ):
            continue
        if sum(char.isdigit() for char in line) > 1:
            continue
        return line
    return "未识别姓名"


def _hr_analysis_payload(resume_id: str, job_id: str, result: dict) -> dict:
    resume = store.get_resume(resume_id)
    return {
        "resume_id": resume_id,
        "job_id": job_id,
        "candidate_name": _candidate_name_from_resume(resume or {}, result),
        "hr_analysis": result,
        "analysis_result": _hr_analysis_markdown(result),
        "studio_markdown": _resume_studio_markdown(resume or {}),
    }


@app.post("/api/v1/resumes/hr-analysis")
def hr_analysis():
    """Run one concise, structured LLM call for recruitment screening."""
    rid = _request_id("resumes")
    data = request.get_json(silent=True) or {}
    resume_ids = data.get("resume_ids")
    if not isinstance(resume_ids, list):
        resume_ids = [data.get("resume_id")] if data.get("resume_id") else []
    resume_ids = list(dict.fromkeys(str(item).strip() for item in resume_ids if str(item).strip()))
    job_id = str(data.get("job_id") or "").strip()
    if not resume_ids or not job_id:
        return _err("resume_id/resume_ids and job_id are required", 422, "resumes")
    if len(resume_ids) > 3:
        return _err("A maximum of 3 resumes can be analyzed at once", 422, "resumes")
    ai_config = _request_ai_config(data)

    if len(resume_ids) == 1:
        result = _run_hr_analysis(resume_ids[0], job_id, ai_config)
        payload = _hr_analysis_payload(resume_ids[0], job_id, result)
    else:
        batch_results, failures = _run_hr_batch_analysis(resume_ids, job_id, ai_config)
        analyses = [
            _hr_analysis_payload(resume_id, job_id, batch_results[resume_id])
            for resume_id in resume_ids
            if resume_id in batch_results
        ]
        if not analyses:
            raise ApiError("本批次所有简历的 AI 分析均失败，请重试。", 502, "resumes")
        payload = {
            **analyses[0],
            "batch_analyses": analyses,
            "batch_failures": failures,
        }

    return jsonify(
        {
            "request_id": rid,
            "data": payload,
        }
    )


@app.get("/api/v1/resumes")
def get_resume():
    """获取简历 + 结构化数据。"""
    rid = _request_id("resumes")
    resume_id = request.args.get("resume_id")
    if not resume_id:
        return _err("resume_id is required", 422, "resumes")

    view = store.get_resume_view(resume_id)
    if not view:
        return _err(f"Resume not found: {resume_id}", 404, "resumes")
    return jsonify({"request_id": rid, "data": view})


@app.post("/api/v1/resumes/improved-markdown")
def improved_markdown():
    """
    从分析结果里提取优化后的简历 markdown（给 a4cv 编辑器用）。
    先正则抽 ```md 块；抽不到则从结构化简历拼兜底 markdown。
    """
    rid = _request_id("resumes")
    data = request.get_json(silent=True) or {}
    analysis_result = data.get("analysis_result") or ""
    resume_id = data.get("resume_id")

    # 1. 尝试从分析文本抽代码块
    md, source = _extract_md_block(analysis_result)
    if md:
        md = _normalize_md_for_a4cv(md)  # 加粗小节标题转 ##，让 a4cv 能识别结构
        return jsonify(
            {
                "request_id": rid,
                "data": {
                    "markdown": md,
                    "source": "extracted",
                    "sections_detected": _count_sections(md),
                },
            }
        )

    # 2. 兜底：从结构化简历拼装
    if resume_id:
        resume = store.get_resume(resume_id)
        if resume:
            md = _resume_studio_markdown(resume)
            if md:
                return jsonify(
                    {
                        "request_id": rid,
                        "data": {
                            "markdown": md,
                            "source": "fallback",
                            "sections_detected": _count_sections(md),
                        },
                    },
                )

    return jsonify(
        {
            "request_id": rid,
            "data": {"markdown": "", "source": "none", "sections_detected": 0},
        }
    )


# ════════════════════════════════════════════════════════════════════
# 岗位接口
# ════════════════════════════════════════════════════════════════════
@app.post("/api/v1/jobs/upload")
def upload_job():
    """上传 JD（可批量）。手动校验 Content-Type 必须是 application/json。"""
    rid = _request_id("jobs")

    # 手动 Content-Type 校验（照搬旧版）
    ctype = request.headers.get("Content-Type", "")
    if "application/json" not in ctype:
        return _err("Content-Type must be application/json", 400, "jobs")

    data = request.get_json(silent=True) or {}
    resume_id = data.get("resume_id")
    job_descriptions = data.get("job_descriptions") or []

    if not resume_id:
        return _err("resume_id is required", 422, "jobs")
    if not job_descriptions:
        return _err("job_descriptions is required", 422, "jobs")

    # 校验 resume 存在
    if not store.get_resume(resume_id):
        return _err(f"resume corresponding to resume_id: {resume_id} not found", 400, "jobs")

    job_ids = []
    for desc in job_descriptions:
        processed = doc_parser.summarize_job_locally(desc)
        jid = store.save_job(resume_id=resume_id, content=desc, processed=processed)
        job_ids.append(jid)
        logger.info(f"Job created: {jid}")

    return jsonify(
        {
            "message": "data successfully processed",
            "request_id": rid,
            "job_id": job_ids,
        }
    )


@app.get("/api/v1/jobs")
def get_job():
    """获取 JD + 结构化数据。"""
    rid = _request_id("jobs")
    job_id = request.args.get("job_id")
    if not job_id:
        return _err("job_id is required", 422, "jobs")

    view = store.get_job_view(job_id)
    if not view:
        return _err(f"Job not found: {job_id}", 404, "jobs")
    return jsonify({"request_id": rid, "data": view})


# ════════════════════════════════════════════════════════════════════
# markdown 提取工具（照搬旧版 markdown_extractor.py 逻辑）
# ════════════════════════════════════════════════════════════════════
_CODE_BLOCK_RE = re.compile(r"```(?:md|markdown)?[ \t]*\n([\s\S]+?)\n```", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,2}\s+\S+", re.MULTILINE)
_SECTION_RE = re.compile(r"^##\s+\S+", re.MULTILINE)
# 识别代码块是否是「真正的简历 markdown」而非其他内容（如代码/JSON）。
# 放宽判断：很多 LLM 生成的简历首行是纯名字（无 # 标题），但有小节标题、
# 加粗、列表或分隔线等 markdown 结构。只要够长且含这些痕迹就认为是简历。
_MD_SIGNATURE_RE = re.compile(
    r"(^#{1,3}\s+\S)"           # 任意层级标题
    r"|(\*\*[^*]+\*\*)"          # 加粗 **xxx**
    r"|(^[-*]\s+\S)"             # 无序列表
    r"|(^---\s*$)",              # 分隔线
    re.MULTILINE,
)


def _extract_md_block(text: str):
    """
    从分析结果抽 ```md 代码块作为优化后的简历。返回 (md, source)。

    启发式评分替代原先的"取最长"：
      - 长度分：capped len / 100
      - 简历关键词分：包含"工作经历"/"教育背景"/"项目经验"/"技能"等核心小节 +5/项
      - 标题/列表/加粗结构 +1/项
    取得分最高的代码块；都不过关返回 None 走 fallback。
    """
    if not text:
        return None, "none"
    matches = _CODE_BLOCK_RE.findall(text)
    if not matches:
        return None, "none"

    # 简历常见中文小节标题；命中一个 +5 分
    resume_section_keywords = (
        "工作经历", "教育背景", "项目经验", "项目经历",
        "技能", "个人信息", "联系方式", "工作业绩",
        "教育经历", "实习经历", "工作项目", "工作项目经验",
    )

    def _score(candidate: str) -> int:
        score = 0
        score += min(len(candidate) // 100, 50)  # 长度分封顶 50
        for kw in resume_section_keywords:
            if kw in candidate:
                score += 5
        # markdown 结构信号
        score += len(_HEADING_RE.findall(candidate))
        score += len(_SECTION_RE.findall(candidate))
        return score

    best = max(matches, key=_score).strip()
    if _score(best) >= 5 and len(best) > 150:
        return best, "extracted"
    return None, "none"


# 用 _ALL_HEADING_RE 统计小节数（覆盖 1-3 级标题，避免漏计 ### 级）
_ALL_HEADING_RE = re.compile(r"^#{1,3}\s+\S+", re.MULTILINE)


def _count_sections(md: str) -> int:
    """统计 markdown 中的标题数量（1-3 级），用于返回 sections_detected。"""
    if not md:
        return 0
    return len(_ALL_HEADING_RE.findall(md))


# a4cv 能识别的简历小节标题关键词（与 a4cv looksLikeSectionTitle 对齐）。
# LLM 常把小节标题写成加粗 **工作经历** 而非 ## 工作经历，这里统一转成 ##，
# 让 a4cv 的 normalizeImportedMarkdown 能正确识别结构。
_A4CV_SECTION_KEYWORDS = (
    "自我评价|个人简介|职业概况|个人优势|工作经历|工作经验|项目经历|项目经验|"
    "实习经历|教育经历|教育背景|技能|技能特长|专业技能|证书|证书与荣誉|荣誉奖项|"
    "关键成果|作品集|发表论文|论文发表|社团经历|培训经历"
)
# 匹配独立的加粗小节标题行：**工作经历** 或 **工作经历**
_BOLD_SECTION_RE = re.compile(
    r"^[ \t]*\*{2}\s*(" + _A4CV_SECTION_KEYWORDS + r")\s*[:：]?\*{2}[ \t]*$",
    re.MULTILINE,
)


def _normalize_md_for_a4cv(md: str) -> str:
    """
    把 LLM 生成的简历 markdown 标准化，让 a4cv 编辑器能正确识别结构：
    1. 加粗小节标题转 ## 标题：**工作经历** → ## 工作经历
    2. 首行若是纯名字（无 #），补成 # 姓名（a4cv 靠 # 识别姓名栏）

    a4cv 的 normalizeImportedMarkdown：只要 markdown 含任何 # 标题就走
    normalizeHeadingMarkdown 直接返回，不会自动补 # 姓名。而 LLM 常把姓名
    写成纯文本首行，所以必须在这里补上。
    """
    if not md:
        return md

    def _repl(m):
        return f"## {m.group(1)}"

    md = _BOLD_SECTION_RE.sub(_repl, md)

    # 首行补 # 姓名：若首行不是标题、不是空行、也不是联系方式行，视为姓名
    lines = md.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue  # 跳过前导空行
        if stripped.startswith("#"):
            break  # 首个非空行已是标题，无需补
        # 简易判断：联系方式行（含 @ 或多个数字）不当作姓名
        if "@" in stripped or re.search(r"\d{4,}", stripped):
            break
        lines[i] = f"# {stripped}"
        break

    return "\n".join(lines)


def _build_fallback_markdown(processed: dict) -> str:
    """从结构化简历拼 a4cv 兼容的最小 markdown。"""
    pd = processed.get("personal_data") or {}
    name = pd.get("firstName") or pd.get("name") or "你的姓名"
    title = pd.get("title") or pd.get("position") or ""

    contact_bits = []
    for key in ("email", "phone"):
        v = pd.get(key)
        if v:
            contact_bits.append(str(v))
    loc = pd.get("location")
    if isinstance(loc, dict):
        city = loc.get("city")
        if city:
            contact_bits.append(str(city))
    for key in ("linkedin", "portfolio"):
        v = pd.get(key)
        if v:
            contact_bits.append(str(v))

    out = [f"# {name}"]
    if title:
        out.append(f"## {title}")
    if contact_bits:
        out += ["", "> " + " · ".join(contact_bits)]

    experiences = processed.get("experiences") or []
    if experiences:
        out += ["", "## 工作经历"]
        for e in experiences:
            t = e.get("job_title") or e.get("jobTitle") or "职位"
            c = e.get("company") or ""
            sd = e.get("start_date") or e.get("startDate") or ""
            ed = e.get("end_date") or e.get("endDate") or ""
            meta = " · ".join(x for x in (c, f"{sd} - {ed}" if sd else "") if x)
            out.append(f"### {t}" + (f" | {meta}" if meta else ""))
            for b in e.get("description") or []:
                if b:
                    out.append(f"- {b}")

    projects = processed.get("projects") or []
    if projects:
        out += ["", "## 项目经历"]
        for p in projects:
            t = p.get("project_name") or p.get("projectName") or "项目"
            desc = p.get("description") or ""
            out.append(f"### {t}")
            if desc:
                out.append(f"- {desc}")

    education = processed.get("education") or []
    if education:
        out += ["", "## 教育背景"]
        for ed in education:
            school = ed.get("institution") or "学校"
            degree = ed.get("degree") or ""
            out.append(f"### {school}" + (f" | {degree}" if degree else ""))

    skills = processed.get("skills") or []
    if skills:
        out += ["", "## 技能标签"]
        names = []
        for s in skills:
            if isinstance(s, dict):
                names.append(s.get("skill_name") or s.get("skillName") or "")
            else:
                names.append(str(s))
        out.append(" · ".join(filter(None, names)))

    achievements = processed.get("achievements") or []
    if achievements:
        out += ["", "## 证书与荣誉"]
        for a in achievements:
            out.append(f"- {a}")

    return "\n".join(out).rstrip() + "\n"


if __name__ == "__main__":
    # 仅本地直接 python app.py 时用；生产请用 gunicorn app:app
    app.run(host="127.0.0.1", port=config.BACKEND_PORT, debug=(config.ENV != "production"))
