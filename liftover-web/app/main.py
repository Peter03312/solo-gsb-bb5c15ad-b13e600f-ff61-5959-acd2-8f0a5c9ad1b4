"""FastAPI 入口：上传、预览、下载、歧义人工裁决。

结果只在校验完全通过后保存在进程内存中（BytesIO 等价物），
任何解析错误都直接 400 返回，不创建、更不提供任何可下载文件。

裁决只作用于已保存的转换结果：原始转换行与原始 TSV 永不改变；
裁决选择单独保存在任务上，裁决版 TSV 按需重新导出。裁决通过
“版本号 + 每行物理行号”做乐观并发控制：相同版本的并发修改只有
一个能成功，其余得到 409。
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass, field

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .bed import parse_bed
from .chain import parse_chain
from .engine import LiftOverEngine, RowResult
from .report import (
    adjudication_payload,
    build_adjudicated_tsv,
    build_tsv,
    rows_to_preview,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "..", "static")

MAX_UPLOAD_BYTES = int(os.getenv("LIFTOVER_MAX_UPLOAD_MB", "50")) * 1024 * 1024
JOB_TTL_SECONDS = int(os.getenv("LIFTOVER_JOB_TTL_SECONDS", "3600"))
MAX_JOBS = int(os.getenv("LIFTOVER_MAX_JOBS", "100"))

# 裁决版初始版本号：转换完成、尚无任何裁决时为 1；每次成功修改后 +1
INITIAL_ADJ_VERSION = 1

app = FastAPI(title="离线基因组区间迁移 (BED liftover)", version="1.1.0")


@dataclass
class _Job:
    tsv_bytes: bytes
    filename: str
    created_at: float
    rows: list[RowResult] = field(default_factory=list)
    # 源文件物理行号 -> 被选候选的 1 基序号（只可能含歧义行）
    decisions: dict[int, int] = field(default_factory=dict)
    adj_version: int = INITIAL_ADJ_VERSION
    lock: threading.Lock = field(default_factory=threading.Lock)


_jobs: dict[str, _Job] = {}


def _prune_jobs() -> None:
    now = time.time()
    expired = [k for k, j in _jobs.items() if now - j.created_at > JOB_TTL_SECONDS]
    for k in expired:
        _jobs.pop(k, None)
    # 兜底：超过容量时淘汰最早的任务
    if len(_jobs) > MAX_JOBS:
        for k in sorted(_jobs, key=lambda x: _jobs[x].created_at)[: len(_jobs) - MAX_JOBS]:
            _jobs.pop(k, None)


def _read_upload(f: UploadFile) -> str:
    data = f.file.read()
    if not data:
        raise HTTPException(status_code=400, detail={"errors": [f"文件 {f.filename} 为空"]})
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"errors": [f"文件 {f.filename} 超过大小上限 "
                               f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB"]},
        )
    try:
        # BOM 仅允许出现在文件最前面
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=400,
                detail={"errors": [f"文件 {f.filename} 不是有效的 UTF-8 文本"]},
            )


@app.exception_handler(HTTPException)
async def _http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.detail
                        if isinstance(exc.detail, dict) else {"errors": [str(exc.detail)]})


def _get_job(token: str) -> _Job:
    """按 token 取任务；过期或不存在统一 404。"""
    _prune_jobs()
    job = _jobs.get(token)
    if job is None:
        raise HTTPException(status_code=404,
                            detail={"errors": ["结果不存在或已过期，请重新提交转换"]})
    return job


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/liftover")
async def liftover(
    bed: UploadFile = File(...),
    chain: UploadFile = File(...),
    side: str = Form(...),
):
    if side not in ("target", "query"):
        raise HTTPException(status_code=400,
                            detail={"errors": ["side 必须是 target 或 query"]})

    bed_text = _read_upload(bed)
    chain_text = _read_upload(chain)

    bed_result = parse_bed(bed_text)
    chain_result = parse_chain(chain_text)

    errors = list(bed_result.errors) + list(chain_result.errors)
    if errors:
        # 校验失败：只返回错误，绝不写入 _jobs —— 不存在可下载文件
        raise HTTPException(status_code=400, detail={"errors": errors})

    engine = LiftOverEngine(chain_result.chains, side)
    rows = engine.convert_all(bed_result.records)
    preview = rows_to_preview(rows)

    tsv = build_tsv(rows)
    # Excel 友好的 UTF-8 BOM；HTTP 下载本质上仍是内存字节，不落盘
    payload = b"\xef\xbb\xbf" + tsv.encode("utf-8")
    token = secrets.token_urlsafe(24)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    filename = f"liftover_{side}_to_{'query' if side == 'target' else 'target'}_{stamp}.tsv"
    _prune_jobs()
    _jobs[token] = _Job(
        tsv_bytes=payload,
        filename=filename,
        created_at=time.time(),
        rows=rows,
    )

    return {
        "download_token": token,
        "download_name": filename,
        "preview": preview,
        # 裁决接口所需的初始版本（新结果尚未裁决，版本为 1）
        "adjudication_version": INITIAL_ADJ_VERSION,
    }


@app.get("/api/download/{token}")
async def download(token: str) -> Response:
    job = _get_job(token)
    # 原始 TSV 始终是转换当时的字节，任何裁决都不改变它
    return Response(
        content=job.tsv_bytes,
        media_type="text/tab-separated-values; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{job.filename}"'},
    )


@app.get("/api/adjudication/{token}")
async def get_adjudication(token: str) -> dict:
    """查看某结果当前的人工裁决状态（版本号、每行选择）。"""
    job = _get_job(token)
    with job.lock:
        return adjudication_payload(job.rows, job.decisions, job.adj_version)


def _bad_request(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"errors": [msg]})


@app.post("/api/adjudication/{token}")
async def post_adjudication(token: str, request: Request) -> dict:
    """提交一次人工裁决（按物理行号选择候选或撤销选择）。

    请求 JSON：``{"source_line": 4, "candidate_index": 2, "version": 1}``。
    ``candidate_index`` 为 ``null``（或省略）表示撤销该行已有选择。
    裁决只修改该结果上的选择，不改动原始转换；成功修改后版本 +1，
    相同版本的并发修改只有一个成功，其余返回 409。
    """
    job = _get_job(token)

    try:
        body = await request.json()
    except Exception:
        raise _bad_request("请求体必须是 JSON 对象")
    if not isinstance(body, dict):
        raise _bad_request("请求体必须是 JSON 对象")

    if "version" not in body or not isinstance(body["version"], int) \
            or isinstance(body["version"], bool):
        raise _bad_request("version 必须是整数（结果当前裁决版本号）")
    version = body["version"]

    line_no = body.get("source_line")
    if not isinstance(line_no, int) or isinstance(line_no, bool) or line_no < 1:
        raise _bad_request("source_line 必须是 BED 物理行号（从 1 开始的正整数）")

    raw_ci = body.get("candidate_index", None)
    undo = raw_ci is None
    if not undo:
        if not isinstance(raw_ci, int) or isinstance(raw_ci, bool) or raw_ci < 1:
            raise _bad_request("candidate_index 必须是 >= 1 的候选序号，或为 null 表示撤销")

    with job.lock:
        # 版本先校验：旧版本提交（哪怕内容碰巧相同）一律拒绝
        if version != job.adj_version:
            raise HTTPException(
                status_code=409,
                detail={"errors": [
                    f"裁决版本已过期：提交基于版本 {version}，当前版本为 "
                    f"{job.adj_version}；请重新查看当前选择后再提交"
                ], "current_version": job.adj_version},
            )

        row = next((r for r in job.rows if r.line_no == line_no), None)
        if row is None:
            raise _bad_request(
                f"源文件第 {line_no} 行不存在（本次转换共 {len(job.rows)} 条数据行）")
        if row.status != "ambiguous":
            raise _bad_request(
                f"源文件第 {line_no} 行状态为 {row.status}，只有歧义（ambiguous）"
                f"行才允许人工裁决；原始转换结果不会被改动")
        if not undo and raw_ci > len(row.candidates):
            raise _bad_request(
                f"候选序号越界：第 {line_no} 行共有 {len(row.candidates)} 个候选，"
                f"收到 candidate_index={raw_ci}")

        current = job.decisions.get(line_no)
        changed = False
        if undo:
            if current is not None:
                del job.decisions[line_no]
                changed = True
        else:
            if current != raw_ci:
                job.decisions[line_no] = raw_ci
                changed = True

        if changed:
            job.adj_version += 1

        return {
            "changed": changed,
            "version": job.adj_version,
            "source_line": line_no,
            "selected_candidate": None if undo else raw_ci,
            **adjudication_payload(job.rows, job.decisions, job.adj_version),
        }


@app.get("/api/download/adjudicated/{token}")
async def download_adjudicated(token: str) -> Response:
    """下载裁决版 TSV。

    已选歧义行只输出被选候选的全部片段（status 仍为 ambiguous，不改称
    完全映射）；未选歧义行仍输出全部候选并标明“未裁决”；其他行与原始
    TSV 一致。
    """
    job = _get_job(token)
    with job.lock:
        version = job.adj_version
        decisions = dict(job.decisions)
        tsv = build_adjudicated_tsv(job.rows, decisions)
        base = job.filename
    root, ext = os.path.splitext(base)
    adj_name = f"{root}_adjudicated_v{version}{ext or '.tsv'}"
    payload = b"\xef\xbb\xbf" + tsv.encode("utf-8")
    return Response(
        content=payload,
        media_type="text/tab-separated-values; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{adj_name}"'},
    )


# 静态资源（纯本地，无任何外部 CDN 依赖）
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
