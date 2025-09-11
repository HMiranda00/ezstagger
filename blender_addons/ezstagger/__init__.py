bl_info = {
    "name": "EZ Stagger Offset",
    "author": "EZStagger Team",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "Dope Sheet / Action Editor",
    "description": "Alt-drag to apply staggered per-channel frame offsets to selected keyframes",
    "warning": "",
    "doc_url": "",
    "category": "Animation",
}

import bpy
import re
from bpy.types import Operator, AddonPreferences
from bpy.props import BoolProperty, EnumProperty


ADDON_KEYMAP_ITEMS = []
MSGBUS_OWNER = object()
SELECTION_HISTORY: list[str] = []


def _parse_bone_name_from_datapath(data_path: str) -> str | None:
    if not data_path:
        return None
    # Robust, regex-free parsing: pose.bones["BoneName"]
    prefix = 'pose.bones["'
    start = data_path.find(prefix)
    if start == -1:
        return None
    start += len(prefix)
    end = data_path.find('"]', start)
    if end == -1:
        return None
    return data_path[start:end]


def _action_owners_map() -> dict:
    """Build a mapping from Action datablocks to owning Objects.

    Note: An Action can theoretically be used by multiple owners. In such cases,
    we keep the first encountered owner per Action.
    """
    owners: dict = {}
    # Prefer objects in the active view layer for performance & relevance
    for obj in bpy.context.view_layer.objects:
        ad = getattr(obj, "animation_data", None)
        if not ad:
            continue
        act = getattr(ad, "action", None)
        if act and act not in owners:
            owners[act] = obj
        # Also consider NLA strips that reference actions
        if getattr(ad, "nla_tracks", None):
            for track in ad.nla_tracks:
                for strip in track.strips:
                    act = getattr(strip, "action", None)
                    if act and act not in owners:
                        owners[act] = obj
    return owners


def _gather_relevant_actions(context):
    """Yield actions relevant to the current Dope Sheet/Action Editor context.

    Priority:
    - If the current Dope Sheet is in Action Editor mode, use the displayed action.
    - Otherwise, collect actions from selected objects' animation_data (active action and NLA strip actions).
    - As a fallback, iterate all actions (may include unused datablocks).
    """
    yielded = set()
    area = context.area
    if area and area.type == 'DOPESHEET_EDITOR':
        space = context.space_data
        if getattr(space, 'mode', 'DOPESHEET') == 'ACTION':
            action = getattr(space, 'action', None)
            if action:
                yielded.add(action)
                yield action

    # Selected objects' actions
    for obj in context.selected_objects or []:
        ad = getattr(obj, 'animation_data', None)
        if not ad:
            continue
        act = getattr(ad, 'action', None)
        if act and act not in yielded:
            yielded.add(act)
            yield act
        if getattr(ad, 'nla_tracks', None):
            for track in ad.nla_tracks:
                for strip in track.strips:
                    act = getattr(strip, 'action', None)
                    if act and act not in yielded:
                        yielded.add(act)
                        yield act

    # Fallback: all actions
    for act in bpy.data.actions:
        if act not in yielded:
            yield act


def _on_active_object_change():
    # Track selection order by active object history
    try:
        vlayer = bpy.context.view_layer
        obj = vlayer.objects.active if vlayer else None
        if obj is None:
            return
        name = obj.name
        try:
            SELECTION_HISTORY.remove(name)
        except ValueError:
            pass
        SELECTION_HISTORY.append(name)
    except Exception:
        pass


class EZSTAGGER_Preferences(AddonPreferences):
    bl_idname = __name__

    order_mode: EnumProperty(
        name="Default Order",
        description="Default ordering of channels for the stair offset",
        items=(
            ("OUTLINER", "Outliner-like", "Order by owner name, then bone, then path"),
            ("TIME", "Selection-like (Earliest)", "Order by each channel's earliest selected keyframe time"),
        ),
        default="OUTLINER",
    )

    auto_grouping: BoolProperty(
        name="Auto Grouping (per FCurve vs per Owner)",
        description=(
            "Attempt to auto-detect grouping level: if any F-Curve channel header is selected,"
            " group per F-Curve; otherwise group per Object/Bone"
        ),
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="EZ Stagger Offset")
        layout.prop(self, "order_mode")
        layout.prop(self, "auto_grouping")


