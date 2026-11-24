# Optimization: scene performance and agent token cost

## Scene
- Thousands of identical meshes: placing them as individual StaticMeshActors is fine for blockouts and editing. For final dense dressing (grass, rocks, debris) the project should use foliage, PCG or instanced meshes. Ask before converting, because it changes the project's content architecture.
- Nanite: enabling it on meshes is an asset setting. Don't flip it on a whole folder without the user's OK.
- Lights: limit movable shadow-casting point and spot lights in one view. Prefer one directional light plus a sky light outdoors.
- Check `ued inspect` → `top_classes` and `top_assets` to spot accidental mass duplication.

## Token cost (how to work cheaply without skipping checks)
- One `ued batch` per layer, never one call per object. The editor-side payload goes through a file, so 1,000 ops cost about the same tokens as 10.
- Use generators (`scatter`, `grid`, `ring`, `line`) instead of listing coordinates.
- `ued inspect --since <revision>` returns `{"unchanged":true}` when nothing changed. Don't re-inspect after your own batches: the batch result already gives the delta.
- `ued actors <selector> --fields loc --limit 10` only when you need specifics.
- Screenshots: 960x540 is usually enough to judge layout. Go higher only for detail checks. One overview per milestone plus a final shot.
- Full details of the last run are in `Saved/UEDirect/last/<cmd>.json`. Read them only when diagnosing.
