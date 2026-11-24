"""Capability probe: checks that every engine API UE-Direct relies on exists in
the running editor. Cheap (no world changes) and run by ``ued doctor``."""

import unreal

REQUIRED = {
    "EditorActorSubsystem": ["spawn_actor_from_object", "spawn_actor_from_class", "get_all_level_actors",
                             "destroy_actors", "destroy_actor"],
    "LevelEditorSubsystem": ["new_level", "new_level_from_template", "load_level", "save_current_level",
                             "is_in_play_in_editor", "editor_request_begin_play", "editor_request_end_play",
                             "editor_play_simulate"],
    "UnrealEditorSubsystem": ["get_editor_world", "get_level_viewport_camera_info", "set_level_viewport_camera_info"],
    "EditorLoadingAndSavingUtils": ["get_dirty_map_packages", "get_dirty_content_packages", "save_dirty_packages",
                                    "save_packages", "save_map"],
    "EditorAssetLibrary": ["does_asset_exist", "load_asset", "duplicate_asset", "delete_asset"],
    "AssetRegistryHelpers": ["get_asset_registry"],
    "AutomationLibrary": ["take_high_res_screenshot"],
    "SystemLibrary": ["execute_console_command", "get_engine_version"],
    "MaterialEditingLibrary": ["set_material_instance_parent", "set_material_instance_scalar_parameter_value",
                               "set_material_instance_vector_parameter_value",
                               "set_material_instance_texture_parameter_value", "update_material_instance"],
    "AssetToolsHelpers": ["get_asset_tools"],
    "MathLibrary": ["find_look_at_rotation"],
    "Paths": ["get_project_file_path"],
}
OPTIONAL = {
    "SubobjectDataSubsystem": ["k2_gather_subobject_data_for_blueprint", "add_new_subobject", "rename_subobject"],
    "SubobjectDataBlueprintFunctionLibrary": ["get_data", "get_associated_object", "is_handle_valid"],
    "BlueprintEditorLibrary": ["compile_blueprint"],
    "WorldPartitionBlueprintLibrary": ["get_actor_descs"],
}
CLASSES = ["ScopedEditorTransaction", "MaterialInstanceConstantFactoryNew", "BlueprintFactory", "AssetImportTask",
           "AddNewSubobjectParams", "StaticMeshComponent", "MaterialInstanceConstant", "EnumBase", "AttachmentRule"]


def _missing(table):
    out = []
    for cls, methods in table.items():
        obj = getattr(unreal, cls, None)
        if obj is None:
            out.append(cls)
            continue
        out.extend("%s.%s" % (cls, m) for m in methods if not hasattr(obj, m))
    return out


def cmd_selftest(ctx, args):
    missing = _missing(REQUIRED) + [c for c in CLASSES if not hasattr(unreal, c)]
    optional = _missing(OPTIONAL)
    return {"ok": not missing, "missing_required": missing, "missing_optional": optional,
            "engine_version": unreal.SystemLibrary.get_engine_version()}
