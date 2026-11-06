#!/usr/bin/env python
"""RAG 全量测试矩阵（阶段三）：对运行中的 8000 服务跑黑盒用例，输出修复前后对照用的结果表。

用法（服务已启动）:
    .venv\\Scripts\\python.exe scripts\\rag_matrix_test.py

只做被测账号（Ethen）的临时文档创建与清理，结束时恢复原知识库块数。
"""

import concurrent.futures
import json
import os
import sys
import time
import uuid

import pymupdf
import requests

BASE = "http://127.0.0.1:8000"
RESULTS = []          # (case, expected, actual, ok)
CREATED = set()       # 本次上传的临时文档名（结束时清理）

EMPTY_MSG_HINT = "未提取到文本"


def record(case: str, expected: str, actual: str, ok: bool) -> None:
    RESULTS.append((case, expected, actual, ok))
    print(("[PASS] " if ok else "[FAIL] ") + case + " | " + actual)


def new_session(token: str = "") -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 绕过系统代理（127.0.0.1 不应走 10090）
    if token:
        s.headers["Authorization"] = "Bearer " + token
    return s


def login(account: str) -> str:
    s = new_session()
    r = s.post(BASE + "/login", json={"account": account, "password": ""}, timeout=30)
    r.raise_for_status()
    return r.json()["token"]


def documents(s: requests.Session) -> list:
    r = s.get(BASE + "/rag/documents", timeout=60)
    r.raise_for_status()
    return r.json()["documents"]


def upload(s: requests.Session, filename: str, data: bytes, replace: bool = False,
           timeout: int = 300):
    files = {"file": (filename.encode("utf-8"), data, "application/octet-stream")}
    form = {"replace": "true" if replace else "false"}
    return s.post(BASE + "/rag/ingest", files=files, data=form, timeout=timeout)


def upload_async(s: requests.Session, filename: str, data: bytes, timeout: int = 120):
    files = {"file": (filename.encode("utf-8"), data, "application/octet-stream")}
    return s.post(BASE + "/rag/ingest/async", files=files, timeout=timeout)


def upload_batch(s: requests.Session, items: list, timeout: int = 120):
    files = [("files", (name.encode("utf-8"), data, "application/octet-stream"))
             for name, data in items]
    return s.post(BASE + "/rag/ingest/batch", files=files, timeout=timeout)


