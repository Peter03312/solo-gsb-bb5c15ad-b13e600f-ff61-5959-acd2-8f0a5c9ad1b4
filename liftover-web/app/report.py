"""结果导出：TSV 文本与预览 JSON。

TSV 列（英文表头，避免下载文件里混入列名歧义）::

    source_line, status, source_chrom, source_start, source_end,
    candidate_index, chain_id, dest_chrom, dest_strand,
    fragment_index, src_start, src_end, dest_start, dest_end, reason

每个源行号可能对应多行（多个片段 / 多个歧义候选）。
unmapped 行没有候选，相关列写 '.'。
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict
from typing import Any

from .engine import RowResult

STATUS_LABELS = {
    "mapped": "mapped",
    "partial": "partial",
    "ambiguous": "ambiguous",
    "unmapped": "unmapped",
}

TSV_HEADER = [
    "source_line",
    "status",
    "source_chrom",
    "source_start",
    "source_end",
    "candidate_index",
    "chain_id",
    "dest_chrom",
    "dest_strand",
    "fragment_index",
    "src_start",
    "src_end",
    "dest_start",
    "dest_end",
    "reason",
]


def build_tsv(rows: list[RowResult]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter="\t", lineterminator="\n",
                        quoting=csv.QUOTE_MINIMAL)
    writer.writerow(TSV_HEADER)
    for row in rows:
        if not row.candidates:
            writer.writerow([
                row.line_no, row.status, row.src_chrom, row.src_start, row.src_end,
                ".", ".", ".", ".", ".", ".", ".", ".", ".", row.reason,
            ])
            continue
        for ci, cand in enumerate(row.candidates, start=1):
            for fi, frag in enumerate(cand.fragments, start=1):
                writer.writerow([
                    row.line_no, row.status, row.src_chrom, row.src_start, row.src_end,
                    ci, cand.chain_id, cand.dest_chrom, cand.dest_strand,
                    fi, frag.src_start, frag.src_end, frag.dest_start, frag.dest_end,
                    row.reason,
                ])
    return buf.getvalue()


ADJ_MARKER_SELECTED = "【已裁决：选定候选 {ci}】"
ADJ_MARKER_PENDING = "【未裁决】"


def _adj_reason(base: str, marker: str) -> str:
    return f"{base} {marker}" if base else marker


def build_adjudicated_tsv(
    rows: list[RowResult], decisions: dict[int, int]
) -> str:
    """构建人工裁决版 TSV（表头、列序、坐标约定与原始 TSV 完全一致）。

    - 已裁决的歧义行：只输出被选候选的全部片段，status 仍为
      ``ambiguous``（不把歧义改称为 mapped），reason 追加“已裁决”标记；
    - 未裁决的歧义行：仍输出全部候选并在 reason 标明“未裁决”；
    - 其他状态行与原始结果逐字节一致（除歧义行标记外，不改动任何字段）。
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter="\t", lineterminator="\n",
                        quoting=csv.QUOTE_MINIMAL)
    writer.writerow(TSV_HEADER)
    for row in rows:
        if not row.candidates:
            writer.writerow([
                row.line_no, row.status, row.src_chrom, row.src_start, row.src_end,
                ".", ".", ".", ".", ".", ".", ".", ".", ".", row.reason,
            ])
            continue

        chosen = decisions.get(row.line_no) if row.status == "ambiguous" else None
        if row.status == "ambiguous":
            if chosen is not None:
                cand = row.candidates[chosen - 1]
                reason = _adj_reason(row.reason,
                                     ADJ_MARKER_SELECTED.format(ci=chosen))
                for fi, frag in enumerate(cand.fragments, start=1):
                    writer.writerow([
                        row.line_no, row.status, row.src_chrom,
                        row.src_start, row.src_end,
                        chosen, cand.chain_id, cand.dest_chrom, cand.dest_strand,
                        fi, frag.src_start, frag.src_end,
                        frag.dest_start, frag.dest_end, reason,
                    ])
                continue
            reason = _adj_reason(row.reason, ADJ_MARKER_PENDING)
        else:
            reason = row.reason

        for ci, cand in enumerate(row.candidates, start=1):
            for fi, frag in enumerate(cand.fragments, start=1):
                writer.writerow([
                    row.line_no, row.status, row.src_chrom, row.src_start, row.src_end,
                    ci, cand.chain_id, cand.dest_chrom, cand.dest_strand,
                    fi, frag.src_start, frag.src_end, frag.dest_start, frag.dest_end,
                    reason,
                ])
    return buf.getvalue()


def row_to_dict(row: RowResult) -> dict[str, Any]:
    return asdict(row)


def rows_to_preview(rows: list[RowResult]) -> dict[str, Any]:
    counts = {"mapped": 0, "partial": 0, "ambiguous": 0, "unmapped": 0}
    for r in rows:
        counts[r.status] += 1
    return {
        "total": len(rows),
        "counts": counts,
        "rows": [row_to_dict(r) for r in rows],
    }


def adjudication_payload(
    rows: list[RowResult],
    decisions: dict[int, int],
    version: int,
) -> dict[str, Any]:
    """裁决状态：版本号、每个歧义行的裁决情况，以及完整预览行。

    ``decisions`` 的键是源文件物理行号，值是被选候选的 1 基序号。
    """
    ambiguous = [
        {
            "source_line": row.line_no,
            "num_candidates": len(row.candidates),
            "selected_candidate": decisions.get(row.line_no),
            "resolved": row.line_no in decisions,
        }
        for row in rows
        if row.status == "ambiguous"
    ]
    return {
        "version": version,
        "resolved_count": len(decisions),
        "ambiguous_count": len(ambiguous),
        "decisions": [
            {"source_line": line_no, "selected_candidate": ci}
            for line_no, ci in sorted(decisions.items())
        ],
        "ambiguous_rows": ambiguous,
        "preview": rows_to_preview(rows),
    }
