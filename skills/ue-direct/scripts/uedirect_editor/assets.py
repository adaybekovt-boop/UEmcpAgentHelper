"""Asset Registry scan for the host-side asset cache (inside Unreal Editor).

Results are written to the response file, never streamed to the agent.
"""

import unreal

from .common import asset_registry

ENGINE_BASICS = ("/Engine/BasicShapes",)


def _class_name(ad):
    try:
        return str(ad.asset_class_path.asset_name)
    except Exception:  # noqa: BLE001 - pre-5.1 fallback
        return str(ad.asset_class)


def cmd_assets_scan(ctx, args):
    ar = asset_registry()
    roots = args.get("roots") or ["/Game"]
    rows = []
    for root in roots:
        for ad in ar.get_assets_by_path(root, recursive=True) or []:
            rows.append([str(ad.package_name), _class_name(ad)])
    if args.get("engine_basics", True):
        for root in ENGINE_BASICS:
            for ad in ar.get_assets_by_path(root, recursive=False) or []:
                rows.append([str(ad.package_name), _class_name(ad)])
    try:
        loading = bool(ar.is_loading_assets())
    except Exception:  # noqa: BLE001
        loading = False
    return {"rows": rows, "count": len(rows), "registry_loading": loading, "roots": roots}


def cmd_assets_exist(ctx, args):
    paths = args.get("paths") or []
    return {"missing": [p for p in paths if not unreal.EditorAssetLibrary.does_asset_exist(p)]}
