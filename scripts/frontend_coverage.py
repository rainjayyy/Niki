"""前端 API client 实际调用 vs 后端端点 vs mock 覆盖"""
import re
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent / "niki" / "api"
FRONT_API = Path(__file__).resolve().parent.parent.parent / "Niki_frontend" / "src" / "api"
MOCK_FILE = Path(__file__).resolve().parent.parent.parent / "Niki_frontend" / "src" / "mock" / "adapter.ts"

# 后端端点
EP_RE = re.compile(r'@router\.(get|post|put|delete|patch)\("([^"]+)"')
PREFIX_RE = re.compile(r'APIRouter\(prefix="([^"]+)"')

backend_eps = []
for py in sorted(API_DIR.glob("*.py")):
    if py.name == "router.py":
        continue
    content = py.read_text(encoding="utf-8")
    prefix_m = PREFIX_RE.search(content)
    prefix = prefix_m.group(1) if prefix_m else ""
    for m in EP_RE.finditer(content):
        backend_eps.append((m.group(1).upper(), prefix + m.group(2), py.stem))

# 前端 API 调用路径（含模板插值）
frontend_paths = set()
for ts in sorted(FRONT_API.glob("*.ts")):
    if ts.name in ("http.ts", "__tests__"):
        continue
    text = ts.read_text(encoding="utf-8")
    for m in re.finditer(r"(get|post|put|patch|delete)<[^>]+>\(\s*[`'\"](/[^`'\"]+)[`'\"]", text):
        path = m.group(2)
        frontend_paths.add(path)

# mock 关键词
mock_text = MOCK_FILE.read_text(encoding="utf-8")
mock_kws = set()
for m in re.finditer(r"endsWith\(['\"]([^'\"]+)['\"]\)", mock_text):
    mock_kws.add(m.group(1).lstrip("/"))
for m in re.finditer(r"includes\(['\"]([^'\"]+)['\"]\)", mock_text):
    mock_kws.add(m.group(1).lstrip("/"))
# 提取 url.match(/完整 regex/) → 转成 ${id} 占位
for m in re.finditer(r"url\.match\((/[^)]+/)\)", mock_text):
    rx_body = m.group(1)[1:-1]  # 去首尾 /
    # 清理转义：\\/ → /, [^\/]+ → ${id}, [^/]+ → ${id}
    rx_body = rx_body.replace(r"\\/", "/")
    rx_body = rx_body.replace(r"[^\/]+", "${id}").replace(r"[^/]+", "${id}")
    if rx_body.startswith("/"):
        rx_body = rx_body[1:]
    mock_kws.add("/" + rx_body)
mock_kws.update([
    "parents/{parent_id}/letters",
    "children/{child_id}/letters",
    "children/letters/{letter_id}/read",
    "dashboard/events/risk",
    "metrics/vitals",
    "metrics/vitals/recent",
    "push/vapid-key", "push/subscribe", "push/unsubscribe",
])

def endswith_any(s: str, kws) -> bool:
    # 第一遍：占位符 regex 关键词
    for kw in kws:
        if "${id}" in kw or "{id}" in kw:
            rx = kw.replace("${id}", "[^/]+").replace("{id}", "[^/]+")
            try:
                if re.search(rx, s):
                    return True
            except re.error:
                pass
    # 第二遍：纯字符串 endswith
    for kw in kws:
        if "${id}" in kw or "{id}" in kw:
            continue
        if s.endswith(kw) or s.endswith(kw.rstrip("/")):
            return True
        s_norm = re.sub(r"/\$\{[^}]+\}", "", s)
        if s_norm.endswith(kw.lstrip("/")):
            return True
    return False

# 后端端点对应前端调用（含模板）→ 计算覆盖
def normalize_path(p: str) -> str:
    return re.sub(r"/\{[^}]+\}", "/${id}", p)

# 前端调用的归一化路径
frontend_norm = set(normalize_path(p) for p in frontend_paths)

# 后端端点 → 是否被前端调用
backend_called = []
backend_unused = []
for m, p, s in backend_eps:
    if normalize_path(p) in frontend_norm:
        backend_called.append((m, p, s))
    else:
        backend_unused.append((m, p, s))

# 前端调用 → 是否被 mock 覆盖
mock_misses = []
for fp in frontend_paths:
    if not endswith_any(fp, mock_kws):
        mock_misses.append(fp)

print(f"\n=== 真相 ===")
print(f"  后端实际端点:        {len(backend_eps)}")
print(f"  前端业务调用:        {len(frontend_paths)}  ({len(frontend_paths)/len(backend_eps)*100:.0f}% of backend)")
print(f"  前端调用 → mock 覆盖: {len(frontend_paths) - len(mock_misses)} / {len(frontend_paths)}  ({(len(frontend_paths) - len(mock_misses))/len(frontend_paths)*100:.0f}%)")
print(f"  后端端点未被前端调用: {len(backend_unused)}  (内部/编排用)")
print(f"\n=== 前端 mock 漏掉的端点（{len(mock_misses)}）===")
for fp in sorted(mock_misses):
    print(f"  {fp}")

print(f"\n=== 后端有但前端不需要的端点（{len(backend_unused)}）===")
from collections import defaultdict
by_src = defaultdict(list)
for m, p, s in backend_unused:
    by_src[s].append(f"{m} {p}")
for s, items in sorted(by_src.items()):
    print(f"\n--- {s} ({len(items)}) ---")
    for it in items[:5]:
        print(f"  {it}")
    if len(items) > 5:
        print(f"  ... and {len(items) - 5} more")