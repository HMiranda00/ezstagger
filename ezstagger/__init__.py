"""EZ Stagger Offset - Visual gizmo-based stagger tool for keyframe animation."""

import bpy
import blf
import gpu
import random
import math
from gpu_extras.batch import batch_for_shader
from bpy.types import Operator, AddonPreferences
from bpy.props import BoolProperty, EnumProperty, IntProperty


# -----------------------------------------------------------------------------
# Global State
# -----------------------------------------------------------------------------

class StaggerWidgetState:
    """Stores the state for the stagger gizmo overlay"""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.draw_handlers = {}
            cls._instance.enabled = True
            cls._instance.hovered = None  # "NORMAL", "REVERSE", "RANDOM" or None
            cls._instance.random_seed = 42
            cls._instance.gizmo_positions = []  # [(x, y, mode), ...]
            cls._instance.has_selection = False
            cls._instance.bbox = None
        return cls._instance


state = StaggerWidgetState()
addon_keymaps = []


# -----------------------------------------------------------------------------
# Utility Functions
# -----------------------------------------------------------------------------

def _parse_bone_name_from_datapath(data_path: str) -> str | None:
    if not data_path:
        return None
    prefix = 'pose.bones["'
    start = data_path.find(prefix)
    if start == -1:
        return None
    start += len(prefix)
    end = data_path.find('"]', start)
    if end == -1:
        return None
    return data_path[start:end]


def _get_fcurves_from_action(action):
    """Get all fcurves from action (Blender 5.0+ slotted actions API)."""
    fcurves = []
    if not hasattr(action, 'layers') or not action.layers:
        return fcurves
    for layer in action.layers:
        if not hasattr(layer, 'strips') or not layer.strips:
            continue
        for strip in layer.strips:
            if not hasattr(action, 'slots'):
                continue
            for slot in action.slots:
                channelbag = strip.channelbag(slot)
                if channelbag and hasattr(channelbag, 'fcurves') and channelbag.fcurves:
                    fcurves.extend(channelbag.fcurves)
    return fcurves


def _action_owners_map() -> dict:
    """Build a mapping from Action datablocks to owning Objects."""
    owners: dict = {}
    try:
        for obj in bpy.context.view_layer.objects:
            ad = getattr(obj, "animation_data", None)
            if not ad:
                continue
            act = getattr(ad, "action", None)
            if act and act not in owners:
                owners[act] = obj
            if getattr(ad, "nla_tracks", None):
                for track in ad.nla_tracks:
                    for strip in track.strips:
                        act = getattr(strip, "action", None)
                        if act and act not in owners:
                            owners[act] = obj
    except:
        pass
    return owners


def _gather_relevant_actions(context):
    """Yield actions relevant to the current Dope Sheet/Action Editor context."""
    yielded = set()
    try:
        area = context.area
        if area and area.type == 'DOPESHEET_EDITOR':
            space = context.space_data
            if getattr(space, 'mode', 'DOPESHEET') == 'ACTION':
                action = getattr(space, 'action', None)
                if action:
                    yielded.add(action)
                    yield action

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

        for act in bpy.data.actions:
            if act not in yielded:
                yield act
    except:
        pass


# -----------------------------------------------------------------------------
# Selection Detection
# -----------------------------------------------------------------------------

def _get_selected_keyframes_bbox():
    """Get bounding box of selected keyframes and count groups.
    Returns (bbox, num_groups) or (None, 0)."""
    context = bpy.context
    
    if not context.area or context.area.type != 'DOPESHEET_EDITOR':
        return None, 0
    
    region = context.region
    if not region:
        return None, 0
    
    v2d = region.view2d
    
    min_frame = float('inf')
    max_frame = float('-inf')
    channels_with_selection = set()  # Track unique channels
    
    for act in _gather_relevant_actions(context):
        fcurves = _get_fcurves_from_action(act)
        if not fcurves:
            continue
        for fc in fcurves:
            for kp in fc.keyframe_points:
                if getattr(kp, "select_control_point", False):
                    frame = kp.co.x
                    if frame < min_frame:
                        min_frame = frame
                    if frame > max_frame:
                        max_frame = frame
                    # Track this channel as having selection
                    channels_with_selection.add((id(act), id(fc)))
    
    num_groups = len(channels_with_selection)
    
    if num_groups == 0:
        return None, 0
    
    # Convert to region coordinates
    x1, _ = v2d.view_to_region(min_frame, 0, clip=False)
    x2, _ = v2d.view_to_region(max_frame, 0, clip=False)
    
    # Y bounds - use region center
    y_center = region.height / 2
    y_extent = 30
    y1 = y_center - y_extent
    y2 = y_center + y_extent
    
    return (x1 - 10, x2 + 10, y1, y2), num_groups


