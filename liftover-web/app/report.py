"""结果导出：TSV 文本与预览 JSON。

TSV 列（英文表头，避免下载文件里混入列名歧义）::

    source_line, status, source_chrom, source_start, source_end,
    candidate_index, chain_id, dest_chrom, dest_strand,
    fragment_index, src_start, src_end, dest_start, dest_end, reason

每个源行号可能对应多行（多个片段 / 多个歧义候选）。
unmapped 行没有候选，相关列写 '.'。

裁决版 TSV（``build_adjudicated_tsv``）在末尾追加一列 ``decision``：

- 已选候选的歧义行：仅输出被选候选的全部片段，decision=已裁决；
- 未选歧义行：仍输出全部候选的全部片段，decision=未裁决；
- 其他状态行：照原结果逐行输出，decision='.'。

无论是否裁决，``status`` 列始终保留引擎给出的原始状态——
人工裁决不会把 partial 改称 mapped，也不会把 ambiguous 改成别的。
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict
from typing import Any, Mapping

from .engine import Candidate, Fragment, RowResult

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

# 裁决版在原列尾部追加一列（不改动前面任何列与原始 status）
DECISION_HEADER = "decision"
ADJ_TSV_HEADER = TSV_HEADER + [DECISION_HEADER]

DECISION_PICKED = "已裁决"
DECISION_PENDING = "未裁决"
DECISION_NA = "."


def _fragment_values(
    row: RowResult,
    cand: Candidate,
    ci: int,
    frag: Fragment,
    fi: int,
) -> list[Any]:
    return [
        row.line_no, row.status, row.src_chrom, row.src_start, row.src_end,
        ci, cand.chain_id, cand.dest_chrom, cand.dest_strand,
        fi, frag.src_start, frag.src_end, frag.dest_start, frag.dest_end,
        row.reason,
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
                writer.writerow(_fragment_values(row, cand, ci, frag, fi))
    return buf.getvalue()


def build_adjudicated_tsv(
    rows: list[RowResult],
    decisions: Mapping[int, int],
) -> str:
    """生成裁决版 TSV。

    ``decisions`` 为 {源物理行号: 1 基候选序号}，只可能落在歧义行上
    （接口层保证；此处对非法键做容错，按未裁决处理）。

    - 已选行：只写被选候选的全部片段，decision 列写 ``已裁决``；
      candidate_index 沿用其在原始候选列表中的 1 基序号，便于与原结果对照。
    - 未选歧义行：写全部候选、全部片段，decision 列写 ``未裁决``。
    - mapped / partial / unmapped 行：前 15 列与 ``build_tsv`` 逐行一致，
      decision 列写 '.'。

    status 列在任何情况下都保持引擎给出的原始值。
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter="\t", lineterminator="\n",
                        quoting=csv.QUOTE_MINIMAL)
    writer.writerow(ADJ_TSV_HEADER)
    for row in rows:
        if not row.candidates:
            writer.writerow([
                row.line_no, row.status, row.src_chrom, row.src_start, row.src_end,
                ".", ".", ".", ".", ".", ".", ".", ".", ".", row.reason,
                DECISION_NA,
            ])
            continue

        picked = decisions.get(row.line_no)
        is_picked_ambiguous = (
            row.status == "ambiguous"
            and picked is not None
            and 1 <= picked <= len(row.candidates)
        )
        if is_picked_ambiguous:
            cand = row.candidates[picked - 1]
            for fi, frag in enumerate(cand.fragments, start=1):
                values = _fragment_values(row, cand, picked, frag, fi)
                writer.writerow(values + [DECISION_PICKED])
        else:
            decision = DECISION_PENDING if row.status == "ambiguous" else DECISION_NA
            for ci, cand in enumerate(row.candidates, start=1):
                for fi, frag in enumerate(cand.fragments, start=1):
                    values = _fragment_values(row, cand, ci, frag, fi)
                    writer.writerow(values + [decision])
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
