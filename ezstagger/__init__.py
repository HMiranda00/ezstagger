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
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.draw_handlers = {}
            cls._instance.enabled = True
            cls._instance.hovered = None
            cls._instance.random_seed = 42
            cls._instance.gizmo_positions = []
            cls._instance.has_selection = False
            cls._instance.bbox = None
            cls._instance.num_groups = 0
            # Ease values: 0.0 = flat, 1.0 = original curve
            cls._instance.ease_in = 1.0
            cls._instance.ease_out = 1.0
            cls._instance.hover_ease = None
            cls._instance.dragging_ease = None
            cls._instance.ease_slider_bounds = {}
        return cls._instance


state = StaggerWidgetState()
addon_keymaps = []


# -----------------------------------------------------------------------------
# Keyframe Data Collection
# -----------------------------------------------------------------------------

def _parse_bone_name(data_path: str) -> str | None:
    """Extract bone name from data path like 'pose.bones["BoneName"].location'"""
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
    """Get fcurves from action (Blender 5.0+ slotted actions API)."""
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
                try:
                    channelbag = strip.channelbag(slot)
                    if channelbag and hasattr(channelbag, 'fcurves') and channelbag.fcurves:
                        fcurves.extend(channelbag.fcurves)
                except:
                    pass
    return fcurves


def _collect_selected_keyframes():
    """
    Collect all selected keyframes from all objects.
    Returns list of dicts with all necessary info for manipulation.
    """
    selected = []
    
    try:
        for obj in bpy.context.view_layer.objects:
            ad = obj.animation_data
            if not ad or not ad.action:
                continue
            
            fcurves = _get_fcurves_from_action(ad.action)
            for fc in fcurves:
                bone_name = _parse_bone_name(fc.data_path)
                
                for kp_idx, kp in enumerate(fc.keyframe_points):
                    if kp.select_control_point:
                        selected.append({
                            'obj_name': obj.name,
                            'action_name': ad.action.name,
                            'data_path': fc.data_path,
                            'array_index': fc.array_index,
                            'bone_name': bone_name,
                            'kp_index': kp_idx,
                            'frame': float(kp.co.x),
                            'value': float(kp.co.y),
                            'hl_x': float(kp.handle_left.x),
                            'hl_y': float(kp.handle_left.y),
                            'hr_x': float(kp.handle_right.x),
                            'hr_y': float(kp.handle_right.y),
                            'hl_type': kp.handle_left_type,
                            'hr_type': kp.handle_right_type,
                        })
    except Exception as e:
        print(f"EZStagger: Error collecting keyframes: {e}")
    
    return selected


def _get_fcurve_and_keypoint(obj_name, action_name, data_path, array_index, kp_index):
    """Safely retrieve fcurve and keypoint by path info."""
    try:
        obj = bpy.data.objects.get(obj_name)
        if not obj or not obj.animation_data or not obj.animation_data.action:
            return None, None
        
        act = obj.animation_data.action
        # Don't check action name - it might have changed
        
        fcurves = _get_fcurves_from_action(act)
        for fc in fcurves:
            if fc.data_path == data_path and fc.array_index == array_index:
                if 0 <= kp_index < len(fc.keyframe_points):
                    return fc, fc.keyframe_points[kp_index]
        return None, None
    except Exception as e:
        print(f"EZStagger: Error getting keypoint: {e}")
        return None, None


def _calculate_current_ease():
    """
    Calculate current ease percentage for selected keyframes.
    Based on handle X distance relative to adjacent keyframe (timing/easing).
    Returns (ease_in, ease_out) where 0.0 = no ease (handle at keyframe), 1.0 = max ease (handle at adjacent).
    """
    ease_in_vals = []
    ease_out_vals = []
    
    try:
        for obj in bpy.context.view_layer.objects:
            ad = obj.animation_data
            if not ad or not ad.action:
                continue
            
            fcurves = _get_fcurves_from_action(ad.action)
            for fc in fcurves:
                kps = fc.keyframe_points
                for i, kp in enumerate(kps):
                    if not kp.select_control_point:
                        continue
                    
                    frame = kp.co.x
                    
                    # Ease IN: how far left handle X extends toward previous keyframe
                    if i > 0:
                        prev_frame = kps[i - 1].co.x
                        max_range = frame - prev_frame  # Total distance to previous
                        if max_range > 0.001:
                            handle_dist = frame - kp.handle_left.x  # How far handle extends left
                            ease = handle_dist / max_range
                            ease_in_vals.append(max(0.0, min(1.0, ease)))
                    
                    # Ease OUT: how far right handle X extends toward next keyframe
                    if i < len(kps) - 1:
                        next_frame = kps[i + 1].co.x
                        max_range = next_frame - frame  # Total distance to next
                        if max_range > 0.001:
                            handle_dist = kp.handle_right.x - frame  # How far handle extends right
                            ease = handle_dist / max_range
                            ease_out_vals.append(max(0.0, min(1.0, ease)))
    except:
        pass
    
    ein = sum(ease_in_vals) / len(ease_in_vals) if ease_in_vals else 0.33
    eout = sum(ease_out_vals) / len(ease_out_vals) if ease_out_vals else 0.33
    
    return ein, eout


