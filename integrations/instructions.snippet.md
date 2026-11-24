<!-- UE-DIRECT:BEGIN (managed by the UE-Direct installer; edits inside this block are overwritten) -->
## Unreal Engine work (UE-Direct)

- Levels, actors, assets, Blueprints, materials, lighting and anything visual are built **inside Unreal Editor** with the `ue-direct` skill (`{UED} ...`). Read its SKILL.md first.
- Never write `.umap`/`.uasset` files directly, and never deliver JSON layouts, generator scripts or runtime spawners instead of saved content (unless the user explicitly asked for runtime generation).
- Never discard unsaved editor work; never mass-delete content without the user's permission.
- A task is done only when `{UED} complete` returns `"status":"success"`. If the editor cannot be reached or fixed, say so and run `{UED} task block --reason "..."`; do not fake results.
<!-- UE-DIRECT:END -->
