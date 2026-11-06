# -*- coding: utf-8 -*-
"""一次性迁移：给历史单层分块补齐 chunk_type="flat"。

背景（M-1）：父子分块上线后，检索条件曾是「chunk_type == child」，而历史数据
（如 default 的 rag_knowledge_base 共 132 块）根本没有 chunk_type 字段，导致
一旦有新文档写入（父子模式），旧数据就被静默过滤掉、查不到。

修复后检索条件改为「排除 parent」（child + flat 都可召回），因此历史块必须显式
打上 chunk_type="flat" 才能被新检索条件覆盖。本脚本做一次性补标，可重复执行
（幂等：只处理缺失 chunk_type 的块）。

用法（先停服务，避免 SQLite 并发写）：
    .venv\\Scripts\\python.exe scripts\\migrate_legacy_flat_chunks.py --dry-run
    .venv\\Scripts\\python.exe scripts\\migrate_legacy_flat_chunks.py
    .venv\\Scripts\\python.exe scripts\\migrate_legacy_flat_chunks.py --backup-dir data/backups/chroma_pre_m1_xxx

迁移前请先备份 data/chroma（脚本会把备份目录写入迁移记录，便于回滚溯源）。
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 允许 `python scripts/xxx.py` 直接运行（把仓库根加入 sys.path）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from skills.rag_qa.config import CHROMA_DIR  # noqa: E402

MIGRATION_DIR = PROJECT_ROOT / "data" / "migrations"
BATCH = 500


def _collection_name(col) -> str:
    """兼容不同 chromadb 版本：list_collections 可能返回 Collection 或名字。"""
    return col if isinstance(col, str) else col.name


def _iter_entries(collection):
    """分页读取全部 (id, metadata)。"""
    offset = 0
    while True:
        res = collection.get(include=["metadatas"], limit=BATCH, offset=offset)
        ids = res.get("ids") or []
        metas = res.get("metadatas") or []
        if not ids:
            return
        for id_, meta in zip(ids, metas):
            yield id_, meta
        if len(ids) < BATCH:
            return
        offset += len(ids)


def migrate(dry_run: bool, backup_dir: str) -> dict:
    import chromadb

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    report = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "chroma_dir": str(CHROMA_DIR),
        "backup_dir": backup_dir or None,
        "dry_run": dry_run,
        "collections": {},
    }

    for col in client.list_collections():
        name = _collection_name(col)
        collection = client.get_collection(name)

        pending: list[tuple[str, dict]] = []
        stats = {"total": 0, "already_tagged": 0, "flat": 0, "child": 0, "parent": 0}
        for id_, meta in _iter_entries(collection):
            stats["total"] += 1
            meta = dict(meta or {})
            chunk_type = meta.get("chunk_type")
            if chunk_type:
                stats["already_tagged"] += 1
                if chunk_type in stats:
                    stats[chunk_type] += 1
                continue
            meta["chunk_type"] = "flat"
            pending.append((id_, meta))

        stats["to_migrate"] = len(pending)
        if pending and not dry_run:
            for i in range(0, len(pending), BATCH):
                batch = pending[i:i + BATCH]
                # update 会整体替换 metadata，所以必须回传合并后的完整 metadata
                collection.update(
                    ids=[b[0] for b in batch],
                    metadatas=[b[1] for b in batch],
                )
        report["collections"][name] = stats
        action = "待迁移" if dry_run else "已迁移"
        print(
            f"[{name}] 总块数={stats['total']}，{action}={len(pending)}，"
            f"已有标记={stats['already_tagged']}（child={stats['child']}, "
            f"parent={stats['parent']}, flat={stats['flat']}）"
        )

    # 迁移后自检：不应再有缺失 chunk_type 的块
    if not dry_run:
        missing = {}
        for col in client.list_collections():
            name = _collection_name(col)
            collection = client.get_collection(name)
            n = sum(1 for _, meta in _iter_entries(collection) if not (meta or {}).get("chunk_type"))
            if n:
                missing[name] = n
        report["verify_missing_chunk_type"] = missing
        if missing:
            print(f"[自检失败] 仍有块缺少 chunk_type: {missing}")
        else:
            print("[自检通过] 所有块均已带 chunk_type")

    MIGRATION_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "dryrun" if dry_run else "applied"
    record = MIGRATION_DIR / f"chunk_type_{suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    record.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"迁移记录: {record}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="给历史单层分块补齐 chunk_type=flat")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--backup-dir", default="", help="迁移前备份目录（写入记录，便于回滚）")
    args = parser.parse_args()

    report = migrate(dry_run=args.dry_run, backup_dir=args.backup_dir)
    if report.get("verify_missing_chunk_type"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
