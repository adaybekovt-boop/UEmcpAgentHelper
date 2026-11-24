# Lighting

## Minimal outdoor rig (one batch)
```json
{"ops":[
 {"class":"DirectionalLight","id":"sun","rot":[0,-40,35],"folder":"Lighting"},
 {"class":"SkyLight","id":"skylight","folder":"Lighting","comp_props":{"SkyLightComponent":{"real_time_capture":true}}},
 {"class":"SkyAtmosphere","id":"atmosphere","folder":"Lighting"},
 {"class":"ExponentialHeightFog","id":"fog","folder":"Lighting"},
 {"class":"VolumetricCloud","id":"clouds","folder":"Lighting"}
]}
```
`comp_props` keys may be a component's class name (`PointLightComponent`, `SkyLightComponent`; first match wins) or its object name. Property names follow the Python API (snake_case), for example `intensity`, `light_color`, `mobility`, `cast_shadows`. A wrong name fails that op with a clear error and changes nothing.

## Interior / night
- Use `PointLight` and `SpotLight` with `comp_props:{"PointLightComponent":{"intensity":..., "attenuation_radius":...}}`. Spot cones are set with `inner_cone_angle` and `outer_cone_angle`.
- Movable lights cost the most. Set `mobility` to `STATIC` or `STATIONARY` where the project uses baked lighting. Lumen projects usually keep movable.

## Verify
- `ued inspect` → `lighting` flags show whether directional, sky, atmosphere and fog are present.
- Shoot at least one `--mode overview` image and one close detail with `--select`. Check for black areas, blown-out highlights, missing sky and harsh shadows on key areas.
- Exposure: an outdoor scene that looks black in the shot often has no SkyLight, or a sun pitch above the horizon. Pitch must be negative for the sun to shine down.
