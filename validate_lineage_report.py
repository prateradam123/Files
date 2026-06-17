#!/usr/bin/env python3
"""Lightweight validation for lineage-report.json.

This intentionally avoids third-party packages so it can run in locked-down
corporate environments. It validates the high-value invariants that matter most
for the viewer and for report quality.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VALID_CONFIDENCE = {"high", "medium", "low"}
VALID_SHAPE_KINDS = {"object", "array", "field"}
VALID_NODE_KINDS = {
    "object", "group", "array", "field", "assignment", "transform", "source", "method",
    "repository", "event", "external", "config", "constant", "runtime", "unknown",
}


def collect_leaf_paths(node: dict) -> list[str]:
    kind = node.get("kind")
    if kind == "field":
        return [node.get("path", "")]
    paths: list[str] = []
    for child in node.get("children", []) or []:
        paths.extend(collect_leaf_paths(child))
    return paths


def validate(report: dict) -> list[str]:
    errors: list[str] = []

    for key in ["object", "summary", "objectShape", "fields"]:
        if key not in report:
            errors.append(f"Missing top-level key: {key}")

    object_shape = report.get("objectShape", {})
    fields = report.get("fields", [])

    leaf_paths = set(collect_leaf_paths(object_shape))
    field_paths = {field.get("path") for field in fields}

    missing_field_reports = sorted(leaf_paths - field_paths)
    extra_field_reports = sorted(field_paths - leaf_paths)
    if missing_field_reports:
        errors.append("Leaf fields missing field report entries: " + ", ".join(missing_field_reports))
    if extra_field_reports:
        errors.append("Field reports not present in objectShape leaves: " + ", ".join(extra_field_reports))

    def walk_shape(node: dict, path: str = "objectShape") -> None:
        kind = node.get("kind")
        if kind not in VALID_SHAPE_KINDS:
            errors.append(f"{path}: invalid shape kind {kind!r}")
        if not node.get("name"):
            errors.append(f"{path}: missing name")
        if not node.get("path"):
            errors.append(f"{path}: missing path")
        if not node.get("type"):
            errors.append(f"{path}: missing type")
        for idx, child in enumerate(node.get("children", []) or []):
            walk_shape(child, f"{path}.children[{idx}]")

    walk_shape(object_shape)

    for idx, field in enumerate(fields):
        prefix = f"fields[{idx}] {field.get('path', '<no-path>')}"
        if field.get("confidence") not in VALID_CONFIDENCE:
            errors.append(f"{prefix}: invalid confidence {field.get('confidence')!r}")
        lineage = field.get("lineage") or {}
        nodes = lineage.get("nodes") or []
        edges = lineage.get("edges") or []
        node_ids = {node.get("id") for node in nodes}
        if not nodes:
            errors.append(f"{prefix}: lineage.nodes is empty")
        if not edges:
            errors.append(f"{prefix}: lineage.edges is empty")
        for node in nodes:
            if node.get("kind") not in VALID_NODE_KINDS:
                errors.append(f"{prefix}: node {node.get('id')!r} invalid kind {node.get('kind')!r}")
            if not node.get("title"):
                errors.append(f"{prefix}: node {node.get('id')!r} missing title")
        for edge in edges:
            if not isinstance(edge, list) or len(edge) < 3:
                errors.append(f"{prefix}: invalid edge {edge!r}")
                continue
            src, dst = edge[0], edge[1]
            if src != "FIELD" and src not in node_ids:
                errors.append(f"{prefix}: edge source {src!r} not found")
            if dst not in node_ids:
                errors.append(f"{prefix}: edge target {dst!r} not found")

    summary = report.get("summary", {})
    if summary.get("fieldsFound") != len(fields):
        errors.append(f"summary.fieldsFound={summary.get('fieldsFound')} but fields has {len(fields)} entries")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate lineage-report.json for the outbound-payload-lineage viewer.")
    parser.add_argument("json_path")
    args = parser.parse_args()

    report = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
    errors = validate(report)
    if errors:
        for error in errors:
            print("ERROR:", error, file=sys.stderr)
        return 1
    print("OK: lineage report passed validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
