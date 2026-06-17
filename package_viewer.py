#!/usr/bin/env python3
"""Package a lineage JSON report into a self-contained HTML viewer.

The viewer template is a compiled React Flow app with a sample fallback report.
This script embeds the requested report as window.__LINEAGE_REPORT__, so the
resulting HTML can be opened directly from disk without a dev server, CDN, npm,
or a separate JSON fetch.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "viewer-template" / "prebuilt-viewer-template.html"


def safe_json_for_script(data: object) -> str:
    """Dump JSON safely for inclusion inside an HTML <script>."""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    # Avoid ending the script tag or triggering HTML parser edge cases.
    return (
        text.replace("</", "<\\/")
        .replace("<!--", "<\\!--")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Embed lineage-report.json into the prebuilt lineage viewer HTML.")
    parser.add_argument("--json", required=True, help="Path to lineage-report.json")
    parser.add_argument("--out", required=True, help="Output path for lineage-viewer.html")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="Path to prebuilt-viewer-template.html")
    args = parser.parse_args()

    json_path = Path(args.json)
    template_path = Path(args.template)
    out_path = Path(args.out)

    report = json.loads(json_path.read_text(encoding="utf-8"))
    template = template_path.read_text(encoding="utf-8")

    marker = "window.__LINEAGE_REPORT__ = window.__LINEAGE_REPORT__ || null;"
    if marker not in template:
        raise RuntimeError(f"Template does not contain expected marker: {marker}")

    embedded = "window.__LINEAGE_REPORT__ = " + safe_json_for_script(report) + ";"
    html = template.replace(marker, embedded, 1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
