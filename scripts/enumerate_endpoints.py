"""枚举后端所有 REST 端点（路径 + 方法）"""
import re
import os
from pathlib import Path

ROOT = Path(__file__).parent.parent / "niki" / "api"

# 提取 prefix
def get_prefix(content: str) -> str:
    m = re.search(r'APIRouter\(prefix="([^"]+)"', content)
    return m.group(1) if m else ""

# 提取每个 @router.METHOD("/path")
EP_RE = re.compile(r'@router\.(get|post|put|delete|patch)\("([^"]+)"')

all_eps = []
for py in sorted(ROOT.glob("*.py")):
    if py.name in ("router.py",):
        continue
    content = py.read_text(encoding="utf-8")
    prefix = get_prefix(content)
    for m in EP_RE.finditer(content):
        method, path = m.group(1).upper(), m.group(2)
        full = f"{prefix}{path}" if prefix else path
        all_eps.append((method, full, py.stem))

# 输出
print(f"\n=== {len(all_eps)} 个端点 ===\n")
prev = None
for method, path, src in all_eps:
    if src != prev:
        print(f"\n--- {src} ---")
        prev = src
    print(f"  {method:7s} {path}")

# 按 method 统计
from collections import Counter
counts = Counter(m for m, _, _ in all_eps)
print(f"\n=== 分布 ===")
for m, c in counts.most_common():
    print(f"  {m:7s}: {c}")