class EZSTAGGER_OT_stagger_modal(Operator):
    bl_idname = "anim.ez_stagger_modal"
    bl_label = "EZ Stagger Offset"
    bl_description = "Alt-drag to offset selected keyframes with a stagger by channel"
    bl_options = {"REGISTER", "UNDO", "GRAB_CURSOR", "BLOCKING"}

    invert_grouping: BoolProperty(
        name="Invert Grouping",
        description="If true, swap grouping mode: per owner vs per fcurve",
        default=False,
        options=set(),
    )

    use_time_order: BoolProperty(
        name="Use Time Order",
        description="If true, order channels by earliest selected keyframe time",
        default=False,
        options=set(),
    )

    # Internal runtime state
    _initial_mouse_region_x: int | None = None
    _initial_time: float | None = None
    _region: object | None = None
    _owners_by_action: dict | None = None
    _groups: list | None = None
    _group_items: dict | None = None
    _fcurves_to_update: set | None = None
    _last_applied_delta: float | None = None

    def invoke(self, context, event):
        # Start only in Dope Sheet editor
        area = context.area
        if not area or area.type != 'DOPESHEET_EDITOR':
            self.report({'WARNING'}, "EZ Stagger works in Dope Sheet / Action Editor")
            return {'CANCELLED'}

        # Modifier toggles at invoke
        self.invert_grouping = event.shift
        self.use_time_order = event.ctrl

        # Capture selection snapshot and prepare groups
        ok = self._prepare_groups(context)
        if not ok:
            self.report({'WARNING'}, "No selected keyframes found")
            return {'CANCELLED'}

        # Setup mouse/time reference
        region = context.region
        if not region or getattr(region, 'type', '') != 'WINDOW':
            self.report({'WARNING'}, "Start the drag in the Dope Sheet main area")
            return {'CANCELLED'}
        v2d = region.view2d
        self._initial_mouse_region_x = event.mouse_region_x
        # Convert region x to time (frames)
        x_view, _ = v2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
        self._initial_time = x_view
        self._region = region
        self._last_applied_delta = None

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            # Revert
            self._apply_offset(0.0)
            self._finalize_updates()
            return {'CANCELLED'}

        if event.type == 'MOUSEMOVE':
            # Compute integer frame delta from initial view x
            region = self._region or context.region
            v2d = region.view2d if region else context.region.view2d
            x_view, _ = v2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
            delta_frames = round(x_view - (self._initial_time or 0.0))
            if self._last_applied_delta != delta_frames:
                self._apply_offset(delta_frames)
                self._tag_redraw(context)
                self._last_applied_delta = delta_frames
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'RELEASE':
            # Confirm current state
            self._finalize_updates()
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def _prepare_groups(self, context) -> bool:
        """Scan all selected keyframe points and build grouping structures.

        Stores:
        - _groups: list of channel keys in the computed order
        - _group_items: mapping channel key -> list of KeyframeItem
        - _fcurves_to_update: set of fcurves to call update() on after edits
        - _owners_by_action: mapping Action -> Object owner
        """
        # Snapshot owners mapping
        self._owners_by_action = _action_owners_map()

        # Collect all selected keyframes across relevant Actions/FCurves
        KeyItem = _KeyItem
        selected_items: list[KeyItem] = []

        for act in _gather_relevant_actions(context):
            if not act.fcurves:
                continue
            owner = self._owners_by_action.get(act)
            for fc in act.fcurves:
                # Skip invisible/muted? Keep it simple: operate purely on selection flags
                for kp in fc.keyframe_points:
                    if getattr(kp, "select_control_point", False):
                        selected_items.append(KeyItem(owner, act, fc, kp))

        if not selected_items:
            return False

        # Decide grouping mode
        prefs = _prefs()
        group_per_fcurve = True
        if prefs.auto_grouping:
            # Prefer per-owner/bone unless a channel header is explicitly selected
            any_channel_header_selected = any(getattr(it.fcurve, 'select', False) for it in selected_items)
            group_per_fcurve = any_channel_header_selected
            if self.invert_grouping:
                group_per_fcurve = not group_per_fcurve
        else:
            # Default to per-fcurve unless inverted
            group_per_fcurve = not self.invert_grouping

        # Group items
        group_items: dict = {}
        fcurves_to_update: set = set()

        for it in selected_items:
            fcurves_to_update.add(it.fcurve)
            chan_key = it.channel_key_per_fcurve() if group_per_fcurve else it.channel_key_per_owner()
            if chan_key not in group_items:
                group_items[chan_key] = []
            group_items[chan_key].append(it)

        # Determine ordering of channels
        order_mode = self._resolve_order_mode()
        if order_mode == 'TIME':
            # Earliest selected key per channel
            def earliest_time(items: list[KeyItem]) -> float:
                return min(k.orig_co_x for k in items)
            groups_sorted = sorted(group_items.keys(), key=lambda k: earliest_time(group_items[k]))
        else:
            # Default: selection order of objects in viewport, fallback to Outliner index
            sel_rank = _build_selection_order_map(context)
            outline_rank = _build_outliner_order_map(context)

            def group_sort_key(k):
                items = group_items[k]
                ref = items[0]
                obj = ref.owner_obj
                sr = sel_rank.get(obj, 10**9)
                orank = outline_rank.get(obj, 10**9)
                return (
                    sr,
                    orank,
                    ref.owner_name or "",
                    ref.bone_name or "",
                    ref.data_path or "",
                    ref.array_index if ref.array_index is not None else -1,
                )

            groups_sorted = sorted(group_items.keys(), key=group_sort_key)

        self._groups = groups_sorted
        self._group_items = group_items
        self._fcurves_to_update = fcurves_to_update
        return True

    def _resolve_order_mode(self) -> str:
        prefs = _prefs()
        if self.use_time_order:
            # Ctrl toggles to TIME ordering during this run
            return 'TIME'
        return prefs.order_mode

    def _apply_offset(self, delta_frames: float) -> None:
        """Apply staggered offsets based on current delta_frames.

        The first group (anchor) gets 0 offset, second gets 1*delta, etc.
        """
        if not self._groups or not self._group_items:
            return
        for idx, gkey in enumerate(self._groups):
            group_offset = idx * delta_frames
            items = self._group_items[gkey]
            for it in items:
                it.apply_offset(group_offset)

    def _tag_redraw(self, context) -> None:
        area = context.area
        if area:
            area.tag_redraw()

    def _finalize_updates(self) -> None:
        # Recalculate fcurves once at the end to avoid heavy updates during drag
        if not self._fcurves_to_update:
            return
        for fc in self._fcurves_to_update:
            try:
                fc.update()
            except Exception:
                pass


