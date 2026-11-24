# Materials

## Reuse first
`ued assets find <word> --cat material` lists materials and material instances. Prefer a **Material Instance** of an existing master material over a new material. Never create a material per actor.

## Create or update a Material Instance
```json
{"op":"mi","path":"/Game/Materials/MI_Brick_Red","parent":"/Game/Materials/M_Brick",
 "scalar":{"Roughness":0.7},"vector":{"BaseColor":[0.6,0.1,0.08,1]},"texture":{"Albedo":"/Game/Textures/T_Brick_D"}}
```
- If the asset already exists it is updated, never duplicated.
- Parameter names must match the parent material's exposed parameters exactly. A wrong name is ignored by the engine, so verify visually.
- Follow the project's naming. Typical prefixes are `M_`, `MI_`, `MF_`, `T_`, with suffixes `_D`, `_N`, `_ORM`.

## Assign
- To one actor: `{"op":"set","target":"id:wall_3","material":"/Game/Materials/MI_Brick_Red"}`
- Per slot: `"materials":[slot0, null, slot2]`. Use `null` to keep a slot.
- In bulk: `{"op":"material","target":"label:Wall_*","material":"..."}`. One op covers every match.

## Master materials
Building material graphs (nodes, expressions) is not part of `ued batch`. Use the editor's material tools, or an MCP material toolset if present. After that, run `ued save` and take a screenshot. Then make instances with `mi`.

## Verify
Take a close shot: `ued shot --select id:wall_* --res 960x540`. Look for stretched UVs, wrong tiling, black (missing texture) or checkerboard (still compiling: wait and re-shoot).
