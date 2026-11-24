---
name: ue-direct
description: Editor-first execution harness for Unreal Engine 5.8+. Use for ANY task that creates or changes levels, maps, actors, assets, Blueprints, materials, lighting, VFX or other game content in an Unreal project - it drives the running Unreal Editor, batches operations, saves native assets and verifies the result (reload + screenshot). Not needed for pure C++, docs or config edits.
license: MIT
metadata:
  author: T&T Labs
  version: "0.1.0"
---

# UE-Direct

Build the game inside Unreal Editor, then prove it was saved and looks right.
`ued` below means `python <this skill dir>/scripts/ued.py` (`python3` on macOS/Linux). Each call prints one JSON line; exit 0 = ok, 1 = failed/blocked.

## Workflow for editor tasks

1. `ued task start --kind level|visual|content|functional [--level /Game/Maps/X] [--goal "..."]`
2. `ued up`: finds the project, attaches to or launches the editor, and waits until it is really ready. If it reports `blocked`, fix the `blockers`. Never work around the editor.
3. `ued inspect` gives a compact scene digest. Run `ued assets find <words> [--cat mesh|material|blueprint]` and **reuse existing assets** before creating new ones.
4. Build with `ued level new|open <path>` and `ued batch plan.json`. One call handles 1 to 20,000 operations.
5. `ued save`, then `ued verify --reload` for level work.
6. `ued shot`, then open the PNG and look at it. Record what you saw with `ued review <shot> --verdict pass|fail --notes "..."`. Fix issues and re-shoot.
7. `ued complete`. Only `"status":"success"` means done. Report its fields as they are.

Code-only tasks (C++, docs, config) skip all of this.

## Batch plan

`{"ops":[...], "on_error":"continue|stop|rollback"}`. Units are cm, Z is up. `rot` is a yaw number or `[roll,pitch,yaw]`.

- **spawn**: `{"asset":"/Game/.../SM_X"` or `"class":"PointLight", "id":"door1", "loc":[x,y,z], "rot":90, "scale":1.2, "label", "folder", "tags", "material"|"materials":[...], "props":{}, "comp_props":{"PointLightComponent":{"intensity":8000}}}`
- **generators** (expanded locally, seeded, deterministic):
  - `scatter {id, asset|assets, count, area:{min:[x,y],max:[x,y]} | {center,radius}, min_dist, seed, yaw:[0,360], scale:[.8,1.2], z}`
  - `grid {id, asset, count:[nx,ny], step:[dx,dy], origin}`
  - `ring {id, asset, count, center, radius}`
  - `line {id, asset, count, start, end}`
- **edit**:
  - `transform {target, loc|offset|rot|rotate|scale|scale_by}`
  - `set {target, props|comp_props|label|folder|tags|material|mesh}`
  - `attach {target, parent}`
  - `delete {target}`: needs `--allow-delete` and the user's permission.
- **assets** (existing assets are reused, never duplicated):
  - `mi {path, parent, scalar, vector, texture}`
  - `bp {path, parent, components:[{class,name,mesh}]}`
  - `import {file, dest}`
  - `duplicate_asset {source, dest}`

Selectors: `id:rock#*`, `label:Tree_*`, `tag:X`, `class:PointLight`, `folder:Props/*`.

Re-running a plan is safe: the same `id` updates the existing actor and never creates a duplicate. Flags:
- `--save --verify --reload` saves and verifies in the same call.
- `--dry-run` validates the plan without the editor.

## Rules

- Never write `.umap`/`.uasset` files yourself. Never deliver JSON layouts, generator scripts or runtime spawners instead of saved content, unless the user asked for runtime generation.
- Never discard unsaved user work. If you get `UNSAVED_CHANGES`, ask the user; use `--save-dirty` only with their consent. Don't close or restart the editor without consent.
- Prefer `ued` over raw MCP calls. Use other editor tools only for what `ued` lacks (Blueprint graphs, PCG, Sequencer, Niagara editing), then still `ued save` / `verify` / `complete`.
- Take screenshots at milestones and at the end, not after every op. Check composition, scale, placement, lighting, materials and intersections.
- If you are truly blocked, run `ued task block --reason "..."` and tell the user exactly what is needed.

## References (load only the one you need)

`references/level-design.md` · `lighting.md` · `materials.md` · `blueprints.md` · `animation.md` · `world-partition.md` · `optimization.md` · `transports.md` (MCP/remote/relay setup) · `troubleshooting.md` (error codes)
