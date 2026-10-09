"""对比后端端点 vs 前端 mock 覆盖"""

import re
from pathlib import Path

API_DIR = Path(__file__).parent.parent / "niki" / "api"
MOCK_FILE = Path(__file__).parent.parent.parent / "Niki_frontend" / "src" / "mock" / "adapter.ts"

# 提取后端端点
EP_RE = re.compile(r'@router\.(get|post|put|delete|patch)\("([^"]+)"')
PREFIX_RE = re.compile(r'APIRouter\(prefix="([^"]+)"')

backend_eps = []
for py in sorted(API_DIR.glob("*.py")):
    if py.name == "router.py":
        continue
    content = py.read_text(encoding="utf-8")
    prefix = (PREFIX_RE.search(content) or [None, ""])[1] if PREFIX_RE.search(content) else ""
    for m in EP_RE.finditer(content):
        method = m.group(1).upper()
        path = prefix + m.group(2)
        backend_eps.append((method, path, py.stem))

# 提取 mock 路由关键词
mock_text = MOCK_FILE.read_text(encoding="utf-8")
keywords = set()
for m in re.finditer(r"endsWith\(['\"]([^'\"]+)['\"]\)", mock_text):
    keywords.add(m.group(1).lstrip("/"))
for m in re.finditer(r"includes\(['\"]([^'\"]+)['\"]\)", mock_text):
    keywords.add(m.group(1).lstrip("/"))
for m in re.finditer(r"match\(/\.([^\/]+)\/", mock_text):
    keywords.add(m.group(1))
# 也包括字母路径（letters, register 等）
keywords.update(
    [
        "/parents/{parent_id}/letters",
        "/children/{child_id}/letters",
        "/children/letters/{letter_id}/read",
        "/dashboard/events/risk",  # 前端自定义
        "/metrics/vitals",  # 自定义
        "/metrics/vitals/recent",
        "/push/vapid-key",
        "/push/subscribe",
        "/push/unsubscribe",
    ]
)


# 模糊匹配：mock 的 endsWith 关键词在后端路径中以"结尾"形式出现
def covered(method: str, path: str) -> bool:
    for kw in keywords:
        # 关键词形式: "/auth/sms/send" -> 看后端路径以同样结尾
        if path.endswith(kw) or path.endswith(kw.rstrip("/")):
            return True
        # 也匹配 {param} 占位
        path_norm = re.sub(r"/\{[^}]+\}", "", path)
        if path_norm.endswith(kw.lstrip("/")):
            return True
        # regex 关键词
        if "/" in kw and re.search(kw, path):
            return True
    return False


covered_list = []
uncovered_list = []
for method, path, src in backend_eps:
    if covered(method, path):
        covered_list.append((method, path, src))
    else:
        uncovered_list.append((method, path, src))

print("\n=== 覆盖统计 ===")
print(f"  后端端点: {len(backend_eps)}")
print(f"  mock 覆盖: {len(covered_list)}  ({len(covered_list) / len(backend_eps) * 100:.0f}%)")
print(f"  未覆盖:   {len(uncovered_list)}  ({len(uncovered_list) / len(backend_eps) * 100:.0f}%)")

# 按 router 统计
from collections import defaultdict

by_src = defaultdict(lambda: [0, 0])  # [total, covered]
for m, p, s in backend_eps:
    by_src[s][0] += 1
for m, p, s in covered_list:
    by_src[s][1] += 1

print("\n=== 按 router 分布 ===")
for src in sorted(by_src):
    total, cov = by_src[src]
    pct = cov / total * 100 if total else 0
    bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
    print(f"  {src:18s} {cov:2d}/{total:2d}  {bar}  {pct:3.0f}%")

print(f"\n=== 未覆盖的端点（{len(uncovered_list)}）===")
prev_src = None
for m, p, s in uncovered_list:
    if s != prev_src:
        print(f"\n--- {s} ---")
        prev_src = s
    print(f"  {m:7s} {p}")
