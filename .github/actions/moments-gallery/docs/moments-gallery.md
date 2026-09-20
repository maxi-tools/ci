# Moments gallery

A single composite action that turns a versioned manifest + a directory of
PNG frames into a self-contained HTML gallery a person can open from a
downloaded artifact or browse on GitHub Pages.

This document defines the manifest schema, gives an example, and lays out
the upgrade path from each of the existing per-repo galleries.

## Why this is a new schema, not an extension of `maxi-ui.waterui-gallery.v1`

The WaterUI gallery schema is a *renderer-test* contract. Its fields
(`renderer`, `freya_dependency`, `expected_sections`, `capture_receipts`,
`interaction_traces`, `waterui_apple_rev`) describe the surface area of
the WaterUI shell and the verification receipts that the lane checks.
Nothing about it expresses a scenario, a per-scenario claim, a frame as
an ordered filmstrip, or an audio track -- and bolting those onto the
WaterUI schema would force every existing consumer of it to take a
position on moments.

The moments schema is a *test-result-timeline* contract. A manifest is a
run header, an ordered list of scenarios, and per scenario an ordered
list of moments with an image, an optional JSON facts file, a
millisecond timestamp, and a list of held/failed claims; the schema also
carries an optional audio file per scenario.

They share the same versioning convention (`maxi-tools.<thing>.vN`),
because both are produced and consumed by infrastructure this
repository hosts; the schemas diverge on what they describe, and that
is the right reason to keep them separate.

## Schema: `maxi-tools.moments-gallery.v1`

A single JSON object. Path types are always relative to the gallery
root (`inputs.root`) and are checked for escaping before any read or
copy.

```jsonc
{
  "schema": "maxi-tools.moments-gallery.v1",
  "title": "HUD desktop e2e — 2026-09-18", // optional; defaults to "moments gallery"

  // Run header. Every field is optional. The HTML renders the
  // well-known keys (`repo`, `ref`, `box`, `date`, `lane`, `commit`)
  // in a fixed order and ignores anything else.
  "run": {
    "repo": "maxi-tools/voicemaci",
    "ref": "wt/moments-gallery-survey",
    "box": "maxibookpro24",
    "date": "2026-09-18T09:00:00Z",
    "lane": "hud-desktop-e2e",
    "commit": "abc1234"
  },

  // Ordered list of scenarios. The order here is the order the gallery
  // renders them in.
  "scenarios": [
    {
      "id": "warmup",                       // optional; falls back to scenario-<index>. Must be a slug of [A-Za-z0-9._-] when set, so the value is safe to embed in a path.
      "label": "warmup",                     // optional; defaults to id
      "description": "cold start; timing not asserted",

      // The audio played during the scenario. Optional. The file is
      // copied into the gallery output and renamed `<id>.<ext>`.
      "audio": "scenarios/warmup/input.wav",

      // Per-scenario claims. Each is rendered as a green or red chip
      // below the filmstrip.
      "claims": [
        { "name": "transcript", "held": true }
      ],

      // Ordered list of moments. The order is the order they appear in
      // the filmstrip.
      "moments": [
        {
          "image": "scenarios/warmup/frame-00000.png",
          "facts": "scenarios/warmup/frame-00000.json",
          "timestamp_ms": 0,
          "label": "start"
        },
        {
          "image": "scenarios/warmup/frame-00250.png",
          "facts": "scenarios/warmup/frame-00250.json",
          "timestamp_ms": 250
        }
      ]
    }
  ]
}
```

