# Blueprints

## What `ued batch` covers
- Create an Actor Blueprint (or reuse an existing one) and add components:
  ```json
  {"op":"bp","path":"/Game/Blueprints/BP_StreetLamp","parent":"Actor",
   "components":[{"class":"StaticMeshComponent","name":"Pole","mesh":"/Game/Props/SM_Pole"},
                 {"class":"PointLightComponent","name":"Bulb","props":{"intensity":5000}}]}
  ```
  Existing components with the same name are skipped. The Blueprint is compiled when `BlueprintEditorLibrary` is available, which `ued doctor` reports.
- Place instances with `{"asset":"/Game/Blueprints/BP_StreetLamp","id":"lamp_1","loc":[...]}`, or a generator.
- Set per-instance variables or properties with `props` (use the variable name as exposed to Python).

## What it does not cover
Event graphs and node logic. Use the editor's Blueprint tooling (Epic's Unreal MCP exposes Blueprint toolsets in 5.8), or write the logic in C++ when the project uses C++. Do not replace Blueprint logic with a Python script that runs in the editor. Editor Python does not run in a packaged game.

## Rules
- Before creating a Blueprint, check `ued assets find <name> --cat blueprint`.
- Use `BP_` prefixes and the project's folder layout.
- After graph edits through other tools, still run `ued save` and `ued pie --seconds 10` to catch runtime errors (`Accessed None`, Blueprint Runtime Error), then `ued complete`.