def _determine_grouping(selected_items):
    """
    Determine grouping based on selection:
    - If multiple objects/bones selected → group by object+bone
    - If single object/bone → group by channel (fcurve)
    
    Returns: list of groups, where each group is a list of items
    """
    if not selected_items:
        return []
    
    # Count unique object+bone combinations
    owners = set()
    for item in selected_items:
        bone = item['bone_name'] or ""
        owner_key = (item['obj_name'], bone)
        owners.add(owner_key)
    
    # Decide grouping mode
    if len(owners) > 1:
        # Multiple objects/bones → group by object+bone
        groups = {}
        for item in selected_items:
            bone = item['bone_name'] or ""
            key = (item['obj_name'], bone)
            if key not in groups:
                groups[key] = []
            groups[key].append(item)
    else:
        # Single object/bone → group by channel (data_path + array_index)
        groups = {}
        for item in selected_items:
            key = (item['obj_name'], item['data_path'], item['array_index'])
            if key not in groups:
                groups[key] = []
            groups[key].append(item)
    
    # Sort groups by earliest keyframe time
    sorted_keys = sorted(groups.keys(), key=lambda k: min(it['frame'] for it in groups[k]))
    
    return [groups[k] for k in sorted_keys]


# -----------------------------------------------------------------------------
# Selection Detection for Drawing
# -----------------------------------------------------------------------------

def _get_selection_info():
    """Get bbox and group count for current selection."""
    context = bpy.context
    
    if not context.area or context.area.type != 'DOPESHEET_EDITOR':
        return None, 0
    
    region = context.region
    if not region:
        return None, 0
    
    selected = _collect_selected_keyframes()
    if not selected:
        return None, 0
    
    groups = _determine_grouping(selected)
    num_groups = len(groups)
    
    # Calculate bbox
    min_frame = min(it['frame'] for it in selected)
    max_frame = max(it['frame'] for it in selected)
    
    v2d = region.view2d
    x1, _ = v2d.view_to_region(min_frame, 0, clip=False)
    x2, _ = v2d.view_to_region(max_frame, 0, clip=False)
    
    y_center = region.height / 2
    y_extent = 30
    
    return (x1 - 10, x2 + 10, y_center - y_extent, y_center + y_extent), num_groups


# -----------------------------------------------------------------------------
# Drawing
# -----------------------------------------------------------------------------

def _draw_rounded_rect(shader, x, y, w, h, color, radius=4):
    if w < 2 or h < 2:
        return
    radius = min(radius, w / 2, h / 2)
    
    if radius <= 1:
        verts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        batch = batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=[(0, 1, 2), (0, 2, 3)])
        shader.uniform_float("color", color)
        batch.draw(shader)
        return
    
    verts = []
    segs = 4
    corners = [
        (x + radius, y + radius, math.pi, 1.5 * math.pi),
        (x + w - radius, y + radius, 1.5 * math.pi, 2 * math.pi),
        (x + w - radius, y + h - radius, 0, 0.5 * math.pi),
        (x + radius, y + h - radius, 0.5 * math.pi, math.pi),
    ]
    for cx, cy, a1, a2 in corners:
        for i in range(segs + 1):
            t = i / segs
            a = a1 + t * (a2 - a1)
            verts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    
    center = (x + w / 2, y + h / 2)
    verts.insert(0, center)
    indices = [(0, i, i + 1) for i in range(1, len(verts) - 1)]
    indices.append((0, len(verts) - 1, 1))
    
    batch = batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=indices)
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_circle(shader, x, y, r, color, segs=16):
    verts = [(x, y)]
    for i in range(segs + 1):
        a = 2 * math.pi * i / segs
        verts.append((x + r * math.cos(a), y + r * math.sin(a)))
    indices = [(0, i + 1, (i + 1) % segs + 1) for i in range(segs)]
    batch = batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=indices)
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_circle_outline(shader, x, y, r, color, width=2):
    verts = []
    for i in range(25):
        a = 2 * math.pi * i / 24
        verts.append((x + r * math.cos(a), y + r * math.sin(a)))
    batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": verts})
    shader.uniform_float("color", color)
    gpu.state.line_width_set(width)
    batch.draw(shader)
    gpu.state.line_width_set(1.0)