class _KeyItem:
    """Snapshot of a selected keyframe point with minimal context and original values."""

    def __init__(self, owner_obj, action, fcurve, keyframe_point):
        self.owner_obj = owner_obj  # may be None
        self.action = action
        self.fcurve = fcurve
        self.kp = keyframe_point

        # Original coordinates for restore and incremental updates
        self.orig_co_x = float(keyframe_point.co.x)
        self.orig_co_y = float(keyframe_point.co.y)
        self.orig_hl_x = float(keyframe_point.handle_left.x)
        self.orig_hl_y = float(keyframe_point.handle_left.y)
        self.orig_hr_x = float(keyframe_point.handle_right.x)
        self.orig_hr_y = float(keyframe_point.handle_right.y)

        # Pre-parse bone name and details for grouping keys
        self.data_path = fcurve.data_path
        self.array_index = fcurve.array_index
        self.bone_name = _parse_bone_name_from_datapath(self.data_path)
        self.owner_name = getattr(owner_obj, "name", None)

    def channel_key_per_fcurve(self):
        return (self.owner_name, self.bone_name, self.data_path, self.array_index)

    def channel_key_per_owner(self):
        # Collapse datapath dimension; treat owner+bone as the channel
        return (self.owner_name, self.bone_name, None, None)

    def apply_offset(self, offset_x: float):
        new_x = self.orig_co_x + offset_x
        dx = new_x - self.kp.co.x
        # Set control point
        self.kp.co.x = new_x
        # Shift handles horizontally by the same absolute delta from original
        # Keep vertical values unchanged
        self.kp.handle_left.x = self.orig_hl_x + offset_x
        self.kp.handle_right.x = self.orig_hr_x + offset_x