# -----------------------------------------------------------------------------
# Drawing Functions
# -----------------------------------------------------------------------------

def _draw_rounded_rect(shader, x, y, width, height, color, radius=4):
    """Draw a filled rectangle with rounded corners."""
    radius = min(radius, width / 2, height / 2)
    
    if radius <= 1:
        vertices = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
        indices = [(0, 1, 2), (0, 2, 3)]
        batch = batch_for_shader(shader, 'TRIS', {"pos": vertices}, indices=indices)
        shader.uniform_float("color", color)
        batch.draw(shader)
        return
    
    vertices = []
    segments = 4
    
    corners = [
        (x + radius, y + radius, math.pi, 1.5 * math.pi),
        (x + width - radius, y + radius, 1.5 * math.pi, 2 * math.pi),
        (x + width - radius, y + height - radius, 0, 0.5 * math.pi),
        (x + radius, y + height - radius, 0.5 * math.pi, math.pi),
    ]
    
    for cx, cy, start_angle, end_angle in corners:
        for i in range(segments + 1):
            t = i / segments
            angle = start_angle + t * (end_angle - start_angle)
            vx = cx + radius * math.cos(angle)
            vy = cy + radius * math.sin(angle)
            vertices.append((vx, vy))
    
    center = (x + width / 2, y + height / 2)
    vertices.insert(0, center)
    
    indices = []
    num_verts = len(vertices)
    for i in range(1, num_verts - 1):
        indices.append((0, i, i + 1))
    indices.append((0, num_verts - 1, 1))
    
    batch = batch_for_shader(shader, 'TRIS', {"pos": vertices}, indices=indices)
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_circle(shader, x, y, radius, color, segments=16):
    """Draw a filled circle."""
    vertices = [(x, y)]
    for i in range(segments + 1):
        angle = 2 * math.pi * i / segments
        vertices.append((x + radius * math.cos(angle), y + radius * math.sin(angle)))
    
    indices = [(0, i + 1, (i + 1) % segments + 1) for i in range(segments)]
    
    batch = batch_for_shader(shader, 'TRIS', {"pos": vertices}, indices=indices)
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_circle_outline(shader, x, y, radius, color, width=2, segments=24):
    """Draw a circle outline."""
    vertices = []
    for i in range(segments + 1):
        angle = 2 * math.pi * i / segments
        vertices.append((x + radius * math.cos(angle), y + radius * math.sin(angle)))
    
    batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": vertices})
    shader.uniform_float("color", color)
    gpu.state.line_width_set(width)
    batch.draw(shader)
    gpu.state.line_width_set(1.0)


def _brighten_color(color, factor=0.15):
    """Brighten a color for hover states."""
    return tuple(min(1.0, c + factor) for c in color[:3]) + (min(1.0, color[3] + 0.1),)


