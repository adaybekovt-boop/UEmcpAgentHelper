# Animation assets

UE-Direct does not author animation data. It covers placing and wiring animated content and verifying it:

- **Import**: `{"op":"import","file":"D:/Art/Hero_Run.fbx","dest":"/Game/Characters/Hero/Anims"}`. An existing asset with the same name is reused unless you pass `"replace":true`. For skeletal FBX the import settings (skeleton choice) come from the project's defaults. Check the result with `ued assets find Hero_Run --cat anim`.
- **Placement**: Blueprints or SkeletalMeshActors are spawned like any asset (`asset` = the Blueprint or skeletal mesh path).
- **Settings on instances**: use `comp_props` on the skeletal mesh component, for example `{"SkeletalMeshComponent0":{"animation_mode":"ANIMATION_SINGLE_NODE"}}`. Property names follow the Python API, and enum values are given as member names.
- **Animation Blueprints, montages, retargeting and Control Rig** are edited with the editor's own tools (or the matching Unreal MCP toolsets). Afterwards run `ued save`, `ued pie`, and a screenshot during PIE when motion matters.

## Verify
`ued pie --seconds 8` reports runtime errors from the log. A static screenshot cannot prove motion. Say so in the report if you could not check motion visually.
