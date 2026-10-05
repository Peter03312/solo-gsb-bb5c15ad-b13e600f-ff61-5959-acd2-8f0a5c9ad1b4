"""人工裁决接口测试：选择/撤销、版本并发、拒绝条件、裁决版 TSV。

裁决是按 BED 物理行号作用于“某一次转换结果”的：
- 只允许歧义行、候选序号必须在范围内；
- 成功修改版本 +1，相同版本并发修改仅一个成功（409）；
- 原始 TSV 与原始 status 永不变；
- 裁决版 TSV：已选行只出被选候选片段且不改称 mapped，
  未选歧义行全部候选保留并标明“未裁决”。
"""

import os
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.report import ADJ_MARKER_PENDING, ADJ_MARKER_SELECTED

client = TestClient(app)

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def _post(bed_name: str = "example_target.bed", side: str = "target",
          chain_name: str = "demo.chain"):
    with open(os.path.join(EXAMPLES, bed_name), "rb") as bf, \
         open(os.path.join(EXAMPLES, chain_name), "rb") as cf:
        return client.post(
            "/api/liftover",
            files={"bed": (bed_name, bf, "text/plain"),
                   "chain": (chain_name, cf, "text/plain")},
            data={"side": side},
        )


@pytest.fixture()
def job():
    resp = _post()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    rows = data["preview"]["rows"]
    ambiguous = [r for r in rows if r["status"] == "ambiguous"]
    assert len(ambiguous) == 1
    return {
        "token": data["download_token"],
        "name": data["download_name"],
        "line": ambiguous[0]["line_no"],
        "n_cands": len(ambiguous[0]["candidates"]),
        "rows": rows,
        "initial_version": data["adjudication_version"],
    }


def _tsv_lines(resp) -> list[str]:
    return resp.content.decode("utf-8-sig").splitlines()


def _ambiguous_data_lines(token: str) -> list[str]:
    body = client.get(f"/api/download/{token}")
    return [ln for ln in _tsv_lines(body)[1:] if "\tambiguous\t" in ln]


# ---------- 初始状态 ----------