def draw_stagger_gizmos():
    """Main draw callback for stagger gizmos."""
    context = bpy.context
    
    if context is None:
        return
    
    if not state.enabled:
        return
    
    if context.area is None or context.area.type != 'DOPESHEET_EDITOR':
        return
    
    region = context.region
    if region is None:
        return
    
    # Get selection bounding box and group count
    bbox, num_groups = _get_selected_keyframes_bbox()
    
    # Only show gizmos if more than 2 groups selected
    if bbox is None or num_groups < 2:
        state.has_selection = False
        state.gizmo_positions = []
        return
    
    state.has_selection = True
    state.bbox = bbox
    
    x1, x2, y1, y2 = bbox
    
    # Position gizmos to the right of selection
    center_x = x2 + 20
    gizmo_radius = 7
    spacing = 20
    
    mid_y = (y1 + y2) / 2
    top_y = mid_y + spacing
    bot_y = mid_y - spacing
    
    gizmo_positions = [
        (center_x, top_y, "REVERSE"),
        (center_x, mid_y, "RANDOM"),
        (center_x, bot_y, "NORMAL"),
    ]
    state.gizmo_positions = gizmo_positions
    
    # Colors
    col_normal = (0.35, 0.75, 0.45, 0.95)
    col_reverse = (0.75, 0.45, 0.35, 0.95)
    col_random = (0.55, 0.45, 0.75, 0.95)
    col_line = (0.4, 0.4, 0.4, 0.6)
    col_hover_outline = (1.0, 1.0, 1.0, 1.0)
    
    # Setup GPU
    gpu.state.blend_set('ALPHA')
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.bind()
    
    # Draw connecting line
    line_verts = [(center_x, bot_y), (center_x, top_y)]
    batch = batch_for_shader(shader, 'LINES', {"pos": line_verts})
    shader.uniform_float("color", col_line)
    gpu.state.line_width_set(2)
    batch.draw(shader)
    gpu.state.line_width_set(1)
    
    # Draw each gizmo
    for gx, gy, mode in gizmo_positions:
        if mode == "NORMAL":
            color = col_normal
            label = "▼"
        elif mode == "REVERSE":
            color = col_reverse
            label = "▲"
        else:
            color = col_random
            label = "◆"
        
        # Brighten if hovered
        if state.hovered == mode:
            color = _brighten_color(color, 0.2)
        
        # Draw filled circle
        _draw_circle(shader, gx, gy, gizmo_radius, color)
        
        # Draw hover outline
        if state.hovered == mode:
            _draw_circle_outline(shader, gx, gy, gizmo_radius + 2, col_hover_outline, 2)
        
        # Draw label
        font_id = 0
        blf.size(font_id, 9)
        blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
        dim = blf.dimensions(font_id, label)
        blf.position(font_id, gx - dim[0] / 2, gy - dim[1] / 2 + 1, 0)
        blf.draw(font_id, label)
    
    gpu.state.blend_set('NONE')
    
    # Draw tooltip for hovered gizmo
    if state.hovered:
        gpu.state.blend_set('ALPHA')
        shader.bind()
        
        tooltips = {
            "NORMAL": "Top → Bottom",
            "REVERSE": "Bottom → Top",
            "RANDOM": f"Random (seed {state.random_seed})",
        }
        tooltip = tooltips.get(state.hovered, "")
        
        for gx, gy, mode in gizmo_positions:
            if mode == state.hovered:
                # Draw tooltip background
                font_id = 0
                blf.size(font_id, 11)
                dim = blf.dimensions(font_id, tooltip)
                padding = 5
                box_x = gx + 18
                box_y = gy - dim[1] / 2 - padding
                box_w = dim[0] + padding * 2
                box_h = dim[1] + padding * 2
                
                _draw_rounded_rect(shader, box_x, box_y, box_w, box_h, (0.1, 0.1, 0.1, 0.9), 3)
                
                # Draw tooltip text
                blf.color(font_id, 0.9, 0.9, 0.9, 1.0)
                blf.position(font_id, box_x + padding, gy - dim[1] / 2, 0)
                blf.draw(font_id, tooltip)
                break
        
        gpu.state.blend_set('NONE')


def _draw_stagger_feedback(operator, context):
    """Draw feedback during modal stagger operation."""
    if operator._last_applied_delta is None:
        return
    
    x = operator._draw_mouse_x
    y = operator._draw_mouse_y
    
    delta = int(operator._last_applied_delta)
    groups = len(operator._groups) if operator._groups else 0
    mode = operator._order_mode
    
    mode_icons = {"NORMAL": "▼", "REVERSE": "▲", "RANDOM": "◆"}
    mode_icon = mode_icons.get(mode, "")
    
    text = f"{mode_icon} {delta:+d}f × {groups}"
    
    if mode == "RANDOM":
        subtext = f"seed {operator._random_seed} (scroll)"
    else:
        subtext = None
    
    font_id = 0
    blf.size(font_id, 14)
    dim1 = blf.dimensions(font_id, text)
    
    padding = 8
    box_x = x + 22
    
    gpu.state.blend_set('ALPHA')
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.bind()
    
    if subtext:
        blf.size(font_id, 10)
        dim2 = blf.dimensions(font_id, subtext)
        max_width = max(dim1[0], dim2[0])
        total_height = dim1[1] + dim2[1] + 4
        box_y = y - total_height / 2 - padding
        box_width = max_width + padding * 2
        box_height = total_height + padding * 2
        
        _draw_rounded_rect(shader, box_x, box_y, box_width, box_height, (0.08, 0.08, 0.08, 0.9), 5)
        
        blf.size(font_id, 14)
        blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
        blf.position(font_id, box_x + padding, box_y + padding + dim2[1] + 4, 0)
        blf.draw(font_id, text)
        
        blf.size(font_id, 10)
        blf.color(font_id, 0.55, 0.55, 0.55, 1.0)
        blf.position(font_id, box_x + padding, box_y + padding, 0)
        blf.draw(font_id, subtext)
    else:
        box_y = y - dim1[1] / 2 - padding
        box_width = dim1[0] + padding * 2
        box_height = dim1[1] + padding * 2
        
        _draw_rounded_rect(shader, box_x, box_y, box_width, box_height, (0.08, 0.08, 0.08, 0.9), 5)
        
        blf.size(font_id, 14)
        blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
        blf.position(font_id, box_x + padding, box_y + padding, 0)
        blf.draw(font_id, text)
    
    gpu.state.blend_set('NONE')