### Field reference

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema` | string | yes | Must be `"maxi-tools.moments-gallery.v1"`. The renderer refuses any other value. |
| `title` | string | no | HTML `<title>` and the H1 at the top of the page. |
| `run` | object | no | Free-form header data. The known fields are `repo`, `ref`, `box`, `date`, `lane`, `commit`. |
| `scenarios[].id` | string | no | Scenario identifier. Optional; the renderer substitutes `scenario-<index>` when absent. Must match `[A-Za-z0-9._-]+` if present (the value is reused in the gallery output path). The renderer writes the resolved id to the section's `data-scenario` attribute. |
| `scenarios[].label` | string | no | Display name; defaults to `id`. |
| `scenarios[].description` | string | no | One-line description shown under the label. |
| `scenarios[].audio` | string | no | Path (relative to root) to the scenario's audio file. Copied into the output and renamed `<id>.<ext>`. |
| `scenarios[].claims[]` | array | no | Each entry is `{ name: string, held: bool }`. Rendered as chips. |
| `scenarios[].moments[]` | array | yes | At least one entry. Each entry has: |
| `moments[].image` | string | yes | Path (relative to root) to a PNG. Must exist; the renderer copies it under `<output>/moments/<id>/<n>.<ext>`. |
| `moments[].facts` | string | no | Path (relative to root) to a JSON file. The renderer reads and embeds it inline as the facts pane beside the frame. |
| `moments[].timestamp_ms` | integer | no | Optional millisecond offset. Rendered under the frame thumbnail. |
| `moments[].label` | string | no | Optional label; defaults to `moment <index>`. |

The renderer refuses to dereference any path that resolves outside the
gallery root after normalization (a hand-edited manifest must not be
able to read `/etc/passwd`).

## Inputs

The action exposes a small set of inputs; the manifest carries
everything else.

| Input | Required | Description |
| --- | --- | --- |
| `manifest` | yes | Path to the moments manifest, relative to `root`. |
| `root` | yes | Directory the manifest is read from and the moment PNG paths are resolved against. The gallery output is written under `<root>/<name>-gallery/`. |
| `name` | yes | Gallery name. Used as the lane label in the HTML header and as the artifact name suffix. |
| `summary_thumbnails` | no (default `3`) | How many scenarios' first frame to embed as a base64 thumbnail in the job summary. |

Pages deploys are deliberately *not* inside this action. The caller
composes a separate `actions/upload-pages-artifact` + `actions/deploy-pages`
step in their workflow, the same way callers of `pr-review-gate` compose
`actions/checkout` themselves. That keeps Pages policy in the caller and
this action usable without `pages: write`.

## Example manifest

```json
{
  "schema": "maxi-tools.moments-gallery.v1",
  "title": "HUD desktop e2e — 2026-09-18",
  "run": {
    "repo": "maxi-tools/voicemaci",
    "ref": "wt/moments-gallery-survey",
    "box": "maxibookpro24",
    "date": "2026-09-18T09:00:00Z",
    "lane": "hud-desktop-e2e"
  },
  "scenarios": [
    {
      "id": "warmup",
      "label": "warmup",
      "description": "cold start; timing not asserted",
      "audio": "scenarios/warmup/input.wav",
      "claims": [{ "name": "transcript", "held": true }],
      "moments": [
        {
          "image": "scenarios/warmup/frame-00000.png",
          "facts": "scenarios/warmup/frame-00000.json",
          "timestamp_ms": 0
        },
        {
          "image": "scenarios/warmup/frame-00250.png",
          "facts": "scenarios/warmup/frame-00250.json",
          "timestamp_ms": 250
        }
      ]
    },
    {
      "id": "samantha-normal",
      "label": "samantha-normal",
      "description": "the quick brown fox jumps over the lazy dog",
      "audio": "scenarios/samantha-normal/input.wav",
      "claims": [
        { "name": "transcript", "held": true },
        { "name": "first_partial_before_audio_end", "held": true },
        { "name": "recording_visible", "held": true }
      ],
      "moments": [
        {
          "image": "scenarios/samantha-normal/frame-00250.png",
          "facts": "scenarios/samantha-normal/frame-00250.json",
          "timestamp_ms": 250
        },
        {
          "image": "scenarios/samantha-normal/frame-00500.png",
          "facts": "scenarios/samantha-normal/frame-00500.json",
          "timestamp_ms": 500
        }
      ]
    }
  ]
}
```

## How a caller uses it

```yaml
- name: Render moments gallery
  uses: maxi-tools/ci/.github/actions/moments-gallery@main
  with:
    manifest: hud-e2e.manifest.json
    root: target/hud-e2e
    name: hud-e2e
```

The action uploads `<root>/<name>-gallery/` as the artifact and writes
the first three scenarios' first frame into the job summary as inline
base64 PNGs.

To deploy to GitHub Pages, the caller adds:

```yaml
- name: Publish to GitHub Pages
  uses: actions/upload-pages-artifact@v3
  with:
    path: target/hud-e2e/hud-e2e-gallery
- name: Deploy to Pages
  uses: actions/deploy-pages@v4
```

## Upgrade path from each existing gallery

The deliverable collapses three previously-separate gallery
implementations into one:

| Existing | Where it lives | What replaces it |
| --- | --- | --- |
| `GalleryGenerator` HTML (cross-platform test report) | `maxi-tools/maxi-e2e/src/gallery.rs`, `adapters/maxi-e2e-gallery/src/generator.rs` | This action emits the gallery HTML; the Rust generator is now redundant for any caller that can write a manifest. Mark the Rust generator for retirement once the Rust consumers that need a Rust-built gallery have adopted the action. |
| `review_gallery.html` template (scenario/media report) | `maxi-tools/maxi-e2e/adapters/maxi-e2e-gallery/src/review_gallery.rs` | Same replacement. Marked for retirement. |
| `glass-gallery` per-page HTML | `maxi-tools/maxi-e2e/tools/glass-gallery/` | Same replacement. Marked for retirement. |
| `waterui-gallery` workflow artifacts | `maxi-tools/maxi-ui/.github/workflows/waterui-gallery.yml` (the workflow that calls `tools/waterui_gallery_snapshot.sh`) | **Not replaced.** The WaterUI gallery's manifest is `maxi-ui.waterui-gallery.v1`, which is a renderer-test contract (capture receipts, expected sections), not a moments timeline. Adopting the action here would force a schema downgrade on a different concern. A follow-up PR can add a *second* workflow in `maxi-ui` that calls this action against the WaterUI gallery's PNGs -- the manifest is trivially writable in Python from the existing layout, because each page becomes one scenario and each is a single moment. |

In every replacement the existing tool continues to work until the
caller migrates; this is a *new* shared lane, not a deprecation of
any other tool today. A separate PR may mark the Rust generator and
`glass-gallery` for retirement once the consumers that need them have
moved across.

## Self-test

```sh
python3 .github/actions/moments-gallery/moments_gallery.py --self-test
```

Builds a synthetic root with two scenarios (warmup: two PNGs;
samantha-normal: three PNGs), a JSON facts file, an audio file,
and a manifest that mixes held and failed claims; runs the renderer;
and asserts on the HTML and the file layout. Uses stdlib only.