"""SCRATCH: probe the self-contained static scanner for gaps."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.selfcontained import check_self_contained  # noqa: E402

CASES = {
    "style_block_url": "<html><head><style>body{background:url(https://cdn.example.com/bg.png)}</style></head><body>hi</body></html>",
    "style_block_import": "<html><head><style>@import url('https://fonts.googleapis.com/css2?family=Inter');</style></head><body>hi</body></html>",
    "style_block_fontface": "<html><head><style>@font-face{font-family:X;src:url(https://cdn.example.com/x.woff2)}</style></head><body>hi</body></html>",
    "link_stylesheet": "<html><head><link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=Inter'></head><body>hi</body></html>",
    "img_src": "<html><body><img src='https://cdn.example.com/a.png'></body></html>",
}

for name, html in CASES.items():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "dist").mkdir()
        (ws / "dist" / "index.html").write_text(html, encoding="utf-8")
        report = check_self_contained(ws)
        print(f"{name:22s} ok={report.ok}  findings={[f.kind + ':' + f.host for f in report.findings]}")