# -----------------------------------------------------------------------------
# Gizmo Hit Detection
# -----------------------------------------------------------------------------

GIZMO_HIT_RADIUS = 11


def check_gizmo_hover(mouse_x, mouse_y):
    """Check if mouse is hovering over a gizmo. Returns mode or None."""
    if not state.has_selection:
        return None
    
    for gx, gy, mode in state.gizmo_positions:
        dist_sq = (mouse_x - gx) ** 2 + (mouse_y - gy) ** 2
        if dist_sq <= GIZMO_HIT_RADIUS ** 2:
            return mode
    
    return None


# -----------------------------------------------------------------------------
# Operators
# -----------------------------------------------------------------------------

class EZSTAGGER_OT_hover(Operator):
    """Update cursor and hover state when moving over gizmos"""
    bl_idname = "anim.ez_stagger_hover"
    bl_label = "EZ Stagger Hover"
    bl_options = {'INTERNAL'}
    
    def invoke(self, context, event):
        if not state.enabled or context.region is None:
            return {'PASS_THROUGH'}
        
        mouse_x = event.mouse_region_x
        mouse_y = event.mouse_region_y
        
        # Check bounds
        if not (0 <= mouse_x <= context.region.width and
                0 <= mouse_y <= context.region.height):
            if state.hovered:
                state.hovered = None
                context.area.tag_redraw()
            return {'PASS_THROUGH'}
        
        old_hovered = state.hovered
        state.hovered = check_gizmo_hover(mouse_x, mouse_y)
        
        # Handle scroll for random seed
        if state.hovered == "RANDOM":
            if event.type == 'WHEELUPMOUSE':
                state.random_seed += 1
                context.area.tag_redraw()
            elif event.type == 'WHEELDOWNMOUSE':
                state.random_seed -= 1
                context.area.tag_redraw()
        
        # Update cursor
        if state.hovered:
            context.window.cursor_set('HAND')
        else:
            context.window.cursor_set('DEFAULT')
        
        # Redraw if hover changed
        if old_hovered != state.hovered:
            context.area.tag_redraw()
        
        return {'PASS_THROUGH'}


class EZSTAGGER_OT_gizmo_click(Operator):
    """Click on a stagger gizmo to start staggering"""
    bl_idname = "anim.ez_stagger_gizmo_click"
    bl_label = "EZ Stagger Click"
    bl_options = {'INTERNAL'}
    
    def invoke(self, context, event):
        if not state.enabled or not state.has_selection:
            return {'PASS_THROUGH'}
        
        mouse_x = event.mouse_region_x
        mouse_y = event.mouse_region_y
        
        clicked_mode = check_gizmo_hover(mouse_x, mouse_y)
        
        if clicked_mode:
            # Start the stagger modal with the clicked mode
            bpy.ops.anim.ez_stagger_modal(
                'INVOKE_DEFAULT',
                order_mode=clicked_mode,
                random_seed=state.random_seed
            )
            return {'FINISHED'}
        
        return {'PASS_THROUGH'}


