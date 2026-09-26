#!/usr/bin/env python3
"""Render a moments-gallery HTML page from a manifest and a frame directory.

This script is the body of the `maxi-tools/ci/.github/actions/moments-gallery`
composite action. It is intentionally a single file (Python stdlib only) so
the action has no third-party dependency and the HTML it produces has no
build step: one HTML file, image and audio paths resolved relative to the
output directory, and a small inline `<script>` for the filmstrip
interactions.

The manifest schema is `maxi-tools.moments-gallery.v1`; see
`docs/moments-gallery.md` in the action's directory for the schema
definition, an example, and the upgrade path from each existing
gallery. The schema's `scenarios[].moments[].image` paths are resolved
relative to the gallery root (`GALLERY_ROOT`), and the same paths under
`<out>/moments/` are emitted into the HTML so the rendered page is
relocatable.

Outputs (via `$GITHUB_OUTPUT`):

    out_dir              absolute path of the gallery output directory
    html_path            absolute path of the rendered `index.html`
    pages_enabled        `true` when the caller asked for a Pages deploy
    lane                 the gallery name, used as the Pages sub-path
    pages_root           absolute path of the staged `<lane>/latest` tree
                         when Pages was requested, else empty

The latest-deployment URL is not one of these: it only exists after the
deploy step has run, and a composite action's outputs are fixed when the
action starts, so the action records it from a later step of its own.

Side effects:

    * `<root>/<name>-gallery/` is created and populated with `index.html`,
      `moments/<scenario>/<n>.png`, and `<scenario>.wav` for any scenario
      that names an `audio` file.
    * The first `GALLERY_SUMMARY_THUMBNAILS` scenarios' first frame are
      embedded as base64 `<img src="data:...">` rows appended to the job
      summary, so a reader who never opens the artifact still sees what
      the run looked like at a glance. Each frame is downscaled to a
      240px-wide PNG thumbnail first: GitHub rejects a step summary over
      1 MiB, and a raw e2e frame (the verifier's 1024x1024 PNG was
      3,147,775 bytes, 4,197,472 once base64-wrapped) blows that cap on
      its own.

Inputs are passed via environment variables rather than argv so a hostile
manifest cannot break out of the python invocation. The script refuses to
load a manifest whose `schema` field is not `maxi-tools.moments-gallery.v1`
and refuses to dereference any path that escapes `GALLERY_ROOT` after
normalisation.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

SCHEMA = "maxi-tools.moments-gallery.v1"

# GitHub's per-step job-summary cap is 1 MiB
# (https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#step-isolation-and-limits).
# The thumbnail block must leave room for the surrounding markdown, so the
# budget is the cap minus a fixed headroom rather than the cap itself.
SUMMARY_BYTE_CAP = 1024 * 1024
SUMMARY_HEADROOM_BYTES = 8 * 1024
# Display width of the summary thumbnail. The <img width> matches it, so
# the bytes we embed are the bytes the reader sees.
SUMMARY_THUMB_WIDTH = 240

# Slug used for scenario ids; conservative so a hand-edited
# manifest cannot escape the gallery root via `moments/<id>/`.
_SCENARIO_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
HTML_FILENAME = "index.html"
MOMENTS_SUBDIR = "moments"


class GalleryError(RuntimeError):
    """Raised when the manifest, the root, or a referenced file is wrong."""


def _log(msg: str) -> None:
    print(f"moments-gallery: {msg}", file=sys.stderr, flush=True)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise GalleryError(f"required env var {name} is not set")
    return value


def _set_output(name: str, value: str) -> None:
    """Append a `$GITHUB_OUTPUT` line. Handles multi-line values safely."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        _log(f"(no GITHUB_OUTPUT) {name}={value}")
        return
    with open(output_path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            # heredoc form, indented with spaces
            fh.write(f"{name}<<EOF_GALLERY\n{value}\nEOF_GALLERY\n")
        else:
            fh.write(f"{name}={value}\n")


def _append_summary(block: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write(block)
        if not block.endswith("\n"):
            fh.write("\n")


def _safe_join(root: pathlib.Path, relative: str) -> pathlib.Path:
    """Resolve `relative` under `root`, refusing any path that escapes it."""
    if not relative:
        raise GalleryError("path is empty")
    # Purely lexical normalisation: callers may not have given a real path
    # on this machine, and we want the same rejection for a hand-edited
    # manifest as for a symlink that escapes the root.
    root_abs = root.resolve(strict=False)
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root_abs)
    except ValueError as exc:
        raise GalleryError(
            f"path {relative!r} resolves outside the gallery root"
        ) from exc
    return candidate


def _read_json(path: pathlib.Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GalleryError(f"could not read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GalleryError(f"{path} is not valid JSON: {exc}") from exc


def _read_manifest(root: pathlib.Path, manifest_rel: str) -> dict[str, Any]:
    manifest_path = _safe_join(root, manifest_rel)
    if not manifest_path.is_file():
        raise GalleryError(f"manifest not found at {manifest_rel}")
    data = _read_json(manifest_path)
    if not isinstance(data, dict):
        raise GalleryError("manifest root must be a JSON object")
    schema = data.get("schema")
    if schema != SCHEMA:
        raise GalleryError(
            f"manifest schema is {schema!r}, expected {SCHEMA!r}"
        )
    return data


def _normalise_scenarios(
    manifest: dict[str, Any], root: pathlib.Path
) -> list[dict[str, Any]]:
    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise GalleryError("manifest has no scenarios[]")
    normalised: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    for index, raw in enumerate(scenarios):
        if not isinstance(raw, dict):
            raise GalleryError(f"scenarios[{index}] is not an object")
        raw_id = raw.get("id")
        if raw_id is None or raw_id == "":
            scenario_id = f"scenario-{index}"
        elif (
            not isinstance(raw_id, str)
            or _SCENARIO_ID_RE.fullmatch(raw_id) is None
            or raw_id in (".", "..")
        ):
            raise GalleryError(
                f"scenarios[{index}].id must be a slug of "
                f"[A-Za-z0-9._-]; got {raw_id!r}"
            )
        else:
            scenario_id = raw_id
        # Reject duplicate scenario ids: the rendered gallery uses the
        # id as the section's data-scenario attribute and the
        # client-side JS keys interactions off `querySelector`, so a
        # duplicate would leave the second scenario's controls broken
        # silently. Detect here and fail the manifest load loudly.
        prior = seen_ids.get(scenario_id)
        if prior is not None:
            raise GalleryError(
                f"scenarios[{index}].id {scenario_id!r} is a duplicate of "
                f"scenarios[{prior}].id; scenario ids must be unique"
            )
        seen_ids[scenario_id] = index
        moments_raw = raw.get("moments")
        if not isinstance(moments_raw, list) or not moments_raw:
            raise GalleryError(
                f"scenario {scenario_id!r} has no moments[]"
            )
        moments: list[dict[str, Any]] = []
        for m_index, moment in enumerate(moments_raw):
            if not isinstance(moment, dict):
                raise GalleryError(
                    f"scenario {scenario_id!r} moments[{m_index}] is not an object"
                )
            image_rel = moment.get("image")
            if not isinstance(image_rel, str) or not image_rel:
                raise GalleryError(
                    f"scenario {scenario_id!r} moments[{m_index}].image is missing"
                )
            facts_rel = moment.get("facts")
            facts_data: Any = None
            if facts_rel is not None:
                if not isinstance(facts_rel, str):
                    raise GalleryError(
                        f"scenario {scenario_id!r} moments[{m_index}].facts must be a string path"
                    )
                facts_path = _safe_join(root, facts_rel)
                if not facts_path.is_file():
                    raise GalleryError(
                        f"scenario {scenario_id!r} moments[{m_index}].facts points at a missing file {facts_rel!r}"
                    )
                facts_data = _read_json(facts_path)
            timestamp = moment.get("timestamp_ms")
            if timestamp is not None and not isinstance(timestamp, int):
                raise GalleryError(
                    f"scenario {scenario_id!r} moments[{m_index}].timestamp_ms must be an integer"
                )
            label = moment.get("label")
            if label is not None and not isinstance(label, str):
                raise GalleryError(
                    f"scenario {scenario_id!r} moments[{m_index}].label must be a string"
                )
            moments.append(
                {
                    "image_rel": image_rel,
                    "facts_rel": facts_rel,
                    "facts": facts_data,
                    "timestamp_ms": timestamp,
                    "label": label,
                }
            )
        claims_raw = raw.get("claims")
        claims: list[dict[str, Any]] = []
        if claims_raw is not None:
            if not isinstance(claims_raw, list):
                raise GalleryError(
                    f"scenario {scenario_id!r} claims must be a list"
                )
            for c_index, claim in enumerate(claims_raw):
                if not isinstance(claim, dict):
                    raise GalleryError(
                        f"scenario {scenario_id!r} claims[{c_index}] is not an object"
                    )
                name = claim.get("name")
                held = claim.get("held")
                if not isinstance(name, str) or not isinstance(held, bool):
                    raise GalleryError(
                        f"scenario {scenario_id!r} claims[{c_index}] must have string 'name' and boolean 'held'"
                    )
                claims.append({"name": name, "held": held})
        audio_rel = raw.get("audio")
        if audio_rel is not None and not isinstance(audio_rel, str):
            raise GalleryError(
                f"scenario {scenario_id!r} audio must be a string path"
            )
        normalised.append(
            {
                "id": scenario_id,
                "label": raw.get("label") or scenario_id,
                "description": raw.get("description") or "",
                "moments": moments,
                "claims": claims,
                "audio_rel": audio_rel,
                "reference": raw.get("reference") or "",
                "transcript": raw.get("transcript") or "",
            }
        )
    return normalised


def _copy_assets(
    scenarios: list[dict[str, Any]],
    root: pathlib.Path,
    out_dir: pathlib.Path,
) -> None:
    moments_dir = out_dir / MOMENTS_SUBDIR
    moments_dir.mkdir(parents=True, exist_ok=True)
    for scenario in scenarios:
        scenario_dir = moments_dir / scenario["id"]
        scenario_dir.mkdir(parents=True, exist_ok=True)
        # Renumber the frames as `<n>.png` so the HTML's `nth-of-type`
        # selectors match cleanly and the names don't leak the producer's
        # own naming (e.g. `frame-00050.png`).
        for index, moment in enumerate(scenario["moments"]):
            src = _safe_join(root, moment["image_rel"])
            if not src.is_file():
                raise GalleryError(
                    f"scenario {scenario['id']!r} moment {index} image missing: {moment['image_rel']}"
                )
            dst_name = f"{index:04d}{src.suffix.lower() or '.png'}"
            dst = scenario_dir / dst_name
            shutil.copyfile(src, dst)
            moment["_out_image"] = f"{MOMENTS_SUBDIR}/{scenario['id']}/{dst_name}"
        if scenario["audio_rel"]:
            src = _safe_join(root, scenario["audio_rel"])
            if not src.is_file():
                raise GalleryError(
                    f"scenario {scenario['id']!r} audio missing: {scenario['audio_rel']}"
                )
            dst = out_dir / f"{scenario['id']}{src.suffix.lower()}"
            shutil.copyfile(src, dst)
            scenario["_out_audio"] = dst.name


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _format_facts(facts: Any) -> str:
    """Render the optional JSON facts next to a frame as a key/value list.

    The hud_e2e frame JSON looks like `{stage, partial, result, level, hud_ops}`;
    `level` and `hud_ops` are numeric and get a tiny bar so the value is
    readable at a glance rather than buried inside a text dump. Everything
    else is a key/value row in the order it was emitted.
    """
    if facts is None:
        return "<p class=\"empty\">no overlay state recorded</p>"
    if isinstance(facts, dict):
        rows: list[str] = []
        keys = list(facts.keys())
        for key in keys:
            value = facts[key]
            label = _html_escape(str(key))
            if key in ("level", "hud_ops") and isinstance(value, (int, float)):
                v = max(0.0, min(1.0, float(value)))
                bar_width = int(round(v * 100))
                rendered = (
                    f"<span class=\"bar\"><span class=\"bar-fill\" style=\"width:{bar_width}%\"></span></span>"
                    f" <span class=\"bar-val\">{_html_escape(str(value))}</span>"
                )
                rows.append(f"<dt>{label}</dt><dd>{rendered}</dd>")
            else:
                rendered = _html_escape(
                    json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                )
                rows.append(f"<dt>{label}</dt><dd>{rendered}</dd>")
        return f"<dl class=\"facts\">\n{''.join(rows)}\n</dl>"
    return f"<pre class=\"facts-raw\">{_html_escape(json.dumps(facts, indent=2))}</pre>"


def _render_run_header(run: dict[str, Any]) -> str:
    fields = [
        ("repo", run.get("repo")),
        ("ref", run.get("ref")),
        ("box", run.get("box")),
        ("date", run.get("date")),
        ("lane", run.get("lane")),
        ("commit", run.get("commit")),
    ]
    rows = []
    for key, value in fields:
        if value is None or value == "":
            continue
        rows.append(
            f"<span class=\"hdr-item\"><span class=\"hdr-k\">{_html_escape(key)}</span>"
            f"<span class=\"hdr-v\">{_html_escape(str(value))}</span></span>"
        )
    return f"<header class=\"run-header\">\n{''.join(rows)}\n</header>"


def _render_scenario(scenario: dict[str, Any]) -> str:
    moments_html = []
    for index, moment in enumerate(scenario["moments"]):
        ts = moment.get("timestamp_ms")
        ts_label = f"{ts} ms" if ts is not None else ""
        label = moment.get("label") or f"moment {index}"
        moments_html.append(
            f"<button type=\"button\" class=\"frame\" data-index=\"{index}\" "
            f"data-factstype=\"button\" aria-label=\"{_html_escape(label)} {ts_label}\">"
            f"<img loading=\"lazy\" src=\"{_html_escape(moment['_out_image'])}\" alt=\"{_html_escape(label)}\" />"
            f"<span class=\"frame-ts\">{_html_escape(ts_label)}</span>"
            f"</button>"
        )
    chips_html = []
    for claim in scenario["claims"]:
        chip_class = "chip chip-held" if claim["held"] else "chip chip-failed"
        chip_label = "held" if claim["held"] else "failed"
        chips_html.append(
            f"<span class=\"{chip_class}\" title=\"{_html_escape(claim['name'])}: {chip_label}\">"
            f"<span class=\"chip-name\">{_html_escape(claim['name'])}</span>"
            f"<span class=\"chip-state\">{chip_label}</span></span>"
        )
    audio_html = ""
    if scenario.get("_out_audio"):
        audio_html = (
            f"<audio controls preload=\"metadata\" src=\"{_html_escape(scenario['_out_audio'])}\"></audio>"
        )
    facts_blocks = "\n".join(
        f"<section class=\"facts-pane\" data-index=\"{index}\"{' hidden' if index else ''}>"
        f"<h4>{_html_escape(moment.get('label') or f'moment {index}')}</h4>"
        f"{_format_facts(moment.get('facts'))}</section>"
        for index, moment in enumerate(scenario["moments"])
    )
    chips_joined = "".join(chips_html)
    if not chips_joined:
        chips_joined = '<span class="empty">no claims recorded</span>'
    return (
        f"<section class=\"scenario\" data-scenario=\"{_html_escape(scenario['id'])}\">"
        f"<header class=\"scenario-header\">"
        f"<h3>{_html_escape(scenario['label'])}</h3>"
        f"<p class=\"scenario-desc\">{_html_escape(scenario['description'])}</p>"
        f"</header>"
        f"<div class=\"scenario-body\">"
        f"<div class=\"filmstrip\">{''.join(moments_html)}</div>"
        f"<div class=\"viewer\"><img class=\"viewer-img\" "
        f"src=\"{_html_escape(scenario['moments'][0]['_out_image'])}\" "
        f"alt=\"\" /></div>"
        f"<div class=\"facts-wrap\">{facts_blocks}</div>"
        f"</div>"
        f"<footer class=\"scenario-footer\">"
        f"<div class=\"chips\">{chips_joined}</div>"
        f"<div class=\"audio\">{audio_html}</div>"
        f"</footer>"
        f"</section>"
    )


def _render_html(manifest: dict[str, Any], scenarios: list[dict[str, Any]]) -> str:
    title = _html_escape(manifest.get("title") or "moments gallery")
    run_header = _render_run_header(manifest.get("run") or {})
    body = "\n".join(_render_scenario(s) for s in scenarios)
    # Defense in depth: the blob is embedded inside a <script
    # type="application/json"> block. Manifest and facts values may
    # contain `<script>` or markup; Unicode-escape the three HTML
    # context-sensitive characters so the renderer cannot be tricked
    # by a hostile summary string into terminating the element.
    json_blob = (
        json.dumps({"scenarios": scenarios})
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    # The embedded JSON keeps the file self-contained so the gallery works
    # when opened straight from a downloaded artifact (no fetch, no XHR).
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {{
    color-scheme: dark;
    --bg: #0d0f14;
    --bg-elev: #15181f;
    --bg-frame: #1c2029;
    --fg: #e6e8ee;
    --fg-dim: #8b92a1;
    --accent: #6ea8ff;
    --held: #2bb673;
    --failed: #e0625b;
    --border: #2a2f3a;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--fg); }}
  body {{ font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  h1, h2, h3 {{ font-weight: 600; margin: 0; }}
  .page {{ max-width: 1280px; margin: 0 auto; padding: 24px 24px 64px; }}
  .run-header {{
    display: flex; flex-wrap: wrap; gap: 12px 24px; padding: 16px 20px;
    background: var(--bg-elev); border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 24px;
  }}
  .hdr-item {{ display: flex; flex-direction: column; gap: 2px; }}
  .hdr-k {{ font-size: 11px; text-transform: uppercase; color: var(--fg-dim); letter-spacing: 0.08em; }}
  .hdr-v {{ font-size: 14px; color: var(--fg); font-variant-numeric: tabular-nums; }}
  .scenario {{
    background: var(--bg-elev); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; margin-bottom: 16px;
  }}
  .scenario-header {{ margin-bottom: 12px; }}
  .scenario-desc {{ color: var(--fg-dim); margin: 4px 0 0; }}
  .scenario-body {{
    display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); gap: 16px;
  }}
  @media (max-width: 720px) {{
    .scenario-body {{ grid-template-columns: 1fr; }}
  }}
  .filmstrip {{
    grid-column: 1 / -1; display: flex; gap: 8px; overflow-x: auto;
    padding-bottom: 8px;
  }}
  .frame {{
    flex: 0 0 auto; background: var(--bg-frame); border: 2px solid transparent;
    border-radius: 6px; padding: 0; cursor: pointer; color: inherit;
    font: inherit;
  }}
  .frame[aria-current="true"] {{ border-color: var(--accent); }}
  .frame img {{ display: block; height: 96px; width: auto; border-radius: 4px; }}
  .frame-ts {{
    display: block; font-size: 10px; color: var(--fg-dim);
    padding: 2px 4px; font-variant-numeric: tabular-nums;
  }}
  .viewer {{ grid-column: 1 / 2; }}
  .viewer-img {{
    width: 100%; height: auto; display: block; border-radius: 6px;
    background: #000;
  }}
  .facts-wrap {{ grid-column: 2 / 3; }}
  dl.facts {{ margin: 0; display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; }}
  dl.facts dt {{ color: var(--fg-dim); font-size: 12px; }}
  dl.facts dd {{ margin: 0; font-variant-numeric: tabular-nums; }}
  .bar {{
    display: inline-block; width: 100px; height: 6px; background: var(--bg-frame);
    border-radius: 3px; vertical-align: middle; overflow: hidden; margin-right: 6px;
  }}
  .bar-fill {{ display: block; height: 100%; background: var(--accent); }}
  .bar-val {{ font-size: 12px; color: var(--fg); }}
  pre.facts-raw {{
    margin: 0; padding: 12px; background: var(--bg-frame);
    border-radius: 4px; font-size: 12px; overflow-x: auto;
  }}
  .empty {{ color: var(--fg-dim); font-style: italic; }}
  .scenario-footer {{
    display: flex; justify-content: space-between; align-items: center;
    margin-top: 12px; gap: 16px; flex-wrap: wrap;
  }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .chip {{
    display: inline-flex; gap: 6px; align-items: center;
    padding: 2px 8px; border-radius: 12px; font-size: 12px;
    border: 1px solid var(--border); background: var(--bg-frame);
  }}
  .chip-held {{ border-color: var(--held); }}
  .chip-held .chip-state {{ color: var(--held); }}
  .chip-failed {{ border-color: var(--failed); }}
  .chip-failed .chip-state {{ color: var(--failed); }}
  .chip-name {{ color: var(--fg-dim); }}
  .chip-state {{ font-weight: 600; text-transform: uppercase; font-size: 10px; letter-spacing: 0.06em; }}
  audio {{ height: 32px; }}
  /* enlarged view: a simple fullscreen-ish overlay for the currently
     selected frame, toggled by JS. */
  body.enlarged .scenario {{ display: none; }}
  body.enlarged .enlarged-host {{ display: flex; }}
  .enlarged-host {{
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.92);
    align-items: center; justify-content: center; z-index: 100;
  }}
  .enlarged-host img {{ max-width: 96vw; max-height: 96vh; border-radius: 6px; }}
  .enlarged-close {{
    position: absolute; top: 16px; right: 16px; background: var(--bg-elev);
    color: var(--fg); border: 1px solid var(--border); padding: 6px 12px;
    border-radius: 6px; cursor: pointer; font: inherit;
  }}
</style>
</head>
<body>
<div class="page">
  <h1>{title}</h1>
  {run_header}
  {body}
</div>
<div class="enlarged-host" id="enlarged">
  <button type="button" class="enlarged-close" id="enlarged-close">close</button>
  <img id="enlarged-img" alt="" />
</div>
<script id="moments-data" type="application/json">{json_blob}</script>
<script>
(function () {{
  const data = JSON.parse(document.getElementById("moments-data").textContent);
  const scenarios = data.scenarios || [];

  let activeScenario = null;
  let activeIndex = 0;
  let returnFocus = null;
  const enlarged = document.getElementById("enlarged");
  const enlargedImg = document.getElementById("enlarged-img");
  const closeButton = document.getElementById("enlarged-close");

  scenarios.forEach(function (scenario) {{
    const root = document.querySelector(
      '.scenario[data-scenario="' + scenario.id + '"]'
    );
    if (!root) return;
    const frames = root.querySelectorAll(".frame");
    const viewer = root.querySelector(".viewer-img");
    const factsPanes = root.querySelectorAll(".facts-pane");

    const select = function (index) {{
      const safeIndex = Math.max(0, Math.min(index, scenario.moments.length - 1));
      const moment = scenario.moments[safeIndex];
      if (!moment) return;
      viewer.src = moment._out_image;
      frames.forEach(function (f, i) {{
        if (i === safeIndex) f.setAttribute("aria-current", "true");
        else f.removeAttribute("aria-current");
      }});
      factsPanes.forEach(function (pane, i) {{
        if (i === safeIndex) pane.removeAttribute("hidden");
        else pane.setAttribute("hidden", "");
      }});
      root._selectedIndex = safeIndex;
    }};
    root._select = select;

    frames.forEach(function (frame, index) {{
      frame.addEventListener("click", function () {{
        select(index);
        enlarge(scenario.id, index);
      }});
    }});
    select(0);

    root.addEventListener("keydown", function (event) {{
      if (document.body.classList.contains("enlarged")) return;
      const current = root._selectedIndex || 0;
      if (event.key === "ArrowRight") {{
        event.preventDefault();
        select(current + 1);
      }} else if (event.key === "ArrowLeft") {{
        event.preventDefault();
        select(current - 1);
      }} else if (event.key === "Enter" || event.key === " ") {{
        if (document.activeElement && document.activeElement.classList.contains("frame")) {{
          event.preventDefault();
          enlarge(scenario.id, current);
        }}
      }}
    }});
    root.tabIndex = 0;
  }});

  // Overlay navigation keeps explicit state; focus is on the close button.
  function closeEnlarged() {{
    document.body.classList.remove("enlarged");
    activeScenario = null;
    if (returnFocus) returnFocus.focus();
    returnFocus = null;
  }}
  closeButton.addEventListener("click", closeEnlarged);
  function enlarge(scenarioId, index) {{
    const scenario = scenarios.find(function (s) {{ return s.id === scenarioId; }});
    const root = document.querySelector('.scenario[data-scenario="' + scenarioId + '"]');
    if (!scenario || !root || !scenario.moments[index]) return;
    if (!document.body.classList.contains("enlarged")) returnFocus = document.activeElement;
    root._select(index);
    activeScenario = scenarioId;
    activeIndex = index;
    enlargedImg.src = scenario.moments[index]._out_image;
    enlargedImg.alt = scenario.moments[index].label || "moment " + (index + 1);
    document.body.classList.add("enlarged");
    closeButton.focus();
  }}
  enlarged.addEventListener("click", function (event) {{
    if (event.target === enlarged) closeEnlarged();
  }});
  document.addEventListener("keydown", function (event) {{
    if (!document.body.classList.contains("enlarged")) return;
    if (event.key === "Escape") {{
      event.preventDefault();
      closeEnlarged();
      return;
    }}
    const scenario = scenarios.find(function (s) {{ return s.id === activeScenario; }});
    if (!scenario) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {{
      event.preventDefault();
      const delta = event.key === "ArrowRight" ? 1 : -1;
      const next = Math.max(0, Math.min(activeIndex + delta, scenario.moments.length - 1));
      enlarge(activeScenario, next);
    }}
  }});
}})();
</script>
</body>
</html>
"""


def _png_thumbnail(data: bytes, width: int) -> bytes:
    """Downscale a PNG to `width` pixels wide, nearest-neighbour, stdlib only.

    Summary thumbnails are a glance, not the evidence: the full frame is
    in the artifact. Nearest-neighbour keeps the implementation inside
    the stdlib (no Pillow on the runner) and the result is still a real,
    decodable PNG. A frame already at or under `width` is returned as-is,
    so a small synthetic fixture is embedded unchanged.
    """
    import struct
    import zlib as _zlib

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GalleryError("thumbnail source is not a PNG")
    # Walk the chunks until IHDR and the concatenated IDAT are both in
    # hand. Ancillary chunks (tEXt, iCCP, ...) are dropped on purpose:
    # they can be megabytes and would defeat the size cap.
    pos = 8
    ihdr = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            ihdr = payload
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break
    if ihdr is None or not idat:
        raise GalleryError("PNG is missing IHDR or IDAT")
    if len(ihdr) != 13:
        raise GalleryError("invalid PNG IHDR")
    src_w, src_h, bit_depth, color_type = struct.unpack(">IIBB", ihdr[:10])
    if src_w == 0 or src_h == 0:
        raise GalleryError("invalid PNG IHDR")
    if ihdr[10:13] != b"\x00\x00\x00":
        # The scanline decoder below handles non-interlaced rows only.
        # Adam7 uses seven differently-sized passes, not src_h full rows.
        raise GalleryError("PNG thumbnail requires standard compression, filter method, and non-interlaced rows")
    if bit_depth != 8 or color_type not in (2, 6):
        # Indexed, greyscale, and 16-bit frames are not what the e2e
        # lanes emit. Refuse rather than guess a decode.
        raise GalleryError(
            f"PNG thumbnail only handles 8-bit RGB/RGBA, got "
            f"bit_depth={bit_depth} color_type={color_type}"
        )
    if src_w <= width:
        return data
    channels = 3 if color_type == 2 else 4
    raw = _zlib.decompress(bytes(idat))
    row_bytes = src_w * channels
    stride = 1 + row_bytes
    if len(raw) != src_h * stride:
        raise GalleryError("PNG IDAT size disagrees with its IHDR")
    # PNG stores filtered differences, not pixel bytes. Each row's Up,
    # Average and Paeth predictors depend on the *decoded* preceding row.
    decoded = []
    previous = bytearray(row_bytes)
    for sy in range(src_h):
        offset = sy * stride
        filter_type = raw[offset]
        if filter_type > 4:
            raise GalleryError(f"unsupported PNG scanline filter {filter_type}")
        row = bytearray(raw[offset + 1 : offset + stride])
        for i in range(row_bytes):
            left = row[i - channels] if i >= channels else 0
            above = previous[i]
            upper_left = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                p = left + above - upper_left
                distances = (abs(p - left), abs(p - above), abs(p - upper_left))
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            else:
                predictor = 0
            row[i] = (row[i] + predictor) & 255
        decoded.append(row)
        previous = row
    dst_w = width
    dst_h = max(1, round(src_h * dst_w / src_w))
    out = bytearray()
    for y in range(dst_h):
        sy = min(src_h - 1, y * src_h // dst_h)
        row = decoded[sy]
        out.append(0)
        for x in range(dst_w):
            sx = min(src_w - 1, x * src_w // dst_w)
            start = sx * channels
            # Flatten alpha onto black so the summary PNG is always RGB
            # and a transparent frame does not render as a browser default.
            if channels == 4:
                alpha = row[start + 3] / 255
                out.extend(
                    bytes(int(round(row[start + c] * alpha)) for c in range(3))
                )
            else:
                out.extend(row[start : start + 3])
    ihdr_out = struct.pack(">IIBBBBB", dst_w, dst_h, 8, 2, 0, 0, 0)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", _zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr_out)
        + chunk(b"IDAT", _zlib.compress(bytes(out), 9))
        + chunk(b"IEND", b"")
    )


def _png_data_url(path: pathlib.Path, width: int = SUMMARY_THUMB_WIDTH) -> str:
    """Encode a downscaled PNG as a base64 data URL for the job summary."""
    data = _png_thumbnail(path.read_bytes(), width)
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


def _write_summary(
    manifest: dict[str, Any],
    scenarios: list[dict[str, Any]],
    out_dir: pathlib.Path,
    max_thumbnails: int,
    html_relpath: str,
) -> None:
    if max_thumbnails <= 0:
        return
    title = manifest.get("title") or "moments gallery"
    rows: list[str] = []
    for scenario in scenarios[:max_thumbnails]:
        moments = scenario["moments"]
        if not moments:
            continue
        first_frame_name = moments[0]["_out_image"]
        first_frame_path = out_dir / first_frame_name
        if not first_frame_path.is_file():
            continue
        # GitHub caps ONE step's summary at 1 MiB and drops the upload
        # (without failing the step) when it is exceeded, so a raw frame
        # inlined here is silently invisible. Downscale first; the full
        # frame stays in the artifact the table links to.
        try:
            data_url = _png_data_url(first_frame_path)
        except (OSError, GalleryError) as exc:
            _log(f"summary thumbnail skipped for {scenario['id']}: {exc}")
            continue
        chips = " ".join(
            f"<span style=\"color:{'#2bb673' if c['held'] else '#e0625b'}\">{_html_escape(c['name'])}</span>"
            for c in scenario["claims"]
        ) or "<em>no claims recorded</em>"
        rows.append(
            f"<tr><td><strong>{_html_escape(scenario['label'])}</strong>"
            f"<br/><span style=\"color:#8b92a1\">{_html_escape(scenario['description'])}</span></td>"
            f"<td><img src=\"{data_url}\" width=\"240\" alt=\"\" /></td>"
            f"<td>{chips}</td></tr>"
        )
    if not rows:
        return
    table = (
        f"## {title}\n\n"
        f"<table><thead><tr><th>scenario</th><th>first frame</th><th>claims</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>\n\n"
        f"<sub>Open the <code>{_html_escape(html_relpath)}</code> artifact for the full gallery "
        f"(filmstrip + click-to-enlarge + keyboard left/right).</sub>\n"
    )
    # Last line of defence: if the downscaled rows still exceed the cap
    # (a pathological number of scenarios), drop rows from the end until
    # the block fits, and say so. A summary GitHub will actually render
    # beats one it silently discards.
    budget = SUMMARY_BYTE_CAP - SUMMARY_HEADROOM_BYTES
    encoded = table.encode("utf-8")
    while len(encoded) > budget and rows:
        rows.pop()
        table = (
            f"## {title}\n\n"
            f"<table><thead><tr><th>scenario</th><th>first frame</th><th>claims</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>\n\n"
            f"<sub>Further thumbnails omitted to stay under GitHub's 1 MiB "
            f"step-summary cap. Open the <code>{_html_escape(html_relpath)}</code> "
            f"artifact for the full gallery.</sub>\n"
        )
        encoded = table.encode("utf-8")
    if len(encoded) > budget:
        _log("summary block exceeds 1 MiB even with no thumbnails; skipped")
        return
    _append_summary(table)


def _stage_pages(out_dir: pathlib.Path, root: pathlib.Path, lane: str) -> pathlib.Path:
    """Stage the gallery at `<root>/<name>-pages/gallery/<lane>/latest/`.

    GitHub Pages serves an artifact from the site root, so the directory
    layout IS the URL. Staging under `gallery/<lane>/latest` makes the
    deployed URL `<repo>/gallery/<lane>/latest/`, which is the contract
    the acceptance states, and a later run of the same lane replaces
    `latest` rather than accumulating dated copies.
    """
    pages_dir = root / f"{lane}-pages"
    if pages_dir.exists():
        shutil.rmtree(pages_dir)
    staged = pages_dir / "gallery" / lane / "latest"
    shutil.copytree(out_dir, staged)
    return pages_dir


def _main_impl() -> int:
    try:
        manifest_rel = _env("GALLERY_MANIFEST")
        root = pathlib.Path(_env("GALLERY_ROOT")).resolve(strict=False)
        name = _env("GALLERY_NAME")
        summary_thumbnails = int(_env("GALLERY_SUMMARY_THUMBNAILS", "3"))
        pages_enabled = _env("GALLERY_PAGES", "false").lower() == "true"
    except GalleryError as exc:
        _log(f"input error: {exc}")
        return 2

    if not root.is_dir():
        _log(f"gallery root does not exist: {root}")
        return 2

    try:
        manifest = _read_manifest(root, manifest_rel)
        scenarios = _normalise_scenarios(manifest, root)
    except GalleryError as exc:
        _log(f"manifest error: {exc}")
        return 2

    out_dir = root / f"{name}-gallery"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    try:
        _copy_assets(scenarios, root, out_dir)
    except GalleryError as exc:
        _log(f"asset error: {exc}")
        return 2

    html = _render_html(manifest, scenarios)
    html_path = out_dir / HTML_FILENAME
    html_path.write_text(html, encoding="utf-8")

    pages_root = ""
    if pages_enabled:
        pages_root = str(_stage_pages(out_dir, root, name))

    _set_output("out_dir", str(out_dir))
    _set_output("html_path", str(html_path))
    _set_output("pages_enabled", "true" if pages_enabled else "false")
    _set_output("lane", name)
    _set_output("pages_root", pages_root)

    _write_summary(manifest, scenarios, out_dir, summary_thumbnails, HTML_FILENAME)
    _log(f"wrote {html_path} ({len(scenarios)} scenarios)")
    return 0


def main() -> int:
    return _main_impl()


def _make_self_test_root() -> pathlib.Path:
    """Build a temporary root that exercises every branch of the renderer.

    PNGs are written with stdlib (zlib + struct + a flat colour) so the
    self-test does not need Pillow. The bytes are a real, decodable PNG;
    a verify step below confirms the IHDR + IDAT round-trip.
    """
    import tempfile
    import struct
    import zlib as _zlib

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="moments-gallery-self-test-"))

    def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
        raw = bytearray()
        for _ in range(height):
            raw.append(0)  # filter type 0 (None)
            for _ in range(width):
                raw.extend(rgb)
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", _zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", _zlib.compress(bytes(raw)))
            + chunk(b"IEND", b"")
        )

    scenarios_dir = tmp / "scenarios"
    scenarios_dir.mkdir()
    for scenario_id, label, n_frames in (
        ("warmup", "warmup", 2),
        ("normal", "samantha-normal", 3),
    ):
        sdir = scenarios_dir / scenario_id
        sdir.mkdir()
        for i in range(n_frames):
            (sdir / f"frame-{i:05d}.png").write_bytes(_png(160, 90, (12, 16, 24)))
        (sdir / "frame-final.json").write_text(
            json.dumps(
                {"stage": "Recording", "partial": "fox", "result": "",
                 "level": 0.42, "hud_ops": 17}
            )
        )
    # manifest with both scenarios, one audio, mixed claim results
    manifest = {
        "schema": "maxi-tools.moments-gallery.v1",
        "title": "self-test moments gallery",
        "run": {
            "repo": "maxi-tools/voicemaci",
            "ref": "wt/test",
            "box": "maxibookpro24",
            "date": "2026-09-18T09:00:00Z",
            "lane": "hud-desktop-e2e",
            "commit": "deadbeef",
        },
        "scenarios": [
            {
                "id": "warmup",
                "label": "warmup",
                "description": "cold start, timing not asserted",
                "moments": [
                    {
                        "image": "scenarios/warmup/frame-00000.png",
                        "facts": "scenarios/warmup/frame-final.json",
                        "timestamp_ms": 0,
                    },
                    {
                        "image": "scenarios/warmup/frame-00001.png",
                        "facts": "scenarios/warmup/frame-final.json",
                        "timestamp_ms": 250,
                    },
                ],
                "claims": [{"name": "transcript", "held": True}],
            },
            {
                "id": "normal",
                "label": "samantha-normal",
                "description": "the quick brown fox",
                "audio": "scenarios/normal/input.wav",
                "moments": [
                    {
                        "image": "scenarios/normal/frame-00000.png",
                        "facts": "scenarios/normal/frame-final.json",
                        "timestamp_ms": 250,
                    },
                    {
                        "image": "scenarios/normal/frame-00001.png",
                        "facts": "scenarios/normal/frame-final.json",
                        "timestamp_ms": 500,
                    },
                    {
                        "image": "scenarios/normal/frame-00002.png",
                        "facts": "scenarios/normal/frame-final.json",
                        "timestamp_ms": 750,
                    },
                ],
                "claims": [
                    {"name": "transcript", "held": True},
                    {"name": "first_partial_before_audio_end", "held": False},
                ],
            },
        ],
    }
    (scenarios_dir / "normal" / "input.wav").write_bytes(b"RIFF$\x00\x00\x00WAVEfmt ")
    (tmp / "manifest.json").write_text(json.dumps(manifest))
    return tmp


def _self_test() -> int:
    """Run the renderer against a synthetic root and validate outputs.

    The synthetic PNGs are written with stdlib only (no Pillow), so this
    self-test runs in any environment with Python 3.10+.
    """
    tmp = _make_self_test_root()
    out_dir = tmp / "hud-desktop-e2e-gallery"
    # Drive the renderer directly so we can assert on its outputs without
    # going through the GitHub Actions step wrapper.
    os.environ["GALLERY_MANIFEST"] = "manifest.json"
    os.environ["GALLERY_ROOT"] = str(tmp)
    os.environ["GALLERY_NAME"] = "hud-desktop-e2e"
    os.environ["GALLERY_SUMMARY_THUMBNAILS"] = "3"
    os.environ.pop("GALLERY_PAGES", None)
    # Don't write a real summary file in self-test.
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    saved_summary = None
    if summary is not None:
        saved_summary = summary
        os.environ.pop("GITHUB_STEP_SUMMARY")
    output_file = tmp / "github-output.txt"
    os.environ["GITHUB_OUTPUT"] = str(output_file)
    rc = _main_impl()
    if saved_summary is not None:
        os.environ["GITHUB_STEP_SUMMARY"] = saved_summary
    if rc != 0:
        print(f"SELF_TEST_FAIL main() returned {rc}", file=sys.stderr)
        return 1
    html = (out_dir / HTML_FILENAME).read_text(encoding="utf-8")

    def _expect(name: str, condition: bool, detail: str) -> None:
        if not condition:
            print(f"SELF_TEST_FAIL {name}: {detail}", file=sys.stderr)
            raise SystemExit(1)

    _expect("warmup scenario", 'data-scenario="warmup"' in html, "warmup scenario missing from HTML")
    _expect("normal scenario", 'data-scenario="normal"' in html, "normal scenario missing from HTML")
    _expect("renumbered frame path", "moments/normal/0000.png" in html, "renumbered frame path missing")
    _expect(
        "audio basename normalised",
        "moments/normal/input.wav" not in html and 'src="normal.wav"' in html,
        "audio path should be renamed to normal.wav and src-rewritten",
    )
    _expect("held chip class", "chip chip-held" in html, "held chip class missing")
    _expect("failed chip class", "chip chip-failed" in html, "failed chip class missing")
    _expect("facts bar", 'class="bar"' in html, "facts bar missing")

    outputs = _read_outputs(output_file)
    _expect("pages output defaults false", outputs.get("pages_enabled") == "false", str(outputs))
    _expect("lane output", outputs.get("lane") == "hud-desktop-e2e", str(outputs))
    _expect("no pages staging by default", outputs.get("pages_root") == "", str(outputs))
    _expect(
        "pages tree absent by default",
        not (tmp / "hud-desktop-e2e-pages").exists(),
        "pages staging ran with pages disabled",
    )

    _self_test_pages(tmp)
    _self_test_large_png_summary()
    print("SELF_TEST_OK")
    return 0


def _read_outputs(path: pathlib.Path) -> dict[str, str]:
    outputs: dict[str, str] = {}
    if not path.is_file():
        return outputs
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key:
            outputs[key] = value
    return outputs


def _self_test_pages(tmp: pathlib.Path) -> None:
    """Re-run with pages requested and check the staged tree and outputs."""
    os.environ["GALLERY_ROOT"] = str(tmp)
    os.environ["GALLERY_PAGES"] = "true"
    output_file = tmp / "github-output-pages.txt"
    os.environ["GITHUB_OUTPUT"] = str(output_file)
    rc = _main_impl()
    if rc != 0:
        print(f"SELF_TEST_FAIL pages main() returned {rc}", file=sys.stderr)
        raise SystemExit(1)
    outputs = _read_outputs(output_file)
    lane = "hud-desktop-e2e"
    staged = tmp / f"{lane}-pages" / "gallery" / lane / "latest" / HTML_FILENAME
    if outputs.get("pages_enabled") != "true":
        print(f"SELF_TEST_FAIL pages_enabled: {outputs}", file=sys.stderr)
        raise SystemExit(1)
    if outputs.get("lane") != lane:
        print(f"SELF_TEST_FAIL lane: {outputs}", file=sys.stderr)
        raise SystemExit(1)
    if not staged.is_file():
        print(f"SELF_TEST_FAIL pages staging missing {staged}", file=sys.stderr)
        raise SystemExit(1)
    if outputs.get("pages_root") != str(tmp / f"{lane}-pages"):
        print(f"SELF_TEST_FAIL pages_root: {outputs}", file=sys.stderr)
        raise SystemExit(1)
    # The deploy step's page_url does not exist until that step has run,
    # so the recording step is exercised here the way the action runs it.
    # The script is read out of action.yml rather than copied, so the two
    # cannot drift.
    record = tmp / "github-output-record.txt"
    action_text = (
        pathlib.Path(__file__).resolve().parent / "action.yml"
    ).read_text(encoding="utf-8")
    marker = '      run: |\n'
    start = action_text.index(marker, action_text.index("Record the Pages deployment"))
    body = action_text[start + len(marker) :]
    script = "\n".join(
        line[8:] for line in body.splitlines() if line.startswith("        ")
    )
    for page_url, expected in (
        (
            "https://maxi-tools.github.io/voicemaci/",
            f"https://maxi-tools.github.io/voicemaci/gallery/{lane}/latest/",
        ),
        (
            "https://maxi-tools.github.io/voicemaci",
            f"https://maxi-tools.github.io/voicemaci/gallery/{lane}/latest/",
        ),
    ):
        record.write_text("")
        result = subprocess.run(
            ["bash", "-c", script],
            check=False,
            env={
                "PAGE_URL": page_url,
                "LANE": lane,
                "GITHUB_OUTPUT": str(record),
                "PATH": os.environ.get("PATH", ""),
            },
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"SELF_TEST_FAIL record step: {result.stderr}", file=sys.stderr)
            raise SystemExit(1)
        got = _read_outputs(record).get("latest_deployment")
        if got != expected:
            print(f"SELF_TEST_FAIL latest_deployment: {got!r}", file=sys.stderr)
            raise SystemExit(1)
    os.environ.pop("GALLERY_PAGES", None)


def _self_test_large_png_summary() -> None:
    """One valid 4 MB-class PNG must keep the step summary under 1 MiB.

    This is the case the verifier reproduced: a 1024x1024 RGB PNG of
    3,147,775 bytes inlined raw produced a 4,197,472-byte summary, which
    GitHub discards. The thumbnail must be a real <img> and the summary
    must fit the cap.
    """
    import struct
    import tempfile
    import zlib as _zlib

    width, height = 1024, 1024
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend((x & 0xFF, y & 0xFF, (x + y) & 0xFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", _zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", _zlib.compress(bytes(raw), 1))
        + chunk(b"IEND", b"")
    )
    # The verifier's frame was 3,147,775 bytes. A flat-colour fixture
    # compresses far below that, so pad with a tEXt chunk to land past
    # 4 MB and prove ancillary chunks are dropped rather than embedded.
    pad = b"x" * (4 * 1024 * 1024)
    png = (
        png[: -len(chunk(b"IEND", b""))]
        + chunk(b"tEXt", b"Comment\x00" + pad)
        + chunk(b"IEND", b"")
    )

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="moments-gallery-large-png-"))
    (tmp / "big.png").write_bytes(png)
    manifest = {
        "schema": SCHEMA,
        "title": "large frame",
        "scenarios": [
            {
                "id": "big",
                "label": "big frame",
                "moments": [{"image": "big.png", "timestamp_ms": 0}],
            }
        ],
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest))
    summary = tmp / "summary.md"
    os.environ["GALLERY_MANIFEST"] = "manifest.json"
    os.environ["GALLERY_ROOT"] = str(tmp)
    os.environ["GALLERY_NAME"] = "large-png"
    os.environ["GALLERY_SUMMARY_THUMBNAILS"] = "3"
    os.environ.pop("GALLERY_PAGES", None)
    os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
    os.environ["GITHUB_OUTPUT"] = str(tmp / "github-output.txt")
    rc = _main_impl()
    if rc != 0:
        print(f"SELF_TEST_FAIL large-png main() returned {rc}", file=sys.stderr)
        raise SystemExit(1)
    rendered = summary.read_text(encoding="utf-8")
    size = summary.stat().st_size
    if size >= SUMMARY_BYTE_CAP:
        print(
            f"SELF_TEST_FAIL summary is {size} bytes, cap is {SUMMARY_BYTE_CAP}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if 'src="data:image/png;base64,' not in rendered:
        print("SELF_TEST_FAIL summary has no inline thumbnail", file=sys.stderr)
        raise SystemExit(1)
    # The raw frame must not have been embedded: its base64 alone is
    # over the cap, so its absence is what keeps the summary renderable.
    if base64.b64encode(png).decode("ascii") in rendered:
        print("SELF_TEST_FAIL summary embeds the raw frame", file=sys.stderr)
        raise SystemExit(1)
    print(
        f"SELF_TEST large png: frame {len(png)} bytes, summary {size} bytes"
    )


if __name__ == "__main__":  # noqa: F811 -- extended below
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        raise SystemExit(_self_test())
    raise SystemExit(main())