def _brighten(color, f=0.15):
    return tuple(min(1.0, c + f) for c in color[:3]) + (min(1.0, color[3] + 0.1),)


def draw_widgets():
    """Main draw callback."""
    ctx = bpy.context
    if not ctx or not state.enabled:
        return
    if not ctx.area or ctx.area.type != 'DOPESHEET_EDITOR':
        return
    if not ctx.region:
        return
    
    bbox, num_groups = _get_selection_info()
    state.num_groups = num_groups
    
    if not bbox or num_groups == 0:
        state.has_selection = False
        state.gizmo_positions = []
        state.ease_slider_bounds = {}
        return
    
    state.has_selection = True
    state.bbox = bbox
    x1, x2, y1, y2 = bbox
    mid_y = (y1 + y2) / 2
    
    gpu.state.blend_set('ALPHA')
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.bind()
    
    current_x = x2 + 18
    font_id = 0
    
    # === STAGGER GIZMOS (if 2+ groups) ===
    if num_groups >= 2:
        r = 6
        sp = 22
        top_y, bot_y = mid_y + sp, mid_y - sp
        
        positions = [
            (current_x, top_y, "REVERSE"),
            (current_x, mid_y, "RANDOM"),
            (current_x, bot_y, "NORMAL"),
        ]
        state.gizmo_positions = positions
        
        cols = {"NORMAL": (0.35, 0.75, 0.45, 0.95), "REVERSE": (0.75, 0.45, 0.35, 0.95), "RANDOM": (0.55, 0.45, 0.75, 0.95)}
        labels = {"NORMAL": "▼", "REVERSE": "▲", "RANDOM": "◆"}
        
        # Line
        batch = batch_for_shader(shader, 'LINES', {"pos": [(current_x, bot_y), (current_x, top_y)]})
        shader.uniform_float("color", (0.4, 0.4, 0.4, 0.6))
        gpu.state.line_width_set(2)
        batch.draw(shader)
        gpu.state.line_width_set(1)
        
        for gx, gy, mode in positions:
            col = _brighten(cols[mode], 0.2) if state.hovered == mode else cols[mode]
            _draw_circle(shader, gx, gy, r, col)
            if state.hovered == mode:
                _draw_circle_outline(shader, gx, gy, r + 2, (1, 1, 1, 1), 2)
            blf.size(font_id, 8)
            blf.color(font_id, 1, 1, 1, 1)
            dim = blf.dimensions(font_id, labels[mode])
            blf.position(font_id, gx - dim[0] / 2, gy - dim[1] / 2 + 1, 0)
            blf.draw(font_id, labels[mode])
        
        # Tooltip
        if state.hovered:
            tips = {"NORMAL": "Top→Bot", "REVERSE": "Bot→Top", "RANDOM": f"Rnd:{state.random_seed}"}
            tip = tips.get(state.hovered, "")
            for gx, gy, m in positions:
                if m == state.hovered:
                    blf.size(font_id, 9)
                    dim = blf.dimensions(font_id, tip)
                    pad = 4
                    bx = gx + 12
                    by = gy - dim[1] / 2 - pad
                    _draw_rounded_rect(shader, bx, by, dim[0] + pad * 2, dim[1] + pad * 2, (0.1, 0.1, 0.1, 0.9), 3)
                    blf.color(font_id, 0.9, 0.9, 0.9, 1)
                    blf.position(font_id, bx + pad, gy - dim[1] / 2, 0)
                    blf.draw(font_id, tip)
                    break
        
        current_x += 22
    else:
        state.gizmo_positions = []
    
    # === EASE SLIDERS ===
    # Update ease values from current selection (unless dragging)
    if not state.dragging_ease:
        state.ease_in, state.ease_out = _calculate_current_ease()
    
    sw, sh, ssp = 80, 14, 6
    pad, lbl_w = 8, 22
    pw = sw + pad * 2 + lbl_w + 30  # Extra space for percentage
    ph = sh * 2 + ssp + pad * 2
    px = current_x + 8
    pb = mid_y - ph / 2
    
    _draw_rounded_rect(shader, px, pb, pw, ph, (0.12, 0.12, 0.12, 0.85), 5)
    
    slx = px + pad + lbl_w
    sly_out = pb + pad
    sly_in = sly_out + sh + ssp
    
    blf.size(font_id, 9)
    blf.color(font_id, 0.5, 0.8, 0.5, 1)
    blf.position(font_id, px + pad, sly_in + 3, 0)
    blf.draw(font_id, "In")
    blf.color(font_id, 0.8, 0.5, 0.5, 1)
    blf.position(font_id, px + pad, sly_out + 3, 0)
    blf.draw(font_id, "Out")
    
    def slider(sx, sy, w, h, val, hov, accent, label):
        # val: 0.0 = flat, 1.0 = full curve
        _draw_rounded_rect(shader, sx, sy, w, h, (0.25, 0.25, 0.25, 0.9) if hov else (0.2, 0.2, 0.2, 0.9), 3)
        # Fill bar from left based on value
        if val > 0:
            fw = w * val
            _draw_rounded_rect(shader, sx, sy + 1, fw, h - 2, accent, 2)
        # Thumb position
        tx = sx + w * val
        _draw_circle(shader, tx, sy + h / 2, 5, (0.95, 0.95, 0.95, 1) if hov else (0.75, 0.75, 0.75, 1))
        # Percentage text
        pct = f"{int(val * 100)}%"
        blf.size(font_id, 8)
        blf.color(font_id, 0.7, 0.7, 0.7, 1)
        blf.position(font_id, sx + w + 6, sy + 3, 0)
        blf.draw(font_id, pct)
    
    slider(slx, sly_in, sw, sh, state.ease_in, state.hover_ease == "EASE_IN", (0.3, 0.55, 0.3, 0.8), "In")
    slider(slx, sly_out, sw, sh, state.ease_out, state.hover_ease == "EASE_OUT", (0.55, 0.3, 0.3, 0.8), "Out")
    state.ease_slider_bounds = {"EASE_IN": (slx, sly_in, sw, sh), "EASE_OUT": (slx, sly_out, sw, sh)}
    
    gpu.state.blend_set('NONE')


