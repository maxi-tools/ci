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

    out_dir   absolute path of the gallery output directory
    html_path absolute path of the rendered `index.html`

Side effects:

    * `<root>/<name>-gallery/` is created and populated with `index.html`,
      `moments/<scenario>/<n>.png`, and `<scenario>.wav` for any scenario
      that names an `audio` file.
    * The first `GALLERY_SUMMARY_THUMBNAILS` scenarios' first frame are
      embedded as base64 `<img src="data:...">` rows appended to the job
      summary, so a reader who never opens the artifact still sees what
      the run looked like at a glance.

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
import sys
from typing import Any

SCHEMA = "maxi-tools.moments-gallery.v1"

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
        f"<section class=\"facts-pane\" data-index=\"{index}\" hidden>"
        f"<h4>{_html_escape(moment.get('label') or f'moment {index}')}</h4>"
        f"{_format_facts(moment.get('facts'))}</section>"
        for index, moment in enumerate(scenario["moments"])
    )
    # The first frame's facts pane is shown by default; the JS toggles
    # `hidden` when a different frame is selected.
    first_facts_unhide = ""
    if scenario["moments"]:
        first_facts_unhide = (
            f"<script>document.currentScript && "
            f"(document.currentScript.previousElementSibling || "
            f"document.currentScript.parentElement).querySelector("
            f"'.facts-pane[data-index=\"0\"]').removeAttribute('hidden');</script>"
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
        f"<div class=\"facts-wrap\">{facts_blocks}{first_facts_unhide}</div>"
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
      }});
      frame.addEventListener("dblclick", function () {{
        enlarge(scenario.id, index);
      }});
    }});
    select(0);

    root.addEventListener("keydown", function (event) {{
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

  // Enlarged view: a single overlay reused across scenarios, addressed by
  // (scenario_id, moment_index). Click on a frame, double-click again to
  // leave.
  const enlarged = document.getElementById("enlarged");
  const enlargedImg = document.getElementById("enlarged-img");
  document.getElementById("enlarged-close").addEventListener("click", function () {{
    document.body.classList.remove("enlarged");
  }});
  function enlarge(scenarioId, index) {{
    const root = document.querySelector(
      '.scenario[data-scenario="' + scenarioId + '"]'
    );
    if (!root) return;
    const moment = root._selectedIndex !== undefined
      ? scenarios.find(function (s) {{ return s.id === scenarioId; }}).moments[root._selectedIndex]
      : null;
    if (!moment) return;
    enlargedImg.src = moment._out_image;
    document.body.classList.add("enlarged");
  }}
  enlarged.addEventListener("click", function (event) {{
    if (event.target === enlarged) document.body.classList.remove("enlarged");
  }});
  document.addEventListener("keydown", function (event) {{
    if (event.key === "Escape") document.body.classList.remove("enlarged");
    if (!document.body.classList.contains("enlarged")) return;
    const active = document.activeElement;
    const root = active && active.closest
      ? active.closest(".scenario")
      : null;
    if (!root) return;
    const scenarioId = root.getAttribute("data-scenario");
    const current = root._selectedIndex || 0;
    if (event.key === "ArrowRight") {{
      event.preventDefault();
      const next = Math.min(current + 1, (root.querySelectorAll(".frame").length || 1) - 1);
      root._select(next);
      enlarge(scenarioId, next);
    }} else if (event.key === "ArrowLeft") {{
      event.preventDefault();
      const prev = Math.max(current - 1, 0);
      root._select(prev);
      enlarge(scenarioId, prev);
    }}
  }});
}})();
</script>
</body>
</html>
"""


def _png_data_url(path: pathlib.Path) -> str:
    """Encode a PNG as a base64 data URL for inline summary embedding."""
    data = path.read_bytes()
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
        # Thumbnail size: GitHub caps job-summary body size at 20 MiB.
        # PNG bytes are encoded inline; a 480px wide frame at ~30 KB
        # comfortably fits well within the limit even with a dozen rows.
        try:
            data_url = _png_data_url(first_frame_path)
        except OSError:
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
    _append_summary(table)


def _main_impl() -> int:
    try:
        manifest_rel = _env("GALLERY_MANIFEST")
        root = pathlib.Path(_env("GALLERY_ROOT")).resolve(strict=False)
        name = _env("GALLERY_NAME")
        summary_thumbnails = int(_env("GALLERY_SUMMARY_THUMBNAILS", "3"))
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

    _set_output("out_dir", str(out_dir))
    _set_output("html_path", str(html_path))

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
    # Don't write a real summary file in self-test.
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    saved_summary = None
    if summary is not None:
        saved_summary = summary
        os.environ.pop("GITHUB_STEP_SUMMARY")
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
    print("SELF_TEST_OK")
    return 0


if __name__ == "__main__":  # noqa: F811 -- extended below
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        raise SystemExit(_self_test())
    raise SystemExit(main())