class EZSTAGGER_OT_stagger_modal(Operator):
    bl_idname = "anim.ez_stagger_modal"
    bl_label = "EZ Stagger Offset"
    bl_description = "Drag to offset selected keyframes with a stagger by channel"
    bl_options = {"REGISTER", "UNDO", "GRAB_CURSOR", "BLOCKING"}

    order_mode: EnumProperty(
        name="Order Mode",
        items=(
            ("NORMAL", "Normal", "First selected → last"),
            ("REVERSE", "Reverse", "Last selected → first"),
            ("RANDOM", "Random", "Random order"),
        ),
        default="NORMAL",
    )
    
    random_seed: IntProperty(name="Random Seed", default=42)
    invert_grouping: BoolProperty(name="Invert Grouping", default=False, options=set())

    # Internal state
    _initial_time: float | None = None
    _region: object | None = None
    _groups: list | None = None
    _groups_original: list | None = None
    _group_items: dict | None = None
    _fcurves_to_update: set | None = None
    _last_applied_delta: float | None = None
    _draw_handler: object | None = None
    _draw_mouse_x: int = 0
    _draw_mouse_y: int = 0
    _order_mode: str = "NORMAL"
    _random_seed: int = 42

    def invoke(self, context, event):
        area = context.area
        if not area or area.type != 'DOPESHEET_EDITOR':
            self.report({'WARNING'}, "Works in Dope Sheet / Action Editor")
            return {'CANCELLED'}

        self.invert_grouping = event.shift
        self._order_mode = self.order_mode
        self._random_seed = self.random_seed

        ok = self._prepare_groups(context)
        if not ok:
            self.report({'WARNING'}, "No selected keyframes")
            return {'CANCELLED'}

        region = context.region
        if not region:
            return {'CANCELLED'}
        
        v2d = region.view2d
        x_view, _ = v2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
        self._initial_time = x_view
        self._region = region
        self._last_applied_delta = 0
        self._draw_mouse_x = event.mouse_region_x
        self._draw_mouse_y = event.mouse_region_y

        self._draw_handler = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
            _draw_stagger_feedback, (self, context), 'WINDOW', 'POST_PIXEL'
        )

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        self._draw_mouse_x = event.mouse_region_x
        self._draw_mouse_y = event.mouse_region_y

        if event.type in {'ESC', 'RIGHTMOUSE'}:
            self._apply_offset(0.0)
            self._finalize_updates()
            self._remove_draw_handler()
            return {'CANCELLED'}

        # Scroll changes random seed
        if self._order_mode == "RANDOM":
            if event.type == 'WHEELUPMOUSE':
                self._random_seed += 1
                self._reorder_groups_random()
                self._apply_offset(self._last_applied_delta or 0)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.type == 'WHEELDOWNMOUSE':
                self._random_seed -= 1
                self._reorder_groups_random()
                self._apply_offset(self._last_applied_delta or 0)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            region = self._region or context.region
            v2d = region.view2d
            x_view, _ = v2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
            delta_frames = round(x_view - (self._initial_time or 0.0))
            if self._last_applied_delta != delta_frames:
                self._apply_offset(delta_frames)
                context.area.tag_redraw()
                self._last_applied_delta = delta_frames
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'RELEASE':
            self._finalize_updates()
            self._remove_draw_handler()
            state.random_seed = self._random_seed
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def _remove_draw_handler(self):
        if self._draw_handler:
            bpy.types.SpaceDopeSheetEditor.draw_handler_remove(self._draw_handler, 'WINDOW')
            self._draw_handler = None

    def _prepare_groups(self, context) -> bool:
        """Build groups ordered by first selected keyframe time per channel."""
        owners_map = _action_owners_map()
        
        KeyItem = _KeyItem
        selected_items = []
        channel_first_time = {}
        
        for act in _gather_relevant_actions(context):
            fcurves = _get_fcurves_from_action(act)
            if not fcurves:
                continue
            owner = owners_map.get(act)
            for fc in fcurves:
                for kp in fc.keyframe_points:
                    if getattr(kp, "select_control_point", False):
                        selected_items.append(KeyItem(owner, act, fc, kp))
                        key = (id(act), id(fc))
                        frame = kp.co.x
                        if key not in channel_first_time or frame < channel_first_time[key]:
                            channel_first_time[key] = frame
        
        if not selected_items:
            return False
        
        # Grouping mode
        group_per_fcurve = True
        any_header_selected = any(getattr(it.fcurve, 'select', False) for it in selected_items)
        group_per_fcurve = any_header_selected
        if self.invert_grouping:
            group_per_fcurve = not group_per_fcurve
        
        group_items = {}
        fcurves_to_update = set()
        
        for it in selected_items:
            fcurves_to_update.add(it.fcurve)
            chan_key = it.channel_key_per_fcurve() if group_per_fcurve else it.channel_key_per_owner()
            if chan_key not in group_items:
                group_items[chan_key] = []
            group_items[chan_key].append(it)
        
        # Sort by earliest selected time
        def sort_key(chan_key):
            items = group_items[chan_key]
            return min(it.orig_co_x for it in items)
        
        groups_sorted = sorted(group_items.keys(), key=sort_key)
        self._groups_original = list(groups_sorted)
        
        if self._order_mode == "REVERSE":
            groups_sorted = list(reversed(groups_sorted))
        elif self._order_mode == "RANDOM":
            rng = random.Random(self._random_seed)
            groups_sorted = list(groups_sorted)
            rng.shuffle(groups_sorted)
        
        self._groups = groups_sorted
        self._group_items = group_items
        self._fcurves_to_update = fcurves_to_update
        return True

    def _reorder_groups_random(self):
        if self._groups_original:
            rng = random.Random(self._random_seed)
            self._groups = list(self._groups_original)
            rng.shuffle(self._groups)

    def _apply_offset(self, delta_frames: float):
        if not self._groups or not self._group_items:
            return
        for idx, gkey in enumerate(self._groups):
            group_offset = idx * delta_frames
            for it in self._group_items[gkey]:
                it.apply_offset(group_offset)

    def _finalize_updates(self):
        if not self._fcurves_to_update:
            return
        for fc in self._fcurves_to_update:
            try:
                fc.update()
            except:
                pass