def _draw_feedback(op, ctx):
    if op._last_delta is None:
        return
    x, y = op._mouse_x, op._mouse_y
    delta = int(op._last_delta)
    groups = len(op._groups) if op._groups else 0
    mode = op._mode
    icons = {"NORMAL": "▼", "REVERSE": "▲", "RANDOM": "◆"}
    text = f"{icons.get(mode, '')} {delta:+d}f × {groups}"
    sub = f"seed {op._seed} (scroll)" if mode == "RANDOM" else None
    
    font_id = 0
    blf.size(font_id, 14)
    dim1 = blf.dimensions(font_id, text)
    pad = 8
    bx = x + 22
    
    gpu.state.blend_set('ALPHA')
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.bind()
    
    if sub:
        blf.size(font_id, 10)
        dim2 = blf.dimensions(font_id, sub)
        mw = max(dim1[0], dim2[0])
        th = dim1[1] + dim2[1] + 4
        by = y - th / 2 - pad
        _draw_rounded_rect(shader, bx, by, mw + pad * 2, th + pad * 2, (0.08, 0.08, 0.08, 0.9), 5)
        blf.size(font_id, 14)
        blf.color(font_id, 1, 1, 1, 1)
        blf.position(font_id, bx + pad, by + pad + dim2[1] + 4, 0)
        blf.draw(font_id, text)
        blf.size(font_id, 10)
        blf.color(font_id, 0.55, 0.55, 0.55, 1)
        blf.position(font_id, bx + pad, by + pad, 0)
        blf.draw(font_id, sub)
    else:
        by = y - dim1[1] / 2 - pad
        _draw_rounded_rect(shader, bx, by, dim1[0] + pad * 2, dim1[1] + pad * 2, (0.08, 0.08, 0.08, 0.9), 5)
        blf.size(font_id, 14)
        blf.color(font_id, 1, 1, 1, 1)
        blf.position(font_id, bx + pad, by + pad, 0)
        blf.draw(font_id, text)
    
    gpu.state.blend_set('NONE')