def test_initial_adjudication_state(job):
    assert job["initial_version"] == 1
    resp = client.get(f"/api/adjudication/{job['token']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == 1
    assert data["resolved_count"] == 0
    assert data["ambiguous_count"] == 1
    assert data["decisions"] == []
    info = data["ambiguous_rows"][0]
    assert info["source_line"] == job["line"]
    assert info["num_candidates"] == job["n_cands"]
    assert info["selected_candidate"] is None
    assert info["resolved"] is False


# ---------- 选择候选 ----------

def test_select_candidate_increments_version(job):
    token, line = job["token"], job["line"]
    resp = client.post(f"/api/adjudication/{token}",
                       json={"source_line": line, "candidate_index": 2, "version": 1})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["changed"] is True
    assert data["version"] == 2
    assert data["selected_candidate"] == 2
    assert data["resolved_count"] == 1
    assert data["decisions"] == [{"source_line": line, "selected_candidate": 2}]

    state = client.get(f"/api/adjudication/{token}").json()
    assert state["version"] == 2
    assert state["ambiguous_rows"][0]["selected_candidate"] == 2
    assert state["ambiguous_rows"][0]["resolved"] is True


def test_repost_same_choice_is_noop_version_unchanged(job):
    token, line = job["token"], job["line"]
    r1 = client.post(f"/api/adjudication/{token}",
                     json={"source_line": line, "candidate_index": 1, "version": 1})
    assert r1.json()["changed"] is True
    assert r1.json()["version"] == 2
    r2 = client.post(f"/api/adjudication/{token}",
                     json={"source_line": line, "candidate_index": 1, "version": 2})
    assert r2.status_code == 200
    assert r2.json()["changed"] is False
    assert r2.json()["version"] == 2


def test_change_choice_then_undo(job):
    token, line = job["token"], job["line"]
    client.post(f"/api/adjudication/{token}",
                json={"source_line": line, "candidate_index": 1, "version": 1})
    # 改选另一个候选
    r = client.post(f"/api/adjudication/{token}",
                    json={"source_line": line, "candidate_index": 2, "version": 2})
    assert r.json()["version"] == 3
    assert r.json()["selected_candidate"] == 2

    # 撤销（candidate_index=null 与省略字段两种写法等价）
    r = client.post(f"/api/adjudication/{token}",
                    json={"source_line": line, "candidate_index": None, "version": 3})
    assert r.status_code == 200
    assert r.json()["changed"] is True
    assert r.json()["version"] == 4
    assert r.json()["selected_candidate"] is None
    assert r.json()["decisions"] == []

    # 未裁决状态再撤销：无变化，版本不动
    r = client.post(f"/api/adjudication/{token}",
                    json={"source_line": line, "version": 4})
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["version"] == 4


# ---------- 裁决版 TSV ----------

def test_adjudicated_tsv_selected_row_keeps_status_and_pending_marker(job):
    token, line = job["token"], job["line"]

    # 未裁决时：全部候选保留，歧义行标“未裁决”；其他状态行与原始一致
    resp = client.get(f"/api/download/adjudicated/{token}")
    assert resp.status_code == 200
    lines = _tsv_lines(resp)
    header = lines[0].split("\t")
    assert header[0] == "source_line"
    assert len(header) == 15
    amb = [ln for ln in lines[1:] if "\tambiguous\t" in ln]
    assert len(amb) == job["n_cands"]
    assert all(ADJ_MARKER_PENDING in ln for ln in amb)
    # 未裁决版不应出现“已裁决”标记
    assert not any("已裁决" in ln for ln in lines)

    # 选定候选 2
    client.post(f"/api/adjudication/{token}",
                json={"source_line": line, "candidate_index": 2, "version": 1})
    resp = client.get(f"/api/download/adjudicated/{token}")
    lines = _tsv_lines(resp)
    amb = [ln for ln in lines[1:] if "\tambiguous\t" in ln]
    # 只剩候选 2 的片段行；candidate_index 列（第 6 列）仍是原始序号 2
    assert len(amb) >= 1
    for ln in amb:
        cols = ln.split("\t")
        assert int(cols[0]) == line
        assert cols[1] == "ambiguous"          # 绝不改称 mapped
        assert cols[5] == "2"
        assert ADJ_MARKER_SELECTED.format(ci=2) in cols[14]
    # 候选 1 的目标染色体 chrA 在该行消失（chrB 为候选 2）
    assert all(cols[7] != "chrA" for cols in (ln.split("\t") for ln in amb))

    # 文件名带裁决版本
    cd = resp.headers["content-disposition"]
    assert "_adjudicated_v2" in cd


def test_original_tsv_unchanged_after_adjudication(job):
    token, line = job["token"], job["line"]
    before = client.get(f"/api/download/{token}").content
    client.post(f"/api/adjudication/{token}",
                json={"source_line": line, "candidate_index": 2, "version": 1})
    client.post(f"/api/adjudication/{token}",
                json={"source_line": line, "candidate_index": None, "version": 2})
    after = client.get(f"/api/download/{token}").content
    assert before == after
    # 原始 TSV 不含任何裁决标记
    assert "未裁决".encode() not in after
    assert "已裁决".encode() not in after


def test_non_ambiguous_and_unmapped_rows_identical_in_adjudicated(job):
    token = job["token"]
    raw = _tsv_lines(client.get(f"/api/download/{token}"))
    adj = _tsv_lines(client.get(f"/api/download/adjudicated/{token}"))
    assert raw[0] == adj[0]
    # 除歧义行外，mapped/partial/unmapped 行逐字节一致
    def other(lines):
        return [ln for ln in lines[1:] if "\tambiguous\t" not in ln]
    assert other(raw) == other(adj)


# ---------- 拒绝条件 ----------

def test_select_on_non_ambiguous_row_rejected(job):
    mapped = next(r for r in job["rows"] if r["status"] == "mapped")
    resp = client.post(f"/api/adjudication/{job['token']}",
                       json={"source_line": mapped["line_no"],
                             "candidate_index": 1, "version": 1})
    assert resp.status_code == 400
    assert "歧义" in resp.json()["errors"][0]
    # 版本未动
    assert client.get(f"/api/adjudication/{job['token']}").json()["version"] == 1


def test_nonexistent_source_line_rejected(job):
    resp = client.post(f"/api/adjudication/{job['token']}",
                       json={"source_line": 99999, "candidate_index": 1, "version": 1})
    assert resp.status_code == 400
    assert "不存在" in resp.json()["errors"][0]


def test_candidate_index_out_of_range_rejected(job):
    resp = client.post(f"/api/adjudication/{job['token']}",
                       json={"source_line": job["line"],
                             "candidate_index": job["n_cands"] + 1, "version": 1})
    assert resp.status_code == 400
    assert "越界" in resp.json()["errors"][0]


def test_invalid_payload_shapes_rejected(job):
    base = f"/api/adjudication/{job['token']}"
    bad_bodies = [
        {"source_line": job["line"], "candidate_index": 1},          # 缺 version
        {"source_line": job["line"], "candidate_index": 1, "version": "1"},
        {"source_line": 0, "candidate_index": 1, "version": 1},      # 行号非正
        {"source_line": "x", "candidate_index": 1, "version": 1},
        {"source_line": job["line"], "candidate_index": 0, "version": 1},
        {"source_line": job["line"], "candidate_index": -1, "version": 1},
        {"source_line": job["line"], "candidate_index": "1", "version": 1},
        {"source_line": job["line"], "candidate_index": True, "version": 1},
        {"source_line": job["line"], "candidate_index": 1, "version": True},
    ]
    for body in bad_bodies:
        resp = client.post(base, json=body)
        assert resp.status_code == 400, body
    # 非 JSON body
    resp = client.post(base, content="not-json",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_stale_version_rejected_with_409(job):
    token, line = job["token"], job["line"]
    # 版本 1 成功
    assert client.post(f"/api/adjudication/{token}",
                       json={"source_line": line, "candidate_index": 1,
                             "version": 1}).status_code == 200
    # 再拿旧版本 1 提交：即使内容不同也必须 409
    resp = client.post(f"/api/adjudication/{token}",
                       json={"source_line": line, "candidate_index": 2, "version": 1})
    assert resp.status_code == 409
    payload = resp.json()
    assert payload["current_version"] == 2
    assert "过期" in payload["errors"][0]
    # 选择没有被旧版本请求改动
    state = client.get(f"/api/adjudication/{token}").json()
    assert state["version"] == 2
    assert state["decisions"][0]["selected_candidate"] == 1

    # 撤销同样受版本保护
    resp = client.post(f"/api/adjudication/{token}",
                       json={"source_line": line, "candidate_index": None, "version": 1})
    assert resp.status_code == 409


def test_concurrent_same_version_only_one_wins(job):
    token, line = job["token"], job["line"]
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker(ci: int):
        barrier.wait()
        r = client.post(f"/api/adjudication/{token}",
                        json={"source_line": line, "candidate_index": ci, "version": 1})
        results.append(r.status_code)

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(2,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 409]
    state = client.get(f"/api/adjudication/{token}").json()
    assert state["version"] == 2
    assert state["resolved_count"] == 1


def test_expired_token_endpoints_404():
    resp = _post()
    token = resp.json()["download_token"]
    assert client.get(f"/api/adjudication/{token}").status_code == 200
    # 直接删除内存任务模拟过期
    from app.main import _jobs
    _jobs.pop(token, None)
    assert client.get(f"/api/adjudication/{token}").status_code == 404
    assert client.post(f"/api/adjudication/{token}",
                       json={"source_line": 1, "candidate_index": 1,
                             "version": 1}).status_code == 404
    assert client.get(f"/api/download/adjudicated/{token}").status_code == 404


def test_query_side_result_adjudication_uses_physical_line():
    # query 侧示例没有歧义行：状态查询仍应正常返回空决策
    resp = _post("example_query.bed", side="query")
    token = resp.json()["download_token"]
    state = client.get(f"/api/adjudication/{token}").json()
    assert state["ambiguous_count"] == 0
    assert state["version"] == 1
