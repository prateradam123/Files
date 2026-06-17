# Outbound Payload Lineage Skill

This folder is a reusable agent skill for tracing every field of an outbound object back to its earliest reachable source.

## Normal run output

```text
lineage-runs/<run-name>/
  lineage-report.json
  lineage-report.md
  lineage-viewer.html
```

`lineage-viewer.html` is self-contained. It embeds the generated JSON into a prebuilt React Flow viewer shell and can be opened directly in a browser.

## Package viewer

```bash
python .agent-skills/outbound-payload-lineage/scripts/package_viewer.py \
  --json lineage-runs/<run-name>/lineage-report.json \
  --out lineage-runs/<run-name>/lineage-viewer.html
```

## Validate report

```bash
python .agent-skills/outbound-payload-lineage/scripts/validate_lineage_report.py \
  lineage-runs/<run-name>/lineage-report.json
```

## Source of truth

The JSON is the source of truth. The HTML is a shareable export.