# -----------------------------------------------------------------------------
# Hit Detection
# -----------------------------------------------------------------------------

def check_gizmo(mx, my):
    if not state.has_selection or state.num_groups < 2:
        return None
    for gx, gy, mode in state.gizmo_positions:
        if (mx - gx) ** 2 + (my - gy) ** 2 <= 100:
            return mode
    return None


def check_ease(mx, my):
    if not state.has_selection:
        return None
    for sid, (sx, sy, sw, sh) in state.ease_slider_bounds.items():
        if sx <= mx <= sx + sw and sy <= my <= sy + sh:
            return sid
    return None


# -----------------------------------------------------------------------------
# Operators
# -----------------------------------------------------------------------------

class EZSTAGGER_OT_hover(Operator):
    bl_idname = "anim.ez_stagger_hover"
    bl_label = "Hover"
    bl_options = {'INTERNAL'}
    
    def invoke(self, context, event):
        if not state.enabled or not context.region:
            return {'PASS_THROUGH'}
        
        mx, my = event.mouse_region_x, event.mouse_region_y
        if not (0 <= mx <= context.region.width and 0 <= my <= context.region.height):
            if state.hovered or state.hover_ease:
                state.hovered = state.hover_ease = None
                context.area.tag_redraw()
            return {'PASS_THROUGH'}
        
        old_h, old_e = state.hovered, state.hover_ease
        state.hovered = check_gizmo(mx, my)
        state.hover_ease = check_ease(mx, my)
        
        if state.hovered == "RANDOM":
            if event.type == 'WHEELUPMOUSE':
                state.random_seed += 1
                context.area.tag_redraw()
            elif event.type == 'WHEELDOWNMOUSE':
                state.random_seed -= 1
                context.area.tag_redraw()
        
        context.window.cursor_set('HAND' if state.hovered or state.hover_ease else 'DEFAULT')
        if old_h != state.hovered or old_e != state.hover_ease:
            context.area.tag_redraw()
        return {'PASS_THROUGH'}


class EZSTAGGER_OT_click(Operator):
    bl_idname = "anim.ez_stagger_click"
    bl_label = "Click"
    bl_options = {'INTERNAL'}
    
    def invoke(self, context, event):
        if not state.enabled or not state.has_selection:
            return {'PASS_THROUGH'}
        
        mx, my = event.mouse_region_x, event.mouse_region_y
        
        mode = check_gizmo(mx, my)
        if mode:
            bpy.ops.anim.ez_stagger_modal('INVOKE_DEFAULT', order_mode=mode, random_seed=state.random_seed)
            return {'FINISHED'}
        
        ease = check_ease(mx, my)
        if ease:
            bpy.ops.anim.ez_ease_modal('INVOKE_DEFAULT', ease_type=ease)
            return {'FINISHED'}
        
        return {'PASS_THROUGH'}