def wait_task(s: requests.Session, task_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = s.get(BASE + f"/rag/tasks/{task_id}", timeout=30)
        if r.status_code == 404:
            return {"status_code": 404}
        r.raise_for_status()
        info = r.json()
        if info.get("status") == "completed":
            return info
        time.sleep(0.5)
    raise TimeoutError(f"任务 {task_id} 超时未完成")


def chunks(s: requests.Session, source: str, offset: int = 0, limit: int = 20):
    return s.get(BASE + "/rag/documents/chunks",
                 params={"source": source, "offset": offset, "limit": limit}, timeout=60)


def delete_doc(s: requests.Session, source: str):
    return s.delete(BASE + "/rag/documents", params={"source": source}, timeout=60)


def chat(s: requests.Session, question: str, history=None, timeout: int = 180):
    return s.post(BASE + "/rag/chat",
                  json={"question": question, "history": history or []}, timeout=timeout)


def make_pdf_no_text() -> bytes:
    """生成无文本层的 PDF（只有矢量图形、无图片）：模拟扫描件但 OCR 也读不出字。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(60, 60, 500, 500), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    data = doc.tobytes()
    doc.close()
    return data


def make_pdf_with_text() -> bytes:
    """生成有文本层的 PDF：用于正常页解析 + 页码引用。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Enterprise Copilot PDF page marker: 蓝鲸-9001")
    data = doc.tobytes()
    doc.close()
    return data


def main() -> int:
    ethen = new_session(login("Ethen"))
    alice = new_session(login("alice"))

    def cleanup(names):
        for name in list(names):
            try:
                r = delete_doc(ethen, name)
                if r.status_code not in (200, 404):
                    print(f"清理失败 {name}: HTTP {r.status_code} {r.text[:120]}")
            except Exception as e:  # noqa: BLE001
                print(f"清理异常 {name}: {e}")

    # 起点：清掉历史残留，记录基线
    baseline = documents(ethen)
    cleanup([d["source"] for d in baseline if d["source"].startswith("matrix_")])
    baseline = documents(ethen)
    baseline_chunks = sum(d["chunks"] for d in baseline)

    # ---------------- 认证边界 ----------------
    anon = new_session()
    r = anon.get(BASE + "/rag/documents", timeout=30)
    record("认证：无令牌访问受保护接口", "401", f"HTTP {r.status_code}", r.status_code == 401)

    forged = new_session("aaa.bbb.ccc")
    r = forged.get(BASE + "/rag/documents", timeout=30)
    record("认证：伪造令牌", "401", f"HTTP {r.status_code}", r.status_code == 401)

    # ---------------- 迁移后旧文档可用 ----------------
    bench = [d for d in baseline if d["source"] == "benchmark_test.md"]
    ok = len(bench) == 1 and bench[0]["chunks"] > 0
    record("迁移：Ethen 名下存在旧文档 benchmark_test.md",
           "1 篇且块数>0", f"{len(bench)} 篇 / {bench[0]['chunks'] if bench else 0} 块", ok)
    record("迁移：基线块数（迁移后 132 块）", "132", str(baseline_chunks), baseline_chunks == 132)

    # ---------------- 空文件 / 纯空白 / 扫描件 ----------------
    before = sum(d["chunks"] for d in documents(ethen))
    r = upload(ethen, "matrix_empty.txt", b"")
    record("空文件（0 字节）", "422 + 未提取到文本",
           f"HTTP {r.status_code} {r.text[:80]}",
           r.status_code == 422 and EMPTY_MSG_HINT in r.text)
    after = sum(d["chunks"] for d in documents(ethen))
    record("空文件：旧数据未被动", "块数不变", f"{before} -> {after}", before == after)

    r = upload(ethen, "matrix_blank.txt", b"   \n\t\r\n  ")
    record("纯空白 txt", "422", f"HTTP {r.status_code} {r.text[:80]}", r.status_code == 422)

    r = upload(ethen, "matrix_scan.pdf", make_pdf_no_text())
    record("扫描件 PDF（无文本层、OCR 无结果）", "422",
           f"HTTP {r.status_code} {r.text[:80]}", r.status_code == 422)

    # ---------------- 编码 / 大文件 / 超长单行 ----------------
    gbk_text = "这是 GBK 编码的文档，关键口令是：蓝鲸-7749。集团年会定于 2026 年 12 月 18 日。"
    r = upload(ethen, "matrix_gbk.txt", gbk_text.encode("gbk"))
    ok = r.status_code == 200 and r.json().get("chunks", 0) > 0
    record("GBK 编码 txt", "200 且块数>0", f"HTTP {r.status_code} {r.text[:80]}", ok)
    if ok:
        CREATED.add("matrix_gbk.txt")

    r = upload(ethen, "matrix_51mb.txt", b"a" * (51 * 1024 * 1024))
    record("超大文件（51MB > 50MB 上限）", "413", f"HTTP {r.status_code} {r.text[:80]}",
           r.status_code == 413)

    long_line = ("超长单元格：" + "甲" * 8000).encode("utf-8")
    r = upload(ethen, "matrix_longline.txt", long_line)
    ok = r.status_code == 200 and r.json().get("chunks", 0) > 1
    record("超长单行（8000 字无换行）", "200 且正常切块",
           f"HTTP {r.status_code} " + (r.text[:80] if not ok else f"{r.json()['chunks']} 块"), ok)
    if ok:
        CREATED.add("matrix_longline.txt")

    r = upload(ethen, "matrix_pdf.pdf", make_pdf_with_text())
    ok = r.status_code == 200 and r.json().get("chunks", 0) > 0 and r.json().get("paged") is True
    record("正常文本 PDF（页码可追溯）", "200 且 paged=true",
           f"HTTP {r.status_code} " + (r.text[:80] if not ok else str(r.json())), ok)
    if ok:
        CREATED.add("matrix_pdf.pdf")

    # ---------------- 特殊文件名 ----------------
    special_names = [
        "中文 测试#1.md",
        "dir/sub 目录/test.md",
        "特殊$字符(2024)+&=,.txt",
    ]
    for name in special_names:
        body = f"特殊文件名测试文档：{name}，内含唯一编号 {uuid.uuid4().hex[:6]}。".encode("utf-8")
        r = upload(ethen, name, body)
        got = None
        chunk_ok = False
        if r.status_code == 200:
            CREATED.add(name)
            c = chunks(ethen, name, 0, 5)
            chunk_ok = c.status_code == 200 and c.json()["total"] > 0
            got = c.status_code
        record(f"特殊文件名：{name}", "上传 200 且分块可查（query 参数）",
               f"上传 HTTP {r.status_code} / 分块 HTTP {got}", r.status_code == 200 and chunk_ok)

    # ---------------- 重复上传 / 替换 ----------------
    v1 = "机制说明：旧版本内容，关键词 红枫。"
    v2 = "机制说明：新版本内容，关键词 银杏。"
    r1 = upload(ethen, "matrix_dup.txt", v1.encode("utf-8"))
    CREATED.add("matrix_dup.txt")
    r2 = upload(ethen, "matrix_dup.txt", v2.encode("utf-8"))
    record("重复上传 replace=false", "409",
           f"首次 {r1.status_code} / 再次 {r2.status_code}",
           r1.status_code == 200 and r2.status_code == 409)

    r3 = upload(ethen, "matrix_dup.txt", v2.encode("utf-8"), replace=True)
    ok = r3.status_code == 200 and r3.json().get("replaced") is True
    record("重复上传 replace=true", "200 且 replaced=true",
           f"HTTP {r3.status_code} {r3.text[:120]}", ok)

    # ---------------- 并发同名上传 ----------------
    def _concurrent_upload(i):
        sess = new_session(ethen.headers["Authorization"].split(" ", 1)[1])
        body = f"并发的同名文档内容，线程 {i}，关键词 {uuid.uuid4().hex[:6]}。".encode("utf-8")
        return upload(sess, "matrix_concurrent.txt", body, timeout=300)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rs = list(pool.map(_concurrent_upload, range(4)))
    codes = sorted(r.status_code for r in rs)
    record("并发同名上传（非替换）", "1×200 + 3×409", str(codes),
           codes == [200, 409, 409, 409])
    if 200 in codes:
        CREATED.add("matrix_concurrent.txt")

    # ---------------- 批量同批重名 ----------------
    rb = upload_batch(ethen, [
        ("matrix_batchdup.txt", "batch A content 关键词 青松".encode("utf-8")),
        ("matrix_batchdup.txt", "batch B content 关键词 白桦".encode("utf-8")),
    ])
    ok_batch = False
    detail = f"HTTP {rb.status_code}"
    if rb.status_code == 200:
        info = wait_task(ethen, rb.json()["task_id"])
        ok_batch = (len(info.get("results", [])) == 1 and len(info.get("failed", [])) == 1)
        detail = f"受理 HTTP {rb.status_code} / 成功 {len(info.get('results', []))} 失败 {len(info.get('failed', []))}"
        if ok_batch:
            CREATED.add("matrix_batchdup.txt")
    record("批量导入同批重名", "1 成功 + 1 失败", detail, ok_batch)

    # ---------------- 异步任务：并发 + 失败重试 ----------------
    ra = upload_async(ethen, "matrix_async_a.txt", "async A 关键词 雪松".encode("utf-8"))
    rbb = upload_async(ethen, "matrix_async_b.txt", "async B 关键词 银杉".encode("utf-8"))
    ok_async = ra.status_code == 200 and rbb.status_code == 200
    detail = f"HTTP {ra.status_code}/{rbb.status_code}"
    task_a = ra.json()["task_id"] if ra.status_code == 200 else ""
    if ok_async:
        info_a = wait_task(ethen, task_a)
        info_b = wait_task(ethen, rbb.json()["task_id"])
        ok_async = (len(info_a.get("failed", [])) == 0 and len(info_b.get("failed", [])) == 0
                    and len(info_a.get("results", [])) == 1 and len(info_b.get("results", [])) == 1)
        detail = f"task_a={info_a['status']} results={len(info_a.get('results', []))}, task_b={info_b['status']}"
        if ok_async:
            CREATED.update({"matrix_async_a.txt", "matrix_async_b.txt"})
    record("异步任务并发（两个文件）", "均完成且成功", detail, ok_async)

    rf = upload_async(ethen, "matrix_async_fail.txt", b"")
    fail_detail = f"HTTP {rf.status_code}"
    ok_fail = False
    if rf.status_code == 200:
        info_f = wait_task(ethen, rf.json()["task_id"])
        failed = info_f.get("failed", [])
        ok_fail = len(failed) == 1 and EMPTY_MSG_HINT in failed[0].get("error", "")
        fail_detail = f"failed={failed}"
    record("异步任务失败（空文件）", "任务完成并记录失败原因",
           fail_detail, ok_fail)

    rg = upload(ethen, "matrix_async_fail.txt", "retry after failure 关键词 银杏叶".encode("utf-8"))
    record("失败后重试同名上传", "200", f"HTTP {rg.status_code} {rg.text[:80]}",
           rg.status_code == 200)
    if rg.status_code == 200:
        CREATED.add("matrix_async_fail.txt")

    # ---------------- 跨账号越权 ----------------
    r = alice.get(BASE + "/rag/documents/chunks",
                  params={"source": "benchmark_test.md", "offset": 0, "limit": 1}, timeout=30)
    record("越权：alice 读 Ethen 文档分块", "404", f"HTTP {r.status_code}", r.status_code == 404)

    r = delete_doc(alice, "benchmark_test.md")
    record("越权：alice 删除 Ethen 文档", "404", f"HTTP {r.status_code}", r.status_code == 404)

    if task_a:
        r = alice.get(BASE + f"/rag/tasks/{task_a}", timeout=30)
        record("越权：alice 查询 Ethen 任务", "404", f"HTTP {r.status_code}", r.status_code == 404)

    r = alice.post(BASE + "/rag/chat", json={"question": "2020年7月31日"}, timeout=180)
    alice_sources = {d["source"] for d in documents(alice)}
    detail = f"HTTP {r.status_code}"
    ok = False
    if r.status_code == 200:
        cits = r.json().get("citations", [])
        ok = all(c.get("file") in alice_sources for c in cits) and "benchmark_test.md" not in {
            c.get("file") for c in cits}
        detail = f"引用来源={sorted({c.get('file') for c in cits})}，alice 库={sorted(alice_sources)}"
    record("越权：alice 问答只引用自己库内文档", "引用全部属于 alice 自己的库", detail, ok)

    # ---------------- 问答：入参上限 ----------------
    r = chat(ethen, "问" * 2001)
    record("超长 question（2001 字）", "400", f"HTTP {r.status_code} {r.text[:80]}",
           r.status_code == 400)

    r = chat(ethen, "历史上限测试", history=[{"role": "user", "content": "hi"}] * 101)
    record("超长 history（101 条）", "400", f"HTTP {r.status_code} {r.text[:80]}",
           r.status_code == 400)

    r = chat(ethen, "历史字符上限测试",
             history=[{"role": "user", "content": "x" * 9000} for _ in range(5)])
    record("超长 history（4.5 万字）", "400", f"HTTP {r.status_code} {r.text[:80]}",
           r.status_code == 400)

    # ---------------- 问答：拒答 / 引用 / 旧文档仍可用 ----------------
    r = chat(ethen, "请解释量子色动力学中渐近自由现象的数学推导。")
    ok = False
    detail = f"HTTP {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        ok = (body["answer"].strip() == "根据现有文档无法回答该问题。"
              or body.get("reflection", {}).get("no_answer") is True)
        detail = f"answer={body['answer'][:30]!r} no_answer={body.get('reflection', {}).get('no_answer')}"
    record("无命中拒答", "拒答话术或无引用", detail, ok)

    r = chat(ethen, "文档里提到的 2020年7月31日 是什么？")
    ok = False
    detail = f"HTTP {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        docs_now = {d["source"]: d for d in documents(ethen)}
        cits = body.get("citations", [])
        ok = bool(cits) and all(c.get("file") in docs_now for c in cits)
        if ok:
            c0 = cits[0]
            cc = chunks(ethen, c0["file"], offset=int(c0["chunk"]), limit=1)
            ok = (cc.status_code == 200 and cc.json()["chunks"]
                  and cc.json()["chunks"][0]["chunk_index"] == c0["chunk"]
                  and cc.json()["chunks"][0]["chunk_type"] in ("child", "flat"))
            detail = f"citation={c0['file']}#{c0['chunk']} 回查={cc.status_code}"
        else:
            detail = f"citations={cits[:2]}"
    record("引用回填正确性（含新建文档后旧文档仍可答）", "引用可回查到原块",
           detail, ok)

    # ---------------- 清理与终态 ----------------
    cleanup(CREATED)
    final = documents(ethen)
    final_chunks = sum(d["chunks"] for d in final)
    record("清理后：Ethen 知识库恢复基线", str(baseline_chunks),
           f"{final_chunks} 块 / {len(final)} 篇", final_chunks == baseline_chunks and len(final) == len(baseline))

    passed = sum(1 for *_, ok in RESULTS if ok)
    print(f"\n== 总计 {len(RESULTS)} 项：通过 {passed}，失败 {len(RESULTS) - passed} ==")
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "tmp", "rag_matrix_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump([{"case": c, "expected": e, "actual": a, "ok": o}
                   for c, e, a, o in RESULTS], f, ensure_ascii=False, indent=2)
    print("结果已写入 " + out)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