# -----------------------------------------------------------------------------
# Key Item Class
# -----------------------------------------------------------------------------

class _KeyItem:
    def __init__(self, owner_obj, action, fcurve, keyframe_point):
        self.owner_obj = owner_obj
        self.action = action
        self.fcurve = fcurve
        self.kp = keyframe_point

        self.orig_co_x = float(keyframe_point.co.x)
        self.orig_hl_x = float(keyframe_point.handle_left.x)
        self.orig_hr_x = float(keyframe_point.handle_right.x)

        self.data_path = fcurve.data_path
        self.array_index = fcurve.array_index
        self.bone_name = _parse_bone_name_from_datapath(self.data_path)
        self.owner_name = getattr(owner_obj, "name", None)

    def channel_key_per_fcurve(self):
        return (self.owner_name, self.bone_name, self.data_path, self.array_index)

    def channel_key_per_owner(self):
        return (self.owner_name, self.bone_name, None, None)

    def apply_offset(self, offset_x: float):
        self.kp.co.x = self.orig_co_x + offset_x
        self.kp.handle_left.x = self.orig_hl_x + offset_x
        self.kp.handle_right.x = self.orig_hr_x + offset_x


# -----------------------------------------------------------------------------
# Preferences
# -----------------------------------------------------------------------------

class EZSTAGGER_Preferences(AddonPreferences):
    bl_idname = __package__

    def draw(self, context):
        layout = self.layout
        layout.label(text="EZ Stagger Offset")
        layout.label(text="Select keyframes in Dope Sheet, then click on the gizmos or Alt+Click to stagger.")


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------

classes = (
    EZSTAGGER_Preferences,
    EZSTAGGER_OT_stagger_modal,
    EZSTAGGER_OT_gizmo_click,
    EZSTAGGER_OT_hover,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    # Register draw handler
    handler = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
        draw_stagger_gizmos, (), 'WINDOW', 'POST_PIXEL'
    )
    state.draw_handlers['SpaceDopeSheetEditor'] = (bpy.types.SpaceDopeSheetEditor, handler)
    
    # Keymaps
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name='Dopesheet', space_type='DOPESHEET_EDITOR')
        
        # Alt+Click for direct stagger
        kmi = km.keymap_items.new(
            EZSTAGGER_OT_stagger_modal.bl_idname,
            type='LEFTMOUSE', value='PRESS', alt=True
        )
        addon_keymaps.append((km, kmi))
        
        # Click on gizmo
        kmi = km.keymap_items.new(
            EZSTAGGER_OT_gizmo_click.bl_idname,
            type='LEFTMOUSE', value='PRESS'
        )
        addon_keymaps.append((km, kmi))
        
        # Hover detection
        kmi = km.keymap_items.new(
            EZSTAGGER_OT_hover.bl_idname,
            type='MOUSEMOVE', value='ANY'
        )
        addon_keymaps.append((km, kmi))
        
        # Scroll for random seed
        kmi = km.keymap_items.new(
            EZSTAGGER_OT_hover.bl_idname,
            type='WHEELUPMOUSE', value='PRESS'
        )
        addon_keymaps.append((km, kmi))
        kmi = km.keymap_items.new(
            EZSTAGGER_OT_hover.bl_idname,
            type='WHEELDOWNMOUSE', value='PRESS'
        )
        addon_keymaps.append((km, kmi))


def unregister():
    # Remove keymaps
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except:
            pass
    addon_keymaps.clear()
    
    # Remove draw handlers
    for space_name, (space_type, handler) in state.draw_handlers.items():
        try:
            space_type.draw_handler_remove(handler, 'WINDOW')
        except:
            pass
    state.draw_handlers.clear()
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