class EZSTAGGER_OT_ease(Operator):
    bl_idname = "anim.ez_ease_modal"
    bl_label = "Ease"
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}
    
    ease_type: EnumProperty(items=(("EASE_IN", "", ""), ("EASE_OUT", "", "")), default="EASE_IN")
    
    _bounds: tuple = (0, 0, 0, 0)
    _data: list = []
    _start_val: float = 1.0
    
    def invoke(self, context, event):
        if self.ease_type not in state.ease_slider_bounds:
            return {'CANCELLED'}
        
        self._bounds = state.ease_slider_bounds[self.ease_type]
        self._data = _collect_selected_keyframes()
        if not self._data:
            return {'CANCELLED'}
        
        # Store starting value
        self._start_val = state.ease_in if self.ease_type == "EASE_IN" else state.ease_out
        
        state.dragging_ease = self.ease_type
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def modal(self, context, event):
        sx, sy, sw, sh = self._bounds
        
        if event.type == 'MOUSEMOVE':
            # Map mouse position to 0-1 range
            val = max(0.0, min(1.0, (event.mouse_region_x - sx) / sw))
            if self.ease_type == "EASE_IN":
                state.ease_in = val
            else:
                state.ease_out = val
            
            self._apply_ease()
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            state.dragging_ease = None
            context.area.tag_redraw()
            return {'FINISHED'}
        
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self._restore()
            state.dragging_ease = None
            # Restore visual state
            if self.ease_type == "EASE_IN":
                state.ease_in = self._start_val
            else:
                state.ease_out = self._start_val
            context.area.tag_redraw()
            return {'CANCELLED'}
        
        return {'RUNNING_MODAL'}
    
    def _apply_ease(self):
        """
        Apply ease by adjusting handle X position (timing).
        ease_factor: 0.0 = handle at keyframe (no ease), 1.0 = handle extended toward adjacent
        """
        is_in = self.ease_type == "EASE_IN"
        factor = state.ease_in if is_in else state.ease_out
        fcs = set()
        
        for it in self._data:
            fc, kp = _get_fcurve_and_keypoint(
                it['obj_name'], it['action_name'], 
                it['data_path'], it['array_index'], it['kp_index']
            )
            if not fc or not kp:
                continue
            
            # Force handle type to FREE so we can modify them
            kp.handle_left_type = 'FREE'
            kp.handle_right_type = 'FREE'
            
            frame = it['frame']
            kp_idx = it['kp_index']
            kps = fc.keyframe_points
            
            if is_in and kp_idx > 0:
                # Get previous keyframe frame
                prev_frame = kps[kp_idx - 1].co.x
                max_dist = frame - prev_frame  # Max distance to extend left
                # Interpolate X: 0 = at keyframe, 1 = extended toward previous
                kp.handle_left.x = frame - (max_dist * factor)
                # Keep Y unchanged
                kp.handle_left.y = it['hl_y']
            elif not is_in and kp_idx < len(kps) - 1:
                # Get next keyframe frame
                next_frame = kps[kp_idx + 1].co.x
                max_dist = next_frame - frame  # Max distance to extend right
                # Interpolate X: 0 = at keyframe, 1 = extended toward next
                kp.handle_right.x = frame + (max_dist * factor)
                # Keep Y unchanged
                kp.handle_right.y = it['hr_y']
            
            fcs.add(fc)
        
        for fc in fcs:
            try:
                fc.update()
            except:
                pass
    
    def _restore(self):
        fcs = set()
        for it in self._data:
            fc, kp = _get_fcurve_and_keypoint(
                it['obj_name'], it['action_name'], 
                it['data_path'], it['array_index'], it['kp_index']
            )
            if not fc or not kp:
                continue
            # Restore original handle types
            kp.handle_left_type = it.get('hl_type', 'FREE')
            kp.handle_right_type = it.get('hr_type', 'FREE')
            kp.handle_left.x = it['hl_x']
            kp.handle_left.y = it['hl_y']
            kp.handle_right.x = it['hr_x']
            kp.handle_right.y = it['hr_y']
            fcs.add(fc)
        for fc in fcs:
            try:
                fc.update()
            except:
                pass


