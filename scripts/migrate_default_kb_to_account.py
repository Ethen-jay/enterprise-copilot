# -*- coding: utf-8 -*-
"""一次性迁移：把 legacy 的 default 知识库迁到演示主账号（默认 Ethen）。

背景（X-1）：身份改为 JWT 派生后不再有"缺少身份回退 default"，`rag_knowledge_base`
（132 块）如果不迁移就没人能看见。做法：
  1. 原样复制 source collection → 目标账号 collection（含文本/元数据/向量）；
  2. 逐条比对 ID 与文本摘要，校验通过后才允许删除源 collection；
  3. 保留迁移记录（含备份目录与内容摘要），并支持 --rollback 反向复制回到源；
  4. 源集合在迁移时不被改写：只有显式 --delete-source 且校验通过才删除。

用法（先停服务）：
    .venv\\Scripts\\python.exe scripts\\migrate_default_kb_to_account.py --dry-run
    .venv\\Scripts\\python.exe scripts\\migrate_default_kb_to_account.py --delete-source \\
        --backup-dir data/backups/chroma_pre_m1_20261106_155825

    # 回滚（把目标账号数据原样复制回 rag_knowledge_base，需先确认源集合为空）
    .venv\\Scripts\\python.exe scripts\\migrate_default_kb_to_account.py \\
        --rollback data/migrations/account_default_to_Ethen_xxx.json
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from skills.rag_qa.config import CHROMA_DIR  # noqa: E402
from skills.rag_qa.vector_store import DEFAULT_COLLECTION, collection_name_for  # noqa: E402

MIGRATION_DIR = PROJECT_ROOT / "data" / "migrations"
BATCH = 200


def _digest(entries: list[tuple[str, str]]) -> str:
    """内容摘要：对 (id, 文本) 排序后做 sha256，用于证明目标与源一致。"""
    h = hashlib.sha256()
    for id_, text in sorted(entries):
        h.update(id_.encode("utf-8"))
        h.update(b"\x00")
        h.update(text.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _read_all(collection) -> tuple[list[str], list[str], list[dict], list[list[float]]]:
    ids, docs, metas, embeddings = [], [], [], []
    offset = 0
    while True:
        res = collection.get(
            include=["documents", "metadatas", "embeddings"], limit=BATCH, offset=offset
        )
        batch_ids = res.get("ids") or []
        if not batch_ids:
            break
        ids.extend(batch_ids)
        docs.extend(res.get("documents") or [""] * len(batch_ids))
        metas.extend(res.get("metadatas") or [{}] * len(batch_ids))
        raw_embeddings = res.get("embeddings")
        if raw_embeddings is not None:  # Chroma 可能返回 numpy 数组，不能用 or 判断
            for e in raw_embeddings:
                embeddings.append(list(map(float, e.tolist() if hasattr(e, "tolist") else e)))
        if len(batch_ids) < BATCH:
            break
        offset += len(batch_ids)
    return ids, docs, metas, embeddings


def _create_like(client, name: str, metadata: dict):
    meta = dict(metadata or {})
    return client.get_or_create_collection(name=name, metadata=meta or None)


def _copy(dst, ids, docs, metas, embeddings) -> int:
    for i in range(0, len(ids), BATCH):
        dst.add(
            ids=ids[i:i + BATCH],
            documents=docs[i:i + BATCH],
            metadatas=metas[i:i + BATCH],
            embeddings=embeddings[i:i + BATCH],
        )
    return len(ids)


def migrate(source_name: str, target_user: str, dry_run: bool, delete_source: bool, backup_dir: str) -> int:
    import chromadb

    target_name = collection_name_for(target_user)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    names = {c if isinstance(c, str) else c.name for c in client.list_collections()}
    if source_name not in names:
        print(f"[失败] 源 collection 不存在: {source_name}")
        return 2
    if target_name == source_name:
        print("[失败] 源与目标相同，无需迁移")
        return 2

    src = client.get_collection(source_name)
    s_ids, s_docs, s_metas, s_embs = _read_all(src)
    print(f"源 {source_name}: {len(s_ids)} 块")
    if not s_ids:
        print("[跳过] 源集合为空")
        return 0

    if target_name in names:
        dst = client.get_collection(target_name)
        t_ids, _, _, _ = _read_all(dst)
        print(f"目标 {target_name} 已存在: {len(t_ids)} 块")
        if t_ids:
            print("[失败] 目标集合已有数据，为避免混写已中止；请先确认目标为空或换账号")
            return 3
    elif dry_run:
        dst = None
    else:
        dst = _create_like(client, target_name, src.metadata)

    if dry_run:
        print(f"[dry-run] 将复制 {len(s_ids)} 块 → {target_name}，摘要 {_digest(list(zip(s_ids, s_docs)))[:16]}")
        return 0

    copied = _copy(dst, s_ids, s_docs, s_metas, s_embs)
    t_ids, t_docs, _, t_embs = _read_all(dst)

    # 校验：ID 集合与内容摘要必须完全一致，向量条数一致
    ok = (
        set(t_ids) == set(s_ids)
        and _digest(list(zip(s_ids, s_docs))) == _digest(list(zip(t_ids, t_docs)))
        and len(t_embs) == len(s_embs)
    )
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": source_name,
        "target": target_name,
        "backup_dir": backup_dir or None,
        "source_count": len(s_ids),
        "target_count": len(t_ids),
        "source_digest": _digest(list(zip(s_ids, s_docs))),
        "target_digest": _digest(list(zip(t_ids, t_docs))),
        "verified": ok,
        "source_deleted": False,
    }
    MIGRATION_DIR.mkdir(parents=True, exist_ok=True)
    record_path = MIGRATION_DIR / f"account_{source_name}_to_{target_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    if not ok:
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[失败] 校验不一致，已保留源集合与迁移记录: {record_path}")
        return 4

    print(f"[成功] 已复制 {copied} 块 → {target_name}，内容摘要一致")

    if delete_source:
        client.delete_collection(source_name)
        record["source_deleted"] = True
        print(f"[成功] 已删除源 collection {source_name}（可用 --rollback 或备份恢复）")

    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"迁移记录: {record_path}")
    return 0


def rollback(record_path: Path) -> int:
    """把目标账号的块原样复制回源 collection（源必须不存在或为空）。"""
    import chromadb

    record = json.loads(record_path.read_text(encoding="utf-8"))
    source_name, target_name = record["source"], record["target"]
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    names = {c if isinstance(c, str) else c.name for c in client.list_collections()}

    if target_name not in names:
        print(f"[失败] 目标 collection 不存在，无法回滚: {target_name}")
        return 2
    if source_name in names and _read_all(client.get_collection(source_name))[0]:
        print(f"[失败] 源 collection 已存在且非空，拒绝覆盖: {source_name}")
        return 3

    dst = client.get_collection(target_name)
    ids, docs, metas, embs = _read_all(dst)
    if not ids:
        print("[失败] 目标集合为空，无可回滚数据")
        return 2

    src = _create_like(client, source_name, dst.metadata)
    if _read_all(src)[0]:
        print(f"[失败] 源 collection 已存在且非空，拒绝覆盖: {source_name}")
        return 3
    _copy(src, ids, docs, metas, embs)
    v_ids, v_docs, _, _ = _read_all(src)
    ok = _digest(list(zip(ids, docs))) == _digest(list(zip(v_ids, v_docs)))
    print(f"[{'成功' if ok else '失败'}] 回滚 {len(ids)} 块 → {source_name}（校验={ok}）")
    return 0 if ok else 4


def main() -> int:
    parser = argparse.ArgumentParser(description="把 default 知识库迁移到指定账号")
    parser.add_argument("--source", default=DEFAULT_COLLECTION)
    parser.add_argument("--to", default="Ethen")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delete-source", action="store_true")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--rollback", default="", help="迁移记录 JSON 路径，执行反向复制")
    args = parser.parse_args()

    if args.rollback:
        return rollback(Path(args.rollback))
    return migrate(args.source, args.to, args.dry_run, args.delete_source, args.backup_dir)


if __name__ == "__main__":
    raise SystemExit(main())
