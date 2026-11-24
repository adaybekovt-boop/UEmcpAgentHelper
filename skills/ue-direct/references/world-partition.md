# World Partition and One File Per Actor (OFPA)

## Facts that matter
- A World Partition level stores each actor in its own package under `/Game/__ExternalActors__/<map path>/...`. The `.umap` alone holds almost nothing. Saving "the map" is not enough: the dirty external actor packages must be saved too. `ued save` does this. It saves the level plus every dirty package under that map's external-actor folder, and then checks each file on disk.
- Deleting an actor deletes its external package file on save. `ued save` reports these as `deleted_external_actors`, not as errors.
- In the editor only actors inside loaded regions exist as objects. `ued inspect` counts loaded actors. After a reload, `ued verify` also compares World Partition actor descriptors (saved actors, loaded or not) when the API is available.

## Creating
`ued level new /Game/Maps/OpenWorld --partitioned` creates a partitioned level. Use it for large or open worlds. Small arenas and menus are simpler as non-partitioned levels.

## Verifying
- `ued verify --reload` reloads the level from disk and checks that every UE-Direct actor is still there and in place. For partitioned maps, actors outside the loaded region after reload may not be loaded. The verify result then flags them as missing. If that happens, check whether the region is loaded before you assume data loss. Load the region in the editor, or verify with a smaller area.
- Source control: external actor files must be committed along with the map, so tell the user about `__ExternalActors__` when they commit.

## Data layers / HLOD
Not managed by `ued batch`. Use the editor tools, then `ued save` and `ued verify`.