class EZSTAGGER_OT_stagger(Operator):
    bl_idname = "anim.ez_stagger_modal"
    bl_label = "Stagger"
    bl_options = {"REGISTER", "UNDO", "GRAB_CURSOR", "BLOCKING"}

    order_mode: EnumProperty(items=(("NORMAL", "", ""), ("REVERSE", "", ""), ("RANDOM", "", "")), default="NORMAL")
    random_seed: IntProperty(default=42)

    _init_time: float = 0
    _region = None
    _groups: list = []
    _groups_orig: list = []
    _last_delta: float = None
    _handler = None
    _mouse_x: int = 0
    _mouse_y: int = 0
    _mode: str = "NORMAL"
    _seed: int = 42

    def invoke(self, context, event):
        if not context.area or context.area.type != 'DOPESHEET_EDITOR':
            return {'CANCELLED'}
        
        self._mode = self.order_mode
        self._seed = self.random_seed
        
        selected = _collect_selected_keyframes()
        if not selected:
            return {'CANCELLED'}
        
        self._groups_orig = _determine_grouping(selected)
        if not self._groups_orig:
            return {'CANCELLED'}
        
        self._groups = list(self._groups_orig)
        if self._mode == "REVERSE":
            self._groups = list(reversed(self._groups))
        elif self._mode == "RANDOM":
            rng = random.Random(self._seed)
            self._groups = list(self._groups_orig)
            rng.shuffle(self._groups)
        
        self._region = context.region
        v2d = self._region.view2d
        self._init_time, _ = v2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
        self._last_delta = 0
        self._mouse_x = event.mouse_region_x
        self._mouse_y = event.mouse_region_y
        
        self._handler = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
            _draw_feedback, (self, bpy.context), 'WINDOW', 'POST_PIXEL'
        )
        
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        self._mouse_x = event.mouse_region_x
        self._mouse_y = event.mouse_region_y

        if event.type in {'ESC', 'RIGHTMOUSE'}:
            self._apply(0)
            self._cleanup()
            return {'CANCELLED'}

        if self._mode == "RANDOM":
            if event.type == 'WHEELUPMOUSE':
                self._seed += 1
                self._reshuffle()
                self._apply(self._last_delta or 0)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.type == 'WHEELDOWNMOUSE':
                self._seed -= 1
                self._reshuffle()
                self._apply(self._last_delta or 0)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            v2d = (self._region or context.region).view2d
            t, _ = v2d.region_to_view(event.mouse_region_x, event.mouse_region_y)
            delta = round(t - self._init_time)
            if self._last_delta != delta:
                self._apply(delta)
                context.area.tag_redraw()
                self._last_delta = delta
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'RELEASE':
            self._cleanup()
            state.random_seed = self._seed
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def _cleanup(self):
        if self._handler:
            try:
                bpy.types.SpaceDopeSheetEditor.draw_handler_remove(self._handler, 'WINDOW')
            except:
                pass
            self._handler = None

    def _reshuffle(self):
        rng = random.Random(self._seed)
        self._groups = list(self._groups_orig)
        rng.shuffle(self._groups)

    def _apply(self, delta):
        fcs = set()
        for idx, group in enumerate(self._groups):
            offset = idx * delta
            for it in group:
                fc, kp = _get_fcurve_and_keypoint(it['obj_name'], it['action_name'], it['data_path'], it['array_index'], it['kp_index'])
                if not fc or not kp:
                    continue
                kp.co.x = it['frame'] + offset
                kp.handle_left.x = it['hl_x'] + offset
                kp.handle_right.x = it['hr_x'] + offset
                fcs.add(fc)
        for fc in fcs:
            try:
                fc.update()
            except:
                pass


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------

class EZSTAGGER_Prefs(AddonPreferences):
    bl_idname = __package__
    def draw(self, context):
        self.layout.label(text="EZ Stagger - Select keyframes to see widgets")


classes = (EZSTAGGER_Prefs, EZSTAGGER_OT_stagger, EZSTAGGER_OT_ease, EZSTAGGER_OT_click, EZSTAGGER_OT_hover)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    h = bpy.types.SpaceDopeSheetEditor.draw_handler_add(draw_widgets, (), 'WINDOW', 'POST_PIXEL')
    state.draw_handlers['main'] = (bpy.types.SpaceDopeSheetEditor, h)
    
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name='Dopesheet', space_type='DOPESHEET_EDITOR')
        for op, t, v, kw in [
            (EZSTAGGER_OT_stagger.bl_idname, 'LEFTMOUSE', 'PRESS', {'alt': True}),
            (EZSTAGGER_OT_click.bl_idname, 'LEFTMOUSE', 'PRESS', {}),
            (EZSTAGGER_OT_hover.bl_idname, 'MOUSEMOVE', 'ANY', {}),
            (EZSTAGGER_OT_hover.bl_idname, 'WHEELUPMOUSE', 'PRESS', {}),
            (EZSTAGGER_OT_hover.bl_idname, 'WHEELDOWNMOUSE', 'PRESS', {}),
        ]:
            kmi = km.keymap_items.new(op, type=t, value=v, **kw)
            addon_keymaps.append((km, kmi))


def unregister():
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except:
            pass
    addon_keymaps.clear()
    
    for _, (st, h) in state.draw_handlers.items():
        try:
            st.draw_handler_remove(h, 'WINDOW')
        except:
            pass
    state.draw_handlers.clear()
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
