# Level design with UE-Direct

## Plan before placing
1. `ued inspect` shows the level, its bounds, categories, lighting flags and issues. A level with `actors: 0` and no lights needs a lighting pass too (see lighting.md).
2. `ued assets find <theme words> --cat mesh` finds kits to reuse. Pick 3–8 meshes per role (ground, structures, props, foliage) and check names and sizes with `ued actors` after a test spawn.
3. Write the plan in layers, one batch per layer: blockout, then structures, then props, then dressing, then lights. Each layer is one `ued batch` call. Give each layer an id prefix (`bld_`, `prop_`, `tree_`) so you can address it later with `id:bld_*`.

## Scale reference (UE units = cm)
| Thing | Typical size |
|---|---|
| Human / PlayerStart capsule | 180 cm tall, radius 42 |
| Door | 100 x 210 cm |
| Story height | 300–400 cm |
| Road lane | 350 cm |
| Sidewalk | 200–300 cm |
| City block | 8,000–15,000 cm |
| Engine `/Engine/BasicShapes/Cube` | 100 cm cube, pivot at center |

Always spawn one instance and measure it before you scale a kit: `ued actors id:x --fields bounds`. Many marketplace meshes have their pivot at the base and others at the center. Set `z` accordingly.

## Patterns
- **Streets/grids**: use `grid` with `step` equal to the block size. Use `line` for street lights along a road, with `align: true` to face along it.
- **Natural scatter**: use `scatter` with `min_dist` (no overlaps), `yaw:[0,360]`, `scale:[0.8,1.25]` and `assets:[...]` for variety. Keep `seed` fixed so re-runs are identical.
- **Plazas/arenas**: use `ring` with `face_center` for seating, pillars and lights.
- **Organization**: set `folder` on every op, for example `City/Buildings` or `City/Props`. The World Outliner stays usable.
- **Gameplay minimum**: one `PlayerStart`, a floor with collision under it, and lighting.

## Common defects (check in `ued inspect` → `issues`, and in screenshots)
- `stacked_duplicates`: two identical meshes at the same spot. The usual cause is re-running a plan with new ids. Keep ids stable.
- `extreme_location`: an actor beyond 50 km. Usually caused by mixing up meters and centimeters.
- Floating or sunken props: the z is wrong for the pivot. Fix with `transform {target, offset:[0,0,dz]}`.
- Intersections: lower the scatter density or raise `min_dist`.

## Editing an existing map
- `ued level open /Game/Maps/X` refuses if there are unsaved changes. Ask the user before you pass `--save-dirty`.
- Address existing actors with `label:` or `class:` selectors. Ids only exist on UE-Direct-spawned actors.
- Deleting needs `--allow-delete` plus the user's go-ahead. The default limit is 50 actors per batch.
