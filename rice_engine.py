#!/usr/bin/env python3
"""AI RICE Prioritization Engine.

An LLM estimates Reach, Impact, Confidence, and Effort for each backlog item
(with a rationale). Python then computes the RICE score, ranks the backlog,
and places items into releases based on team capacity and dependencies.

The model supplies judgment; the math and the release plan stay deterministic
and auditable, and a PM can review or override any estimate before planning.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# Standard RICE scales (Intercom). Estimates are snapped to these values.
IMPACT_SCALE = [3, 2, 1, 0.5, 0.25]
CONFIDENCE_SCALE = [1.0, 0.8, 0.5]

SYSTEM_PROMPT = """You are an experienced B2B SaaS product manager scoring a backlog with RICE.

For each item, estimate:
- reach: number of accounts affected per quarter (integer, grounded in the product context)
- impact: one of 3 (massive), 2 (high), 1 (medium), 0.5 (low), 0.25 (minimal)
- confidence: one of 1.0 (high), 0.8 (medium), 0.5 (low); use 0.5 when evidence is thin or untested
- effort: person-months across engineering, design, and QA (number > 0)
- rationale: one or two sentences tying the estimate to the evidence and strategy

Be conservative: do not inflate reach or impact without evidence in the item's customer signal.
Respond with JSON only, no prose or code fences, in this shape:
{"estimates": [{"id": "...", "reach": 0, "impact": 0, "confidence": 0, "effort": 0, "rationale": "..."}]}"""


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_backlog(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["depends_on"] = [d.strip() for d in (row.get("depends_on") or "").split(";") if d.strip()]
    return rows


def load_estimates(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {e["id"]: e for e in data["estimates"]}


# --------------------------------------------------------------------------- #
# LLM estimation
# --------------------------------------------------------------------------- #
def _parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"):]
    return json.loads(cleaned[cleaned.find("{"): cleaned.rfind("}") + 1])


def estimate_with_claude(items: list[dict], context: str, model: str, batch_size: int) -> dict[str, dict]:
    try:
        from anthropic import Anthropic
    except ImportError:
        sys.exit("Install dependencies first: pip install -r requirements.txt")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY, or run offline with --estimates data/sample_estimates.json")

    client = Anthropic()
    estimates: dict[str, dict] = {}
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        payload = [
            {k: item[k] for k in ("id", "title", "description", "customer_signal")} for item in batch
        ]
        prompt = f"Product context:\n{context}\n\nBacklog items:\n{json.dumps(payload, indent=2)}"
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        for est in _parse_json(text)["estimates"]:
            estimates[est["id"]] = est
        print(f"  Estimated items {start + 1}-{start + len(batch)}", file=sys.stderr)
    return estimates


# --------------------------------------------------------------------------- #
# Validation and scoring
# --------------------------------------------------------------------------- #
def _snap(value: float, scale: list[float]) -> float:
    return min(scale, key=lambda s: abs(s - float(value)))


def validate(est: dict | None, item_id: str) -> tuple[dict, list[str]]:
    """Clamp an estimate to valid RICE values and record anything that changed."""
    if est is None:
        return {"reach": 0, "impact": 0.25, "confidence": 0.5, "effort": 1, "rationale": ""}, ["missing estimate"]
    flags = []
    clean = dict(est)
    clean["reach"] = max(0, int(round(float(est.get("reach", 0)))))
    for key, scale in (("impact", IMPACT_SCALE), ("confidence", CONFIDENCE_SCALE)):
        snapped = _snap(est.get(key, scale[-1]), scale)
        if snapped != float(est.get(key, -1)):
            flags.append(f"{key} {est.get(key)} snapped to {snapped}")
        clean[key] = snapped
    effort = float(est.get("effort", 0))
    if effort <= 0:
        flags.append("non-positive effort set to 0.5")
        effort = 0.5
    clean["effort"] = effort
    return clean, flags


def rice_score(est: dict) -> float:
    return est["reach"] * est["impact"] * est["confidence"] / est["effort"]


# --------------------------------------------------------------------------- #
# Release planning
# --------------------------------------------------------------------------- #
def plan_releases(ranked: list[dict], releases: list[str], capacity: float) -> None:
    """Greedy placement by rank. Dependencies are pulled forward when needed,
    and an item never lands in an earlier release than what it depends on."""
    remaining = {r: capacity for r in releases}
    by_id = {item["id"]: item for item in ranked}

    def place(item: dict, visiting: set[str]) -> int | None:
        if "release_idx" in item:
            return item["release_idx"]
        if item["id"] in visiting:  # dependency cycle
            item["release_idx"], item["plan_note"] = None, "dependency cycle"
            return None
        visiting.add(item["id"])
        earliest = 0
        for dep_id in item["depends_on"]:
            dep = by_id.get(dep_id)
            dep_idx = place(dep, visiting) if dep else None
            if dep_idx is None:
                item["release_idx"], item["plan_note"] = None, f"blocked by {dep_id}"
                return None
            if dep is not None and dep.get("pulled_forward_by") is None and ranked.index(dep) > ranked.index(item):
                dep["pulled_forward_by"] = item["id"]
            earliest = max(earliest, dep_idx)
        for idx in range(earliest, len(releases)):
            if remaining[releases[idx]] >= item["effort"]:
                remaining[releases[idx]] -= item["effort"]
                item["release_idx"] = idx
                return idx
        item["release_idx"], item["plan_note"] = None, "over capacity"
        return None

    for item in ranked:
        place(item, set())
    for item in ranked:
        idx = item.get("release_idx")
        item["release"] = releases[idx] if idx is not None else "Backlog"


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_csv(ranked: list[dict], path: Path) -> None:
    fields = ["rank", "id", "title", "reach", "impact", "confidence", "effort",
              "rice_score", "release", "needs_review", "rationale"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ranked)


def write_report(ranked: list[dict], releases: list[str], capacity: float, path: Path) -> None:
    lines = ["# Prioritized roadmap", "",
             f"RICE = Reach × Impact × Confidence ÷ Effort. Capacity: {capacity:g} person-months per release.", ""]
    for release in releases + ["Backlog"]:
        items = [i for i in ranked if i["release"] == release]
        used = sum(i["effort"] for i in items)
        header = f"## {release}" + (f" ({used:g} / {capacity:g} person-months)" if release != "Backlog" else "")
        lines += [header, "", "| Rank | Item | RICE | Effort | Why |", "|---|---|---|---|---|"]
        for i in items:
            note = []
            if i.get("pulled_forward_by"):
                note.append(f"pulled forward for {i['pulled_forward_by']}")
            if i.get("plan_note"):
                note.append(i["plan_note"])
            why = i["rationale"] + (f" *({'; '.join(note)})*" if note else "")
            lines.append(f"| {i['rank']} | {i['id']} {i['title']} | {i['rice_score']:,.0f} | {i['effort']:g} | {why} |")
        if not items:
            lines.append("| | *(none)* | | | |")
        lines.append("")

    review = [i for i in ranked if i["needs_review"]]
    lines += ["## Needs PM review", "",
              "Low-confidence estimates (validate with discovery before committing) and any estimates the engine adjusted:", ""]
    lines += [f"- **{i['id']} {i['title']}**: {i['needs_review']}" for i in review] or ["- None"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Score and plan a backlog with LLM-assisted RICE.")
    parser.add_argument("--backlog", default="data/backlog.csv", type=Path)
    parser.add_argument("--context", default="data/product_context.md", type=Path)
    parser.add_argument("--estimates", type=Path, help="Use saved estimates instead of calling the API")
    parser.add_argument("--save-estimates", type=Path, help="Save model estimates as JSON for review or editing")
    parser.add_argument("--releases", default="R1,R2,R3", help="Comma-separated release names")
    parser.add_argument("--capacity", default=6, type=float, help="Person-months per release")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", default=10, type=int)
    parser.add_argument("--out", default="output", type=Path)
    args = parser.parse_args()

    items = load_backlog(args.backlog)
    if args.estimates:
        raw = load_estimates(args.estimates)
        print(f"Using saved estimates from {args.estimates}", file=sys.stderr)
    else:
        print(f"Estimating {len(items)} items with {args.model}...", file=sys.stderr)
        raw = estimate_with_claude(items, args.context.read_text(encoding="utf-8"), args.model, args.batch_size)
        if args.save_estimates:
            args.save_estimates.write_text(json.dumps({"estimates": list(raw.values())}, indent=2), encoding="utf-8")
            print(f"Saved estimates to {args.save_estimates}", file=sys.stderr)

    for item in items:
        est, flags = validate(raw.get(item["id"]), item["id"])
        item.update(est)
        if item["confidence"] <= 0.5:
            flags.insert(0, "low confidence")
        item["needs_review"] = "; ".join(flags)
        item["rice_score"] = round(rice_score(item), 1)

    ranked = sorted(items, key=lambda i: (-i["rice_score"], i["effort"], i["id"]))
    for rank, item in enumerate(ranked, 1):
        item["rank"] = rank

    releases = [r.strip() for r in args.releases.split(",") if r.strip()]
    plan_releases(ranked, releases, args.capacity)

    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(ranked, args.out / "prioritized_backlog.csv")
    write_report(ranked, releases, args.capacity, args.out / "roadmap.md")
    print(f"Wrote {args.out / 'prioritized_backlog.csv'} and {args.out / 'roadmap.md'}", file=sys.stderr)


if __name__ == "__main__":
    main()