def _prefs() -> EZSTAGGER_Preferences:
    addon_prefs = bpy.context.preferences.addons.get(__name__)
    if addon_prefs is None:
        # Fallback with defaults
        return EZSTAGGER_Preferences()
    return addon_prefs.preferences


def _build_selection_order_map(context) -> dict:
    """Map objects to ranks using live selection history; fallback to current selected order.

    The most recently active object gets the highest priority (largest index in history),
    but we map to increasing rank (0 is first) preserving history order.
    """
    mapping = {}
    # Use selection history if available
    if SELECTION_HISTORY:
        for i, name in enumerate(SELECTION_HISTORY):
            obj = bpy.data.objects.get(name)
            if obj is not None and obj not in mapping:
                mapping[obj] = i
    # Fallback to current selected objects order
    if not mapping and getattr(context, 'selected_objects', None):
        for i, obj in enumerate(context.selected_objects):
            mapping[obj] = i
    return mapping


def _build_outliner_order_map(context) -> dict:
    """Map objects to a stable index following the View Layer Outliner order.

    We traverse the layer_collection tree depth-first and enumerate collection.objects
    in their stored order, matching the Outliner order as closely as possible.
    """
    mapping = {}
    counter = [0]

    def visit_layer(layer):
        col = layer.collection
        for obj in col.objects:
            if obj not in mapping:
                mapping[obj] = counter[0]
                counter[0] += 1
        for child in layer.children:
            visit_layer(child)

    view_layer = context.view_layer if getattr(context, 'view_layer', None) else None
    if view_layer:
        visit_layer(view_layer.layer_collection)
    else:
        # Fallback to scene collection
        root = context.scene.collection if getattr(context, 'scene', None) else None
        if root:
            for obj in root.all_objects:
                mapping[obj] = counter[0]
                counter[0] += 1
    return mapping


classes = (
    EZSTAGGER_Preferences,
    EZSTAGGER_OT_stagger_modal,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Keymap: Dope Sheet editor, Alt+LeftMouse (Press) starts modal operator
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Dopesheet", space_type='DOPESHEET_EDITOR')
        kmi = km.keymap_items.new(EZSTAGGER_OT_stagger_modal.bl_idname, type='LEFTMOUSE', value='PRESS', alt=True)
        ADDON_KEYMAP_ITEMS.append((km, kmi))

    # Subscribe to active object changes to record selection history
    try:
        bpy.msgbus.subscribe_rna(
            key=(bpy.types.LayerObjects, "active"),
            owner=MSGBUS_OWNER,
            args=(),
            notify=lambda: _on_active_object_change(),
        )
    except Exception:
        # Fallback for API changes
        try:
            bpy.msgbus.subscribe_rna(
                key=(bpy.types.ViewLayer, "objects.active"),
                owner=MSGBUS_OWNER,
                args=(),
                notify=lambda: _on_active_object_change(),
            )
        except Exception:
            pass


def unregister():
    # Remove keymaps
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        for km, kmi in ADDON_KEYMAP_ITEMS:
            try:
                km.keymap_items.remove(kmi)
            except Exception:
                pass
        ADDON_KEYMAP_ITEMS.clear()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    # Unsubscribe msgbus
    try:
        bpy.msgbus.clear_by_owner(MSGBUS_OWNER)
    except Exception:
        pass


if __name__ == "__main__":
    register()


