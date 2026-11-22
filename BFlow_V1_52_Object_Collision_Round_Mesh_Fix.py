bl_info = {
    "name": "B-Flow Particle System",
    "author": "OpenAI",
    "version": (1, 52, 0),
    "blender": (5, 2, 0),
    "location": "Node Editor > B-Flow",
    "description": "Gate particle movement by its stored birth frame",
    "category": "Node",
}

import bpy
import uuid
from mathutils import Quaternion, Vector
from bpy.types import (
    NodeTree, Node, NodeSocket, Operator, Panel, PropertyGroup
)
from bpy.props import (
    StringProperty, IntProperty, FloatProperty, FloatVectorProperty,
    PointerProperty, EnumProperty, CollectionProperty, BoolProperty
)

# ---------------------------------------------------------------------------
# Operator catalogue
# ---------------------------------------------------------------------------

OPERATOR_ITEMS = [
    ("BIRTH", "Birth", "Create particles"),
    ("POSITION_OBJECT", "Position Object", "Emit particles from a mesh object"),
    ("SPEED", "Speed", "Set initial particle velocity"),
    ("GRAVITY", "Gravity", "Apply gravity"),
    ("FORCE", "Force", "Apply selected Blender Force Field objects"),
    ("KEEP_DIST", "Keep Dist", "Softly repel particles that are closer than a set distance"),
    ("ROTATION", "Rotation", "Set random initial particle orientation"),
    ("SPIN", "Spin", "Apply per-particle angular motion"),
    ("COLLISION", "Ground Collision", "Collide with a horizontal ground plane"),
    ("OBJECT_COLLISION", "Object Collision", "Collide with a selected mesh object"),
    ("SHAPE_INSTANCE", "Shape Instance", "Replace particle display with a custom object"),
    ("RAND_COLOR", "Rand Color", "Assign weighted random colors to particle instances"),
    ("SIZE", "Size", "Set fixed size or animate size over real/particle time"),
    ("OPACITY", "Opacity", "Fade particle instances using fixed, real, or particle time"),
    ("DELETE", "DEL", "Delete particles by scene time, particle age, or collision"),
    ("RIGID_BODY", "Rigid Body", "Convert B-Flow particles to native rigid bodies while preserving per-particle colors"),
]


# ---------------------------------------------------------------------------
# Developer-controlled Operator icons
# ---------------------------------------------------------------------------
# Change the icon string on the right to any valid Blender built-in icon ID.
# These are developer defaults only; no icon picker is exposed to add-on users.
#
# Official icon reference (Blender 5.2):
# https://docs.blender.org/api/5.2/bpy_types_enum_items/icon_items.html
#
# Example:
#     "SPEED": "EMPTY_ARROWS",
# Change to:
#     "SPEED": "TIME",
#
OPERATOR_ICON_DEFAULTS = {
    "BIRTH": "PARTICLES",
    "POSITION_OBJECT": "SHAPEKEY_DATA",
    "SPEED": "EMPTY_ARROWS",
    "GRAVITY": "SORT_ASC",
    "FORCE": "FORCE_FORCE",
    "KEEP_DIST": "MESH_CIRCLE",
    "ROTATION": "ORIENTATION_GIMBAL",
    "SPIN": "FORCE_VORTEX",
    "COLLISION": "INDIRECT_ONLY_OFF",
    "OBJECT_COLLISION": "MESH_MONKEY",
    "SHAPE_INSTANCE": "MESH_ICOSPHERE",
    "RAND_COLOR": "COLOR",
    "SIZE": "CON_SIZELIMIT",
    "OPACITY": "MOD_OPACITY",
    "DELETE": "TRASH",
    "RIGID_BODY": "ASSET_MANAGER",
}


def _bflow_set_vector_rotate_euler(node):
    """Configure Blender's Vector Rotate node using the Blender 5.2 enum."""
    try:
        node.rotation_type = 'EULER_XYZ'
    except (TypeError, ValueError):
        # Compatibility fallback for builds exposing a different Euler label.
        enum_items = node.bl_rna.properties["rotation_type"].enum_items
        available = {entry.identifier for entry in enum_items}
        for candidate in ("EULER_XYZ", "EULER"):
            if candidate in available:
                node.rotation_type = candidate
                return
        raise RuntimeError(
            "B-Flow: this Blender build has no supported Euler rotation mode "
            f"for Vector Rotate (available: {', '.join(sorted(available))})"
        )


def operator_icon_for_item(item):
    """Return the icon chosen by the B-Flow developer for this Operator."""
    return OPERATOR_ICON_DEFAULTS.get(item.operator_type, "DOT")


def operator_label(identifier):
    for item in OPERATOR_ITEMS:
        if item[0] == identifier:
            return item[1]
    return identifier.replace("_", " ").title()

def operator_category(identifier):
    if identifier.startswith("BIRTH") or identifier.startswith("POSITION"):
        return "BIRTH"
    if identifier in {"AGE", "COLLISION", "OBJECT_COLLISION", "SPEED_TEST", "DISTANCE_TEST", "RANDOM_TEST"}:
        return "TEST"
    if identifier in {"DELETE", "SPAWN", "DISPLAY", "MATERIAL"}:
        return "OUTPUT"
    if identifier in {"SCALE", "SCALE_OVER_AGE", "SIZE", "OPACITY", "COLOR", "RAND_COLOR", "ROTATION", "SPIN"}:
        return "PROPERTY"
    return "FORCE"

CATEGORY_COLORS = {
    "BIRTH": (0.18, 0.48, 0.23),
    "FORCE": (0.18, 0.32, 0.58),
    "TEST": (0.62, 0.34, 0.12),
    "PROPERTY": (0.38, 0.25, 0.58),
    "OUTPUT": (0.48, 0.24, 0.24),
}

# ---------------------------------------------------------------------------
# Debounced live recompilation
# ---------------------------------------------------------------------------

_BFLOW_LIVE_UPDATE_PENDING = False
_BFLOW_LIVE_UPDATE_RUNNING = False

def _bflow_opacity_start_frame_update(self, context):
    """Keep End Frame at or after Start Frame automatically."""
    if self.opacity_end_frame < self.opacity_start_frame:
        self.opacity_end_frame = self.opacity_start_frame
    _bflow_schedule_live_update(self, context)


def _bflow_opacity_end_frame_update(self, context):
    """Clamp End Frame upward so it can never precede Start Frame."""
    if self.opacity_end_frame < self.opacity_start_frame:
        self.opacity_end_frame = self.opacity_start_frame
    _bflow_schedule_live_update(self, context)


def _bflow_size_start_time_update(self, context):
    if self.size_end_time <= self.size_start_time:
        self.size_end_time = self.size_start_time + 0.001
    _bflow_schedule_live_update(self, context)


def _bflow_size_end_time_update(self, context):
    if self.size_end_time <= self.size_start_time:
        self.size_start_time = max(0.0, self.size_end_time - 0.001)
    _bflow_schedule_live_update(self, context)


def _bflow_schedule_live_update(self, context):
    """Schedule one compile after the user pauses parameter editing."""
    global _BFLOW_LIVE_UPDATE_PENDING
    if _BFLOW_LIVE_UPDATE_RUNNING:
        return
    if not getattr(self, "enabled", True):
        # Disabling an operator is itself an update; still compile the stack.
        pass
    if _BFLOW_LIVE_UPDATE_PENDING:
        return
    _BFLOW_LIVE_UPDATE_PENDING = True

    def _run_update():
        global _BFLOW_LIVE_UPDATE_PENDING, _BFLOW_LIVE_UPDATE_RUNNING
        _BFLOW_LIVE_UPDATE_PENDING = False
        scene = bpy.context.scene
        if scene is None or not getattr(scene, "bflow_graph", None):
            return None
        try:
            _BFLOW_LIVE_UPDATE_RUNNING = True
            bpy.ops.bflow.compile_simulation()
        except Exception as exc:
            print("[B-Flow] Live update failed:", exc)
        finally:
            _BFLOW_LIVE_UPDATE_RUNNING = False
        return None

    try:
        bpy.app.timers.register(_run_update, first_interval=0.65)
    except Exception as exc:
        _BFLOW_LIVE_UPDATE_PENDING = False
        print("[B-Flow] Could not schedule live update:", exc)


_BFLOW_RAND_COLOR_REBALANCING = False


def _bflow_find_rand_color_parent(entry):
    """Find the operator item that owns a Rand Color palette entry."""
    try:
        target_pointer = entry.as_pointer()
    except Exception:
        return None, None

    for tree in bpy.data.node_groups:
        if getattr(tree, "bl_idname", "") != "BFLOW_Tree":
            continue
        for node in tree.nodes:
            if getattr(node, "bl_idname", "") != "BFLOW_EventNode":
                continue
            for operator_item in node.operator_items:
                for candidate in operator_item.rand_color_entries:
                    try:
                        if candidate.as_pointer() == target_pointer:
                            return operator_item, candidate
                    except Exception:
                        continue
    return None, None


def _bflow_rebalance_rand_color_item(item, make_equal=False, edited_entry=None):
    """Keep all palette entries summing to 100%.

    Adding/removing entries uses an equal split.
    Editing one entry preserves that value and redistributes the remainder
    proportionally across the other entries (or equally if their prior total
    was zero).
    """
    global _BFLOW_RAND_COLOR_REBALANCING
    if _BFLOW_RAND_COLOR_REBALANCING:
        return
    entries = list(item.rand_color_entries)
    if not entries:
        return

    _BFLOW_RAND_COLOR_REBALANCING = True
    try:
        if make_equal or len(entries) == 1:
            share = 100.0 / len(entries)
            for entry in entries:
                entry.percentage = share
            return

        if edited_entry is None:
            total = sum(max(0.0, float(entry.percentage)) for entry in entries)
            if total <= 0.000001:
                share = 100.0 / len(entries)
                for entry in entries:
                    entry.percentage = share
            else:
                for entry in entries:
                    entry.percentage = max(0.0, float(entry.percentage)) * 100.0 / total
            return

        edited_value = max(0.0, min(100.0, float(edited_entry.percentage)))
        edited_entry.percentage = edited_value
        others = [entry for entry in entries if entry.as_pointer() != edited_entry.as_pointer()]
        if not others:
            edited_entry.percentage = 100.0
            return

        remaining = max(0.0, 100.0 - edited_value)
        previous_weights = [max(0.0, float(entry.percentage)) for entry in others]
        weight_total = sum(previous_weights)

        if weight_total <= 0.000001:
            share = remaining / len(others)
            for entry in others:
                entry.percentage = share
        else:
            for entry, weight in zip(others, previous_weights):
                entry.percentage = remaining * weight / weight_total
    finally:
        _BFLOW_RAND_COLOR_REBALANCING = False


def _bflow_rand_color_percentage_update(self, context):
    """Rebalance the remaining palette entries after a percentage edit."""
    if _BFLOW_RAND_COLOR_REBALANCING:
        return
    parent_item, entry = _bflow_find_rand_color_parent(self)
    if parent_item is None or entry is None:
        return
    _bflow_rebalance_rand_color_item(parent_item, edited_entry=entry)
    _bflow_schedule_live_update(parent_item, context)


def _bflow_enable_opacity_on_material(material, owner_name):
    """Wrap each active material surface in a Transparent/Surface mix.

    The wrapper is persistent and idempotent so recompiling B-Flow never
    duplicates the opacity nodes or overwrites user edits.

    The Instancer Attribute node reads the per-particle 'bflow_opacity'
    attribute stored on the Geometry Nodes points. Original material datablocks
    are not changed; this function is only used on dedicated material copies.
    """
    if (
        material.get("bflow_opacity_material", False)
        and material.get("bflow_opacity_owner") == owner_name
    ):
        return material

    material.use_nodes = True
    if not material.get("bflow_opacity_original_settings_saved", False):
        try:
            material["bflow_opacity_original_render_method"] = material.surface_render_method
        except Exception:
            material["bflow_opacity_original_render_method"] = 'DITHERED'
        try:
            material["bflow_opacity_original_overlap"] = bool(material.use_transparency_overlap)
        except Exception:
            material["bflow_opacity_original_overlap"] = True
        try:
            material["bflow_opacity_original_shadow"] = bool(material.use_transparent_shadow)
        except Exception:
            material["bflow_opacity_original_shadow"] = False
        material["bflow_opacity_original_settings_saved"] = True
    try:
        material.surface_render_method = 'BLENDED'
    except Exception:
        pass
    try:
        material.use_transparency_overlap = True
    except Exception:
        pass
    try:
        material.use_transparent_shadow = True
    except Exception:
        pass

    node_tree = material.node_tree
    nodes = node_tree.nodes
    links = node_tree.links

    outputs = [
        node for node in nodes
        if node.type == 'OUTPUT_MATERIAL' and getattr(node, "is_active_output", True)
    ]
    if not outputs:
        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (600, 0)
        outputs = [output]

    # Ensure a usable surface exists even for a new/empty material.
    if not any(
        output.inputs.get("Surface") and
        any(link.to_node == output and link.to_socket == output.inputs["Surface"] for link in links)
        for output in outputs
    ):
        principled = next((node for node in nodes if node.type == 'BSDF_PRINCIPLED'), None)
        if principled is None:
            principled = nodes.new("ShaderNodeBsdfPrincipled")
            principled.location = (250, 0)
        for output in outputs:
            surface = output.inputs.get("Surface")
            if surface and not any(link.to_node == output and link.to_socket == surface for link in links):
                links.new(principled.outputs["BSDF"], surface)

    attribute = nodes.new("ShaderNodeAttribute")
    attribute.name = "B-Flow Opacity Attribute"
    attribute.label = "B-FLOW / PARTICLE OPACITY"
    attribute.attribute_type = 'INSTANCER'
    attribute.attribute_name = "bflow_opacity"
    attribute.location = (-600, -300)

    transparent = nodes.new("ShaderNodeBsdfTransparent")
    transparent.name = "B-Flow Opacity Transparent"
    transparent.label = "B-FLOW / TRANSPARENT"
    transparent.location = (0, -250)

    for index, output in enumerate(outputs):
        surface = output.inputs.get("Surface")
        if surface is None:
            continue
        previous_link = next(
            (link for link in links if link.to_node == output and link.to_socket == surface),
            None
        )
        if previous_link is None:
            continue

        original_socket = previous_link.from_socket
        original_node = previous_link.from_node
        links.remove(previous_link)

        mix = nodes.new("ShaderNodeMixShader")
        mix.name = f"B-Flow Opacity Mix {index + 1}"
        mix.label = "B-FLOW / OPACITY FADE"
        mix.location = (350, -100 - index * 220)

        # Mix factor 0 = transparent; 1 = original surface.
        links.new(attribute.outputs["Fac"], mix.inputs[0])
        links.new(transparent.outputs[0], mix.inputs[1])
        links.new(original_socket, mix.inputs[2])
        links.new(mix.outputs[0], surface)

    material["bflow_opacity_material"] = True
    material["bflow_opacity_owner"] = owner_name
    return material


def _bflow_disable_opacity_on_material(material, owner_name):
    """Remove only B-Flow's opacity wrapper and restore the original surface."""
    if not material or not material.get("bflow_opacity_material", False):
        return material
    if material.get("bflow_opacity_owner") != owner_name:
        return material

    if material.use_nodes and material.node_tree:
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        outputs = [
            node for node in nodes
            if node.type == 'OUTPUT_MATERIAL' and getattr(node, "is_active_output", True)
        ]

        # Restore the original shader that was connected to Mix Shader input 2.
        for output in outputs:
            surface = output.inputs.get("Surface")
            if surface is None:
                continue
            opacity_mix = next(
                (
                    link.from_node for link in list(links)
                    if link.to_node == output
                    and link.to_socket == surface
                    and link.from_node.type == 'MIX_SHADER'
                    and link.from_node.name.startswith("B-Flow Opacity Mix")
                ),
                None
            )
            if opacity_mix is None:
                continue
            original_input = opacity_mix.inputs[2]
            original_link = next(
                (link for link in list(links) if link.to_node == opacity_mix and link.to_socket == original_input),
                None
            )
            if original_link is not None:
                original_node = original_link.from_node
                original_socket = original_link.from_socket
                links.remove(next(
                    link for link in list(links)
                    if link.to_node == output and link.to_socket == surface
                ))
                links.new(original_socket, surface)

        # Delete only nodes owned by the B-Flow opacity wrapper.
        for node in list(nodes):
            if (
                node.name.startswith("B-Flow Opacity Mix")
                or node.name == "B-Flow Opacity Attribute"
                or node.name == "B-Flow Opacity Transparent"
            ):
                nodes.remove(node)

    # Restore the material's pre-B-Flow transparency configuration.
    try:
        material.surface_render_method = material.get(
            "bflow_opacity_original_render_method", 'DITHERED'
        )
    except Exception:
        pass
    try:
        material.use_transparency_overlap = bool(
            material.get("bflow_opacity_original_overlap", True)
        )
    except Exception:
        pass
    try:
        material.use_transparent_shadow = bool(
            material.get("bflow_opacity_original_shadow", False)
        )
    except Exception:
        pass

    for key in (
        "bflow_opacity_material",
        "bflow_opacity_owner",
        "bflow_opacity_original_render_method",
        "bflow_opacity_original_overlap",
        "bflow_opacity_original_shadow",
        "bflow_opacity_original_settings_saved",
    ):
        if key in material:
            del material[key]
    return material


def _bflow_enable_particle_color_on_material(material, owner_name):
    """Add one editable particle-color input while preserving the user's shader.

    The B-Flow nodes are inserted only once. Subsequent compiles reuse the
    same material datablock and leave all user edits intact.
    """
    if (
        material.get("bflow_rand_color_material", False)
        and material.get("bflow_display_owner") == owner_name
    ):
        return True

    material.use_nodes = True
    node_tree = material.node_tree
    nodes = node_tree.nodes
    links = node_tree.links

    principled_nodes = [node for node in nodes if node.type == 'BSDF_PRINCIPLED']
    if not principled_nodes:
        # Keep non-Principled materials intact rather than silently replacing
        # their shader graph. Such materials cannot be tinted by this helper.
        material["bflow_rand_color_unsupported"] = True
        material["bflow_display_owner"] = owner_name
        return False

    attribute = nodes.new("ShaderNodeAttribute")
    attribute.name = "B-Flow Particle Color Attribute"
    attribute.label = "B-FLOW / PARTICLE COLOR"
    attribute.attribute_type = 'INSTANCER'
    attribute.attribute_name = "bflow_color"
    attribute.location = (-780, 360)

    for index, principled in enumerate(principled_nodes):
        base_color = principled.inputs.get("Base Color")
        if base_color is None:
            continue

        previous_link = next(
            (link for link in links if link.to_node == principled and link.to_socket == base_color),
            None
        )

        tint = nodes.new("ShaderNodeMixRGB")
        tint.name = f"B-Flow Particle Color Tint {index + 1}"
        tint.label = "B-FLOW / RANDOM COLORIZE"
        # COLOR takes hue and saturation from the per-particle palette color
        # while preserving the original Base Color's value/brightness detail.
        # This avoids MULTIPLY hiding palette colors against pre-colored
        # Shape Instance materials (for example, magenta multiplied by green).
        tint.blend_type = 'COLOR'
        tint.inputs["Fac"].default_value = 1.0
        tint.use_alpha = False
        tint.location = (principled.location.x - 260, principled.location.y - 260)

        if previous_link is not None:
            original_socket = previous_link.from_socket
            links.remove(previous_link)
            links.new(original_socket, tint.inputs["Color1"])
        else:
            tint.inputs["Color1"].default_value = tuple(base_color.default_value)

        links.new(attribute.outputs["Color"], tint.inputs["Color2"])
        links.new(tint.outputs["Color"], base_color)

    material["bflow_rand_color_material"] = True
    material["bflow_display_owner"] = owner_name
    return True


def _bflow_remove_generated_opacity_assets(owner_name):
    """Remove only B-Flow generated hidden source objects/materials for a host."""
    for generated in list(bpy.data.objects):
        if (
            generated.get("bflow_opacity_owner") != owner_name
            and generated.get("bflow_display_owner") != owner_name
        ):
            continue
        data = generated.data
        bpy.data.objects.remove(generated, do_unlink=True)
        if data is not None and getattr(data, "users", 1) == 0:
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)

    # Keep B-Flow's editable material datablocks even when the temporary
    # display-source mesh is rebuilt. They are intentionally user-facing assets.
    for material in list(bpy.data.materials):
        if (
            (
                material.get("bflow_opacity_owner") == owner_name
                or material.get("bflow_display_owner") == owner_name
            )
            and material.users == 0
            and not material.get("bflow_user_editable_material", False)
        ):
            bpy.data.materials.remove(material)


def _bflow_find_editable_particle_material(owner_name, source_name, slot_index):
    """Find the persistent editable material associated with one source slot."""
    for material in bpy.data.materials:
        if (
            material.get("bflow_user_editable_material", False)
            and material.get("bflow_display_owner") == owner_name
            and material.get("bflow_source_material", "") == source_name
            and int(material.get("bflow_material_slot", -1)) == int(slot_index)
        ):
            return material
    return None


def _bflow_expose_particle_material(host_object, material):
    """Attach a generated material to the visible B-Flow preview object's slots."""
    if material is None or not hasattr(host_object.data, "materials"):
        return
    if material.name not in {slot.name for slot in host_object.data.materials if slot}:
        host_object.data.materials.append(material)


def _bflow_make_particle_shape_source(
    source_object, host_object, scene, opacity_enabled=False, rand_color_enabled=False
):
    """Create a hidden display-only Shape Instance copy with requested attributes."""
    if source_object is None or source_object.data is None:
        return None

    duplicate = None
    try:
        duplicate = source_object.copy()
        duplicate.data = source_object.data.copy()
        duplicate.name = f"B-Flow Display Source - {source_object.name}"
        duplicate["bflow_display_owner"] = host_object.name
        duplicate["bflow_display_source"] = True

        target_collection = (
            host_object.users_collection[0]
            if host_object.users_collection
            else scene.collection
        )
        target_collection.objects.link(duplicate)

        data = duplicate.data
        if not hasattr(data, "materials"):
            bpy.data.objects.remove(duplicate, do_unlink=True)
            return None

        original_materials = list(data.materials)
        if not original_materials:
            generated_material = bpy.data.materials.new(
                f"B-Flow Display Material - {source_object.name}"
            )
            generated_material.use_nodes = True
            generated_material["bflow_display_owner"] = host_object.name
            data.materials.append(generated_material)
            original_materials = [generated_material]
            # The generated material already has a Principled BSDF in Blender's
            # default node tree; all requested wrappers are applied below.
            data.materials[0] = generated_material

        for slot_index, original_material in enumerate(original_materials):
            source_name = original_material.name if original_material else "Default"
            copied_material = _bflow_find_editable_particle_material(
                host_object.name, source_name, slot_index
            )

            if copied_material is None:
                if original_material is None:
                    copied_material = bpy.data.materials.new(
                        f"B-Flow Particle Material - {source_object.name} - {slot_index + 1}"
                    )
                    copied_material.use_nodes = True
                else:
                    copied_material = original_material.copy()
                    copied_material.name = f"B-Flow Particle Material - {original_material.name}"

                copied_material["bflow_user_editable_material"] = True
                copied_material["bflow_display_owner"] = host_object.name
                copied_material["bflow_source_material"] = source_name
                copied_material["bflow_material_slot"] = int(slot_index)

            if rand_color_enabled:
                _bflow_enable_particle_color_on_material(copied_material, host_object.name)
            if opacity_enabled:
                _bflow_enable_opacity_on_material(copied_material, host_object.name)
            else:
                _bflow_disable_opacity_on_material(copied_material, host_object.name)

            data.materials[slot_index] = copied_material
            _bflow_expose_particle_material(host_object, copied_material)

        duplicate.hide_render = True
        try:
            duplicate.hide_set(True)
        except Exception:
            pass
        return duplicate

    except Exception as exc:
        print("[B-Flow] Could not create Rand Color/Opacity Shape Instance source:", exc)
        try:
            if duplicate and duplicate.name in bpy.data.objects:
                bpy.data.objects.remove(duplicate, do_unlink=True)
        except Exception:
            pass
        return None


def _bflow_make_default_particle_material(host_object, opacity_enabled=False, rand_color_enabled=False):
    """Get the persistent, user-editable material for B-Flow's default particles."""
    material = next(
        (
            candidate for candidate in bpy.data.materials
            if candidate.get("bflow_user_editable_material", False)
            and candidate.get("bflow_default_particle_material", False)
            and candidate.get("bflow_display_owner") == host_object.name
        ),
        None
    )
    if material is None:
        material = bpy.data.materials.new("B-Flow Particle Material")
        material.use_nodes = True
        material["bflow_user_editable_material"] = True
        material["bflow_default_particle_material"] = True
        material["bflow_display_owner"] = host_object.name

    _bflow_expose_particle_material(host_object, material)

    if rand_color_enabled:
        principled = next(
            (node for node in material.node_tree.nodes if node.type == 'BSDF_PRINCIPLED'),
            None
        )
        if principled is not None and principled.inputs.get("Base Color") is not None:
            # A neutral white base lets the user palette appear at its chosen color.
            principled.inputs["Base Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        _bflow_enable_particle_color_on_material(material, host_object.name)
    if opacity_enabled:
        material["bflow_opacity_owner"] = host_object.name
        _bflow_enable_opacity_on_material(material, host_object.name)
    else:
        _bflow_disable_opacity_on_material(material, host_object.name)

    return material


def _bflow_make_default_opacity_material(host_object):
    """Create a material for B-Flow's built-in Ico Sphere particle display."""
    material = bpy.data.materials.new("B-Flow Particle Opacity")
    material.use_nodes = True
    material["bflow_opacity_owner"] = host_object.name
    _bflow_enable_opacity_on_material(material, host_object.name)
    return material


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class BFLOW_RandColorEntry(PropertyGroup):
    label: StringProperty(
        name="Color Name",
        default="Color",
        update=_bflow_schedule_live_update,
    )
    color: FloatVectorProperty(
        name="Color",
        size=4,
        default=(1.0, 0.15, 0.03, 1.0),
        min=0.0,
        max=1.0,
        subtype='COLOR',
        update=_bflow_schedule_live_update,
    )
    percentage: FloatProperty(
        name="Percentage",
        description="Relative probability weight; all entries are normalized automatically",
        default=50.0,
        min=0.0,
        max=100.0,
        update=_bflow_rand_color_percentage_update,
    )


class BFLOW_ShapeInstanceEntry(PropertyGroup):
    object: PointerProperty(
        name="Object",
        type=bpy.types.Object,
        description="Object to use for this particle shape",
        update=_bflow_schedule_live_update,
    )
    percentage: FloatProperty(
        name="Percentage",
        description="Relative probability weight for this shape",
        default=100.0,
        min=0.0,
        max=100.0,
        update=_bflow_schedule_live_update,
    )


class BFLOW_ForceFieldRef(PropertyGroup):
    field_object: PointerProperty(
        name="Force Field",
        type=bpy.types.Object,
        description="A Blender object with a native Force Field",
        update=_bflow_schedule_live_update,
    )
    influence: FloatProperty(
        name="Influence",
        default=1.0,
        min=-100.0,
        max=100.0,
        update=_bflow_schedule_live_update,
    )


class BFLOW_OperatorItem(PropertyGroup):
    force_fields: CollectionProperty(type=BFLOW_ForceFieldRef)
    enabled: BoolProperty(name="Enabled", default=True, update=_bflow_schedule_live_update)
    operator_type: EnumProperty(name="Operator", items=OPERATOR_ITEMS, default="GRAVITY")
    count: IntProperty(name="Count", default=100, min=1, max=1000000, update=_bflow_schedule_live_update)
    birth_start: IntProperty(name="Start", default=1, min=0, max=1000000, update=_bflow_schedule_live_update)
    birth_stop: IntProperty(name="Stop", default=100, min=0, max=1000000, update=_bflow_schedule_live_update)
    rate: FloatProperty(name="Rate", default=100.0, min=0.0, max=1000000.0)
    speed: FloatProperty(name="Speed", default=2.0, min=-10000.0, max=10000.0, update=_bflow_schedule_live_update)
    speed_randomness: FloatProperty(
        name="Speed Randomness", default=0.0, min=0.0, max=1.0,
        subtype='FACTOR', description="Per-particle speed variation as a fraction of base speed",
        update=_bflow_schedule_live_update
    )
    speed_mode: EnumProperty(
        name="Direction Mode",
        items=[
            ("NORMAL", "Surface Normal", "Shoot along the emitter surface normal"),
            ("CUSTOM", "Custom Direction", "Use the direction vector below"),
        ],
        default="NORMAL", update=_bflow_schedule_live_update,
    )
    direction: FloatVectorProperty(name="Direction", size=3, default=(0.0, 0.0, 1.0), subtype='XYZ', update=_bflow_schedule_live_update)
    gravity: FloatVectorProperty(name="Gravity", size=3, default=(0.0, 0.0, -9.81), subtype='XYZ', update=_bflow_schedule_live_update)
    strength: FloatProperty(name="Strength", default=1.0, min=-10000.0, max=10000.0)
    keep_distance: FloatProperty(
        name="Keep Distance",
        description="Minimum preferred distance between particle points",
        default=0.25,
        min=0.0001,
        max=10000.0,
        update=_bflow_schedule_live_update,
    )
    keep_dist_strength: FloatProperty(
        name="Strength",
        description="How strongly overlapping particles are pushed apart",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_bflow_schedule_live_update,
    )
    damping: FloatProperty(name="Damping", default=0.1, min=0.0, max=1.0)
    collision_height: FloatProperty(name="Ground Height", default=0.0, update=_bflow_schedule_live_update)
    bounce: FloatProperty(name="Bounce", default=0.65, min=0.0, max=1.0, update=_bflow_schedule_live_update)
    bounce_randomness: FloatProperty(
        name="Bounce Randomness", default=0.0, min=0.0, max=1.0,
        subtype='FACTOR', description="Per-particle variation of bounce amount",
        update=_bflow_schedule_live_update
    )
    friction: FloatProperty(name="Friction", default=0.15, min=0.0, max=1.0, update=_bflow_schedule_live_update)
    friction_randomness: FloatProperty(
        name="Friction Randomness", default=0.0, min=0.0, max=1.0,
        subtype='FACTOR', description="Per-particle variation of friction amount",
        update=_bflow_schedule_live_update
    )
    particle_radius: FloatProperty(name="Particle Radius", default=0.035, min=0.0001, max=1000.0, update=_bflow_schedule_live_update)
    lifetime: FloatProperty(name="Lifetime", default=5.0, min=0.001, max=100000.0)
    delete_mode: EnumProperty(
        name="Delete Mode",
        items=[
            ("REAL_TIME", "Real Time", "Delete at a scene timeline time"),
            ("PARTICLE_TIME", "Particle Time", "Delete each particle after its own age"),
            ("AFTER_COLLISION", "After Collision", "Delete a particle after its first ground or object collision"),
        ],
        default="PARTICLE_TIME",
        update=_bflow_schedule_live_update,
    )
    delete_time_unit: EnumProperty(
        name="Time Unit",
        items=[
            ("FRAMES", "Frames", "Measure time in timeline frames"),
            ("SECONDS", "Seconds", "Measure time in seconds"),
        ],
        default="FRAMES",
        update=_bflow_schedule_live_update,
    )
    delete_after: FloatProperty(
        name="Delete After",
        default=30.0,
        min=0.0,
        max=1000000.0,
        update=_bflow_schedule_live_update,
    )
    threshold: FloatProperty(name="Threshold", default=1.0, min=0.0, max=100000.0)
    size: FloatProperty(name="Size", default=0.1, min=0.0001, max=10000.0)
    size_mode: EnumProperty(
        name="Size Mode",
        items=[
            ("FIXED", "Fixed", "Keep a constant display size"),
            ("REAL_TIME", "Real Time", "Scale all particles together using the scene timeline"),
            ("PARTICLE_TIME", "Particle Time", "Scale each particle from its own birth time"),
        ],
        default="FIXED",
        update=_bflow_schedule_live_update,
    )
    size_fixed: FloatProperty(
        name="Fixed Size", default=1.0, min=0.0001, max=10000.0,
        update=_bflow_schedule_live_update,
    )
    size_start: FloatProperty(
        name="Start Size", default=1.0, min=0.0001, max=10000.0,
        update=_bflow_schedule_live_update,
    )
    size_end: FloatProperty(
        name="End Size", default=2.0, min=0.0001, max=10000.0,
        update=_bflow_schedule_live_update,
    )
    size_start_time: FloatProperty(
        name="Start Time",
        description="Start of the size transition, measured in the selected time unit",
        default=0.0,
        min=0.0,
        max=1000000.0,
        precision=0,
        update=_bflow_size_start_time_update,
    )
    size_end_time: FloatProperty(
        name="End Time",
        description="End of the size transition, measured in the selected time unit",
        default=30.0,
        min=0.001,
        max=1000000.0,
        precision=0,
        update=_bflow_size_end_time_update,
    )
    size_time_unit: EnumProperty(
        name="Time Unit",
        items=[
            ("FRAMES", "Frames", "Measure size animation duration in frames"),
            ("SECONDS", "Seconds", "Measure size animation duration in seconds"),
        ],
        default="FRAMES",
        update=_bflow_schedule_live_update,
    )
    opacity_mode: EnumProperty(
        name="Opacity Mode",
        items=[
            ("FIXED", "Fixed", "Keep a constant opacity"),
            ("REAL_TIME", "Real Time", "Fade all particles together using scene time"),
            ("PARTICLE_TIME", "Particle Time", "Fade each particle from its own birth time"),
        ],
        default="PARTICLE_TIME",
        update=_bflow_schedule_live_update,
    )
    opacity_fixed: FloatProperty(
        name="Fixed Opacity",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_bflow_schedule_live_update,
    )
    opacity_start: FloatProperty(
        name="Start Opacity",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_bflow_schedule_live_update,
    )
    opacity_end: FloatProperty(
        name="End Opacity",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_bflow_schedule_live_update,
    )
    opacity_start_frame: IntProperty(
        name="Start Frame",
        description="Scene frame for Real Time, or age in frames for Particle Time",
        default=0,
        min=0,
        max=1000000,
        update=_bflow_opacity_start_frame_update,
    )
    opacity_end_frame: IntProperty(
        name="End Frame",
        description="Scene frame for Real Time, or age in frames for Particle Time",
        default=30,
        min=0,
        max=1000000,
        update=_bflow_opacity_end_frame_update,
    )
    random_amount: FloatProperty(name="Random Amount", default=1.0, min=0.0, max=100000.0)
    child_count: IntProperty(name="Children per Particle", default=5, min=1, max=10000)
    source_object: PointerProperty(name="Source Object", type=bpy.types.Object)
    collider: PointerProperty(name="Collider", type=bpy.types.Object)
    object_bounce: FloatProperty(name="Object Bounce", default=0.85, min=0.0, max=1.0)
    object_friction: FloatProperty(name="Object Friction", default=0.0, min=0.0, max=1.0)
    object_random_deflection: FloatProperty(
        name="Random Deflection",
        description="Adds a per-particle random sideways impulse after object collision; 0 disables it",
        default=0.25,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_bflow_schedule_live_update,
    )
    object_particle_radius: FloatProperty(name="Object Particle Radius", default=0.035, min=0.0001, max=1000.0)

    rb_mass: FloatProperty(
        name="Mass", default=1.0, min=0.001, max=1000000.0,
        description="Mass assigned to each generated native rigid body",
    )
    rb_friction: FloatProperty(
        name="Particle Friction",
        description="Contact friction applied to each generated particle rigid body",
        default=0.7,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
    )
    rb_restitution: FloatProperty(
        name="Restitution", default=0.4, min=0.0, max=1.0,
        subtype='FACTOR',
    )
    rb_collision_shape: EnumProperty(
        name="Collision Shape",
        items=[
            ("SPHERE", "Sphere", "Fast spherical collision shape"),
            ("BOX", "Box", "Box collision shape"),
            ("CAPSULE", "Capsule", "Capsule collision shape"),
            ("CYLINDER", "Cylinder", "Cylinder collision shape"),
            ("CONE", "Cone", "Cone collision shape"),
            ("CONVEX_HULL", "Convex Hull", "Convex hull based on the particle mesh"),
            ("MESH", "Mesh", "Exact mesh collision; slower and intended for static bodies"),
        ],
        default="SPHERE",
    )
    rb_collision_margin: FloatProperty(
        name="Collision Margin", default=0.02, min=0.0, max=10.0,
    )
    rb_linear_damping: FloatProperty(
        name="Linear Damping", default=0.15, min=0.0, max=1.0,
        subtype='FACTOR',
    )
    rb_angular_damping: FloatProperty(
        name="Angular Damping",
        description="Reduces rotational velocity over time",
        default=0.4,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
    )
    rb_use_deactivation: BoolProperty(
        name="Enable Deactivation",
        description="Allow resting particles to sleep when both linear and angular velocity are low",
        default=True,
    )
    rb_deactivate_linear_velocity: FloatProperty(
        name="Linear Sleep Threshold",
        description="Particles may sleep when linear speed falls below this value",
        default=0.8,
        min=0.0,
        max=1000.0,
    )
    rb_deactivate_angular_velocity: FloatProperty(
        name="Angular Sleep Threshold",
        description="Particles may sleep when angular speed falls below this value",
        default=0.8,
        min=0.0,
        max=1000.0,
    )
    rb_transfer_motion: BoolProperty(
        name="Transfer Current Motion",
        description="Sample neighbouring B-Flow frames and transfer linear/angular velocity to native Rigid Bodies",
        default=True,
    )
    rb_transfer_acceleration: BoolProperty(
        name="Transfer Previous-Frame Acceleration",
        description="Estimate linear/angular acceleration from the previous two B-Flow steps and use it to correct the handoff motion",
        default=True,
    )

    # Legacy single-object field is retained for old project migration.
    shape_object: PointerProperty(
        name="Instance Object", type=bpy.types.Object,
        description="Legacy single-object particle shape",
        update=_bflow_schedule_live_update
    )
    shape_instance_entries: CollectionProperty(type=BFLOW_ShapeInstanceEntry)
    shape_scale: FloatProperty(
        name="Instance Scale", default=1.0, min=0.0001, max=10000.0,
        update=_bflow_schedule_live_update
    )
    rotation_offset: FloatVectorProperty(
        name="Rotation XYZ",
        size=3,
        default=(0.0, 0.0, 0.0),
        subtype='EULER',
        description="Additional initial rotation on the local X, Y, and Z axes",
        update=_bflow_schedule_live_update
    )
    rotation_randomness: FloatProperty(
        name="Random Rotation", default=1.0, min=0.0, max=1.0,
        subtype='FACTOR',
        description="Random initial orientation; 1 means a full 0–360 degree range on each axis",
        update=_bflow_schedule_live_update
    )
    spin_randomness: FloatProperty(
        name="Spin Randomness", default=0.5, min=0.0, max=1.0,
        subtype='FACTOR',
        description="Per-particle variation in spin speed",
        update=_bflow_schedule_live_update
    )
    curve_object: PointerProperty(name="Curve Object", type=bpy.types.Object)
    material: PointerProperty(name="Material", type=bpy.types.Material)
    rand_color_entries: CollectionProperty(type=BFLOW_RandColorEntry)

class BFLOW_Socket(NodeSocket):
    bl_idname = "BFLOW_Socket"
    bl_label = "B-Flow Event Link"

    def draw(self, context, layout, node, text):
        layout.label(text=text or "Event", icon='PARTICLES')

    def draw_color(self, context, node):
        return (0.24, 0.68, 0.32, 1.0)

class BFLOW_Tree(NodeTree):
    bl_idname = "BFLOW_Tree"
    bl_label = "B-Flow Event Editor"
    bl_icon = 'PARTICLES'

class BFLOW_EventNode(Node):
    bl_idname = "BFLOW_EventNode"
    bl_label = "Event"
    bl_icon = 'NODE'

    event_name: StringProperty(name="Event Name", default="Event")
    event_id: IntProperty(name="Event ID", default=1, min=1)
    operator_items: CollectionProperty(type=BFLOW_OperatorItem)
    active_operator: IntProperty(default=0, min=0)

    def init(self, context):
        self.inputs.new("BFLOW_Socket", "In")
        self.outputs.new("BFLOW_Socket", "Out")
        self.width = 330
        self.use_custom_color = True
        self.color = (0.16, 0.38, 0.20)

    def draw_label(self):
        return "EVENT %02d  |  %s" % (self.event_id, self.event_name.upper())

    def draw_buttons(self, context, layout):
        name_row = layout.row(align=True)
        name_row.prop(self, "event_name", text="Name")

        row = layout.row(align=True)
        row.label(text=f"{len(self.operator_items)} Operators", icon='MOD_PARTICLES')
        row.label(text=f"ID {self.event_id:02d}")

        if not self.operator_items:
            layout.label(text="Add operators from the B-Flow sidebar", icon='INFO')
            return

        for index, item in enumerate(self.operator_items):
            row = layout.row(align=True)
            row.label(text="", icon=operator_icon_for_item(item))
            select = row.operator(
                "bflow.select_operator",
                text=operator_label(item.operator_type),
                icon='NONE',
                depress=(index == self.active_operator),
            )
            select.tree_name = self.id_data.name
            select.node_name = self.name
            select.index = index

            row.prop(item, "enabled", text="")

            up = row.operator("bflow.move_operator", text="", icon='TRIA_UP')
            up.tree_name = self.id_data.name
            up.node_name = self.name
            up.index = index
            up.direction = "UP"

            down = row.operator("bflow.move_operator", text="", icon='TRIA_DOWN')
            down.tree_name = self.id_data.name
            down.node_name = self.name
            down.index = index
            down.direction = "DOWN"

            remove = row.operator("bflow.remove_operator", text="", icon='X')
            remove.tree_name = self.id_data.name
            remove.node_name = self.name
            remove.index = index
        layout.separator()
        row = layout.row(align=True)
        add = row.operator("bflow.add_operator", text="Add Operator", icon='ADD')
        add.tree_name = self.id_data.name
        add.node_name = self.name

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_tree(tree_name):
    tree = bpy.data.node_groups.get(tree_name)
    return tree if tree and tree.bl_idname == "BFLOW_Tree" else None

def get_event(tree_name, node_name):
    tree = get_tree(tree_name)
    if not tree:
        return None, None
    node = tree.nodes.get(node_name)
    if node and node.bl_idname == "BFLOW_EventNode":
        return tree, node
    return tree, None

def active_event(context):
    tree = getattr(context.space_data, "edit_tree", None)
    if not tree or tree.bl_idname != "BFLOW_Tree":
        return None, None
    node = tree.nodes.active
    if node and node.bl_idname == "BFLOW_EventNode":
        return tree, node
    selected = [n for n in tree.nodes if n.select and n.bl_idname == "BFLOW_EventNode"]
    return (tree, selected[-1]) if selected else (tree, None)


def _bflow_add_default_birth(event_node):
    """Add one visible Birth operator to a new source/root Event."""
    if event_node is None or event_node.bl_idname != "BFLOW_EventNode":
        return None
    if any(item.operator_type == "BIRTH" for item in event_node.operator_items):
        return None
    item = event_node.operator_items.add()
    item.operator_type = "BIRTH"
    item.enabled = True
    item.count = 100
    item.birth_start = 1
    item.birth_stop = 100
    event_node.active_operator = len(event_node.operator_items) - 1
    return item

# ---------------------------------------------------------------------------
# Operators: graph and stack editing
# ---------------------------------------------------------------------------

class BFLOW_OT_new_graph(Operator):
    bl_idname = "bflow.new_graph"
    bl_label = "New B-Flow Graph"

    graph_name: StringProperty(default="B-Flow Graph")

    def execute(self, context):
        tree = bpy.data.node_groups.new(self.graph_name, "BFLOW_Tree")
        event = tree.nodes.new("BFLOW_EventNode")
        event.location = (0, 0)
        event.event_name = "Birth Event"
        event.event_id = 1
        _bflow_add_default_birth(event)
        tree.nodes.active = event
        event.select = True
        context.scene.bflow_graph = tree
        # Capture the selected mesh as the graph's default emitter.
        # Prefer the active object when it is a valid mesh; otherwise use the
        # first selected mesh. This keeps the initial emission working even
        # when the active selection is a camera/light/empty alongside a mesh.
        initial_emitter = context.view_layer.objects.active
        if not (
            initial_emitter is not None
            and initial_emitter.type == 'MESH'
            and initial_emitter.name != "B-Flow Preview"
        ):
            initial_emitter = next(
                (
                    selected for selected in context.selected_objects
                    if selected is not None
                    and selected.type == 'MESH'
                    and selected.name != "B-Flow Preview"
                ),
                None,
            )
        context.scene.bflow_default_emitter = initial_emitter
        # Opening a fresh graph should immediately create the working layout:
        # 3D Viewport on the left and B-Flow Event Editor on the right.
        _bflow_open_split_layout(context, tree)
        self.report({'INFO'}, "New Event Stack graph created")
        return {'FINISHED'}

class BFLOW_OT_add_event(Operator):
    bl_idname = "bflow.add_event"
    bl_label = "Add Event"

    def execute(self, context):
        tree, current = active_event(context)
        if tree is None:
            tree = context.scene.bflow_graph
        if tree is None:
            self.report({'WARNING'}, "Create a B-Flow graph first")
            return {'CANCELLED'}
        node = tree.nodes.new("BFLOW_EventNode")
        node.event_id = max([n.event_id for n in tree.nodes if n.bl_idname == "BFLOW_EventNode"] or [0]) + 1
        node.event_name = f"Event {node.event_id:02d}"
        if current:
            node.location = (current.location.x + 360, current.location.y)
            try:
                tree.links.new(current.outputs["Out"], node.inputs["In"])
            except Exception:
                pass
        else:
            node.location = (0, 0)
            _bflow_add_default_birth(node)
        tree.nodes.active = node
        for n in tree.nodes:
            n.select = (n == node)
        return {'FINISHED'}

def _bflow_seed_rand_color_palette(item):
    """Start a new Rand Color operator with one default color at 100%."""
    if len(item.rand_color_entries) > 0:
        return

    first = item.rand_color_entries.add()
    first.label = "Orange"
    first.color = (1.0, 0.16, 0.025, 1.0)
    first.percentage = 100.0


def _bflow_seed_shape_instance_list(item):
    """Create one empty 100% shape slot when a Shape Instance operator is added."""
    if len(item.shape_instance_entries) == 0:
        entry = item.shape_instance_entries.add()
        entry.percentage = 100.0


class BFLOW_OT_add_shape_instance_entry(Operator):
    bl_idname = "bflow.add_shape_instance_entry"
    bl_label = "Add Shape Object"

    tree_name: StringProperty()
    node_name: StringProperty()
    operator_index: IntProperty()

    def execute(self, context):
        tree, node = get_event(self.tree_name, self.node_name)
        if not node or not (0 <= self.operator_index < len(node.operator_items)):
            self.report({'WARNING'}, "Shape Instance operator not found")
            return {'CANCELLED'}
        item = node.operator_items[self.operator_index]
        if item.operator_type != "SHAPE_INSTANCE":
            self.report({'WARNING'}, "Selected item is not Shape Instance")
            return {'CANCELLED'}
        entry = item.shape_instance_entries.add()
        entry.percentage = 100.0 / max(1, len(item.shape_instance_entries))
        share = 100.0 / len(item.shape_instance_entries)
        for current in item.shape_instance_entries:
            current.percentage = share
        _bflow_schedule_live_update(item, context)
        return {'FINISHED'}


class BFLOW_OT_remove_shape_instance_entry(Operator):
    bl_idname = "bflow.remove_shape_instance_entry"
    bl_label = "Remove Shape Object"

    tree_name: StringProperty()
    node_name: StringProperty()
    operator_index: IntProperty()
    entry_index: IntProperty()

    def execute(self, context):
        tree, node = get_event(self.tree_name, self.node_name)
        if not node or not (0 <= self.operator_index < len(node.operator_items)):
            self.report({'WARNING'}, "Shape Instance operator not found")
            return {'CANCELLED'}
        item = node.operator_items[self.operator_index]
        if item.operator_type != "SHAPE_INSTANCE":
            self.report({'WARNING'}, "Selected item is not Shape Instance")
            return {'CANCELLED'}
        if not (0 <= self.entry_index < len(item.shape_instance_entries)):
            self.report({'WARNING'}, "Shape entry not found")
            return {'CANCELLED'}
        if len(item.shape_instance_entries) <= 1:
            self.report({'INFO'}, "Keep at least one Shape Instance slot")
            return {'CANCELLED'}
        item.shape_instance_entries.remove(self.entry_index)
        total = sum(max(0.0, float(entry.percentage)) for entry in item.shape_instance_entries)
        if total > 0.0:
            for entry in item.shape_instance_entries:
                entry.percentage = max(0.0, float(entry.percentage)) * 100.0 / total
        else:
            share = 100.0 / len(item.shape_instance_entries)
            for entry in item.shape_instance_entries:
                entry.percentage = share
        _bflow_schedule_live_update(item, context)
        return {'FINISHED'}


class BFLOW_OT_add_rand_color_entry(Operator):
    bl_idname = "bflow.add_rand_color_entry"
    bl_label = "Add Random Color"

    tree_name: StringProperty()
    node_name: StringProperty()
    operator_index: IntProperty()

    def execute(self, context):
        tree, node = get_event(self.tree_name, self.node_name)
        if not node or not (0 <= self.operator_index < len(node.operator_items)):
            self.report({'WARNING'}, "Rand Color operator not found")
            return {'CANCELLED'}
        item = node.operator_items[self.operator_index]
        if item.operator_type != "RAND_COLOR":
            self.report({'WARNING'}, "Selected item is not Rand Color")
            return {'CANCELLED'}

        entry = item.rand_color_entries.add()
        entry.label = f"Color {len(item.rand_color_entries)}"
        entry.color = (1.0, 1.0, 1.0, 1.0)
        entry.percentage = 0.0
        _bflow_rebalance_rand_color_item(item, make_equal=True)
        _bflow_schedule_live_update(item, context)
        return {'FINISHED'}


class BFLOW_OT_remove_rand_color_entry(Operator):
    bl_idname = "bflow.remove_rand_color_entry"
    bl_label = "Remove Random Color"

    tree_name: StringProperty()
    node_name: StringProperty()
    operator_index: IntProperty()
    entry_index: IntProperty()

    def execute(self, context):
        tree, node = get_event(self.tree_name, self.node_name)
        if not node or not (0 <= self.operator_index < len(node.operator_items)):
            self.report({'WARNING'}, "Rand Color operator not found")
            return {'CANCELLED'}
        item = node.operator_items[self.operator_index]
        if item.operator_type != "RAND_COLOR":
            self.report({'WARNING'}, "Selected item is not Rand Color")
            return {'CANCELLED'}
        if len(item.rand_color_entries) <= 1:
            self.report({'INFO'}, "Rand Color must keep at least one color entry")
            return {'CANCELLED'}
        if not (0 <= self.entry_index < len(item.rand_color_entries)):
            self.report({'WARNING'}, "Color entry not found")
            return {'CANCELLED'}

        item.rand_color_entries.remove(self.entry_index)
        _bflow_rebalance_rand_color_item(item, make_equal=True)
        _bflow_schedule_live_update(item, context)
        return {'FINISHED'}


class BFLOW_OT_add_force_field(Operator):
    bl_idname = "bflow.add_force_field"
    bl_label = "Add Force Field"
    tree_name: StringProperty()
    node_name: StringProperty()
    operator_index: IntProperty()

    def execute(self, context):
        tree, node = get_event(self.tree_name, self.node_name)
        if not node or not (0 <= self.operator_index < len(node.operator_items)):
            self.report({'WARNING'}, "Force operator not found")
            return {'CANCELLED'}
        item = node.operator_items[self.operator_index]
        if item.operator_type != "FORCE":
            self.report({'WARNING'}, "Selected item is not Force")
            return {'CANCELLED'}
        item.force_fields.add()
        _bflow_schedule_live_update(node, context)
        return {'FINISHED'}


class BFLOW_OT_remove_force_field(Operator):
    bl_idname = "bflow.remove_force_field"
    bl_label = "Remove Force Field"
    tree_name: StringProperty()
    node_name: StringProperty()
    operator_index: IntProperty()
    field_index: IntProperty()

    def execute(self, context):
        tree, node = get_event(self.tree_name, self.node_name)
        if not node or not (0 <= self.operator_index < len(node.operator_items)):
            self.report({'WARNING'}, "Force operator not found")
            return {'CANCELLED'}
        item = node.operator_items[self.operator_index]
        if item.operator_type != "FORCE" or not (0 <= self.field_index < len(item.force_fields)):
            self.report({'WARNING'}, "Force Field entry not found")
            return {'CANCELLED'}
        item.force_fields.remove(self.field_index)
        _bflow_schedule_live_update(node, context)
        return {'FINISHED'}


class BFLOW_OT_add_operator(Operator):
    bl_idname = "bflow.add_operator"
    bl_label = "Add Operator"

    tree_name: StringProperty()
    node_name: StringProperty()
    operator_type: EnumProperty(name="Operator", items=OPERATOR_ITEMS, default="GRAVITY")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        self.layout.prop(self, "operator_type", text="Operator")

    def execute(self, context):
        tree_name = self.tree_name
        node_name = self.node_name
        if not tree_name:
            tree, node = active_event(context)
            if tree and node:
                tree_name, node_name = tree.name, node.name
        tree, node = get_event(tree_name, node_name)
        if not node:
            self.report({'WARNING'}, "Select an Event first")
            return {'CANCELLED'}
        item = node.operator_items.add()
        item.operator_type = self.operator_type
        if item.operator_type == "FORCE" and len(item.force_fields) == 0:
            item.force_fields.add()
        if item.operator_type == "RAND_COLOR":
            _bflow_seed_rand_color_palette(item)
        if item.operator_type == "SHAPE_INSTANCE":
            _bflow_seed_shape_instance_list(item)
        node.active_operator = len(node.operator_items) - 1
        tree.nodes.active = node
        _bflow_schedule_live_update(node, context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Bottom Operator Depot overlay (Node Editor)
# ---------------------------------------------------------------------------

_BFLOW_DEPOT_ITEMS = [
    ("BIRTH", "Birth"),
    ("POSITION_OBJECT", "Position Object"),
    ("SPEED", "Speed"),
    ("GRAVITY", "Gravity"),
    ("FORCE", "Force"),
    ("KEEP_DIST", "Keep Dist"),
    ("ROTATION", "Rotation"),
    ("SPIN", "Spin"),
    ("COLLISION", "Ground Collision"),
    ("OBJECT_COLLISION", "Object Collision"),
    ("SHAPE_INSTANCE", "Shape Instance"),
    ("RAND_COLOR", "Rand Color"),
    ("SIZE", "Size"),
    ("OPACITY", "Opacity"),
    ("DELETE", "DEL"),
    ("RIGID_BODY", "Rigid Body"),
]


# ---------------------------------------------------------------------------
# Procedural vector icons for the floating GPU Operator Depot
# ---------------------------------------------------------------------------
# Icons are drawn directly with Blender's 2D GPU uniform-color shader.
# No PNG/SVG files, Base64 payloads, image datablocks, or GPU textures.
#
_BFLOW_EMBEDDED_SVG_ICONS = {'BIRTH': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
          'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="5" '
          'r="2.35"/><circle cx="19" cy="5" r="1.7"/><circle cx="5" cy="19" r="1.7"/><circle cx="19" cy="19" '
          'r="1.7"/>\n'
          '<path d="M7.4 5H17 M5 7.4V17 M6.8 6.8L17.2 17.2"/></g></svg>',
 'COLLISION': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
              'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 16.8L8.8 '
              '21L13.1 13.5L20.5 3.1 M15.4 4.7L20.5 3.1L19 8.2"/></g></svg>',
 'DELETE': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
           'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.2 6.5H20.8 '
           'M7.2 3.8H16.8 M5.8 7L6.8 21H17.2L18.2 7 M9.2 10V17.8 M14.8 10V17.8"/></g></svg>',
 'FORCE': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24">\n'
          '<g fill="none" stroke="#E8EDF2" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">\n'
          '<path d="M5 3L5 5L3.5 7L3.5 9L5 11L5 13L3.5 15L3.5 17L5 19L5 21"/>\n'
          '<path d="M12 3L12 5L10.5 7L10.5 9L12 11L12 13L10.5 15L10.5 17L12 19L12 21"/>\n'
          '<path d="M19 3L19 5L17.5 7L17.5 9L19 11L19 13L17.5 15L17.5 17L19 19L19 21"/>\n'
          '</g></svg>',
 'GRAVITY': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
            'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.5V20.8 '
            'M7.1 15.6L12 20.8L16.9 15.6"/></g></svg>',
 'GROUND_COLLISION': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g '
                     'fill="none" stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" '
                     'stroke-linejoin="round"><path d="M3 16.8L8.8 21L13.1 13.5L20.5 3.1 M15.4 4.7L20.5 3.1L19 '
                     '8.2"/></g></svg>',
 'KEEP_DIST': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
              'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" '
              'cy="12" r="9.2"/></g></svg>',
 'OBJECT_COLLISION': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g '
                     'fill="none" stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" '
                     'stroke-linejoin="round"><path d="M7.2 5.1L9.1 3.2L12 3.8L14.8 3L17 4.8L18.1 7.2L20.5 8.5L20.1 '
                     '11.1L21.2 13.4L19.2 15.5L18.5 18.1L15.8 18.8L14.1 21L11.6 20.1L9.2 21L7.1 18.9L4.4 18.5L4 '
                     '15.8L2.5 13.7L3.7 11.1L3.5 8.5L5.8 7.1Z M7.2 5.1L8.2 8.2L11 9.1L12.3 7.1L14.8 7.5L16.3 9.7L19.1 '
                     '10.1 M8.2 8.2L6.3 11.2L7.2 14.2L10 15.2L12 13.1L14.8 14L16.3 16.5 M10 15.2L10 19.2 M14.8 14L17.5 '
                     '12.2"/></g></svg>',
 'OPACITY': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
            'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 '
            '8V3H16V16H11 M8 21H3V8H16V21H8Z"/></g></svg>',
 'POSITION_OBJECT': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
                    'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 '
                    '3H21V16L16 21H3V8Z M8 3V8H3 M8 8H17V17H8Z"/></g></svg>',
 'RAND_COLOR': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
               'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.2 '
               '3H11.5V17.8L9.4 21H5.4L3.2 18.8Z M3.2 3H11.5 M15.2 10.5L20.9 16.2L14.7 22L12.7 20.1Z M12.2 19.5L18.4 '
               '13.3"/></g></svg>',
 'RIGID_BODY': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
               'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2.4 '
               '20.5L8.6 7.1L11.3 8.4L5.1 21.8Z M13.1 3.1H16.1V21H13.1Z M19 2.2H22V21H19Z"/></g></svg>',
 'ROTATION': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
             'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M19.67 9.70 '
             'L20.48 10.53 L20.92 11.42 L20.97 12.33 L20.64 13.23 L19.93 14.08 L18.88 14.83 L17.54 15.47 L15.95 15.95 '
             'L14.19 16.27 L12.34 16.40 L10.47 16.34 L8.67 16.09 L7.01 15.66 L5.57 15.08 L4.41 14.36 L3.57 13.54 L3.10 '
             '12.66 L3.02 11.74 L3.32 10.84 L4.00 9.99 L5.02 9.22 L6.35 8.58 L7.92 8.08 L9.67 7.75 L11.51 7.61 L13.38 '
             '7.65 L15.19 7.89 L16.86 8.30 L18.32 8.87 L19.51 9.58 M15.75 7.30 L16.15 8.98 L16.36 10.80 L16.39 12.67 '
             'L16.22 14.52 L15.88 16.25 L15.37 17.80 L14.71 19.10 L13.93 20.09 L13.07 20.73 L12.16 20.99 L11.25 20.87 '
             'L10.37 20.36 L9.56 19.49 L8.86 18.30 L8.29 16.83 L7.88 15.16 L7.65 13.34 L7.61 11.47 L7.76 9.63 L8.09 '
             '7.88 L8.59 6.32 L9.24 5.00 L10.00 3.98 L10.86 3.31 L11.76 3.01 L12.68 3.11 L13.56 3.59 L14.38 4.43 '
             'L15.09 5.60 L15.67 7.04" /></g></svg>',
 'SHAPE_INSTANCE': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
                   'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 '
                   '2.2L21.1 6.8V17.2L12 21.8L2.9 17.2V6.8Z M2.9 6.8L12 11.5L21.1 6.8 M12 11.5V21.8 M2.9 17.2L12 '
                   '11.5L21.1 17.2 M5.1 8L12 4.5L18.9 8L12 11.5Z M5.1 8V16L12 19.5L18.9 16V8"/></g></svg>',
 'SIZE': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
         'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9V3H17V17H9 '
         'M3 13V9 M3 17H9V23H3Z M20 3V5 M20 8V10 M20 13V15 M20 18V20"/></g></svg>',
 'SPEED': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
          'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7.2 21V3 M4.1 '
          '6.1L7.2 3L10.3 6.1 M7.2 17.2H21 M17.8 14L21 17.2L17.8 20.4 M7.2 17.2L3.1 21H1.8"/></g></svg>',
 'SPIN': '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24"><g fill="none" '
         'stroke="#E8EDF2" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5.1 20.2L4.1 '
         '18.7L3.3 16.8L2.8 14.7L2.7 12.5L3.1 10.2L4 8L5.4 6L7.2 4.4L9.3 3.3L11.6 2.8L13.9 2.9L16.1 3.6L18 4.9L19.5 '
         '6.7L20.4 8.8L20.7 11L20.4 13.1L19.5 15L18.1 16.3L16.3 17.1L14.4 17.1L12.7 16.4L11.5 15.1L10.9 13.4L11 '
         '11.8L11.8 10.4L13.2 9.6L14.6 9.8L15.6 10.8L15.8 12.1"/></g></svg>'}


def _bflow_depot_svg_icon_segments(identifier):
    """Load SVG paths and convert supported SVG geometry to line segments."""
    import re
    import math
    import xml.etree.ElementTree as ET
    cache = globals().setdefault("_BFLOW_SVG_ICON_SEGMENT_CACHE", {})
    if identifier in cache:
        return cache[identifier]

    svg_source = _BFLOW_EMBEDDED_SVG_ICONS.get(identifier)
    if svg_source is None:
        return None

    root = ET.fromstring(svg_source)
    segments = []
    token_re = re.compile(r"[MLHVZmlhvz]|[-+]?(?:\d*\.)?\d+(?:[eE][-+]?\d+)?")

    def add_poly(points, close=False):
        if len(points) < 2:
            return
        for p1, p2 in zip(points, points[1:]):
            segments.append((p1[0], p1[1], p2[0], p2[1]))
        if close:
            segments.append((points[-1][0], points[-1][1], points[0][0], points[0][1]))

    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "circle":
            cx, cy, radius = (float(elem.attrib[k]) for k in ("cx", "cy", "r"))
            pts = [(cx + math.cos(i * math.tau / 32) * radius,
                    cy + math.sin(i * math.tau / 32) * radius) for i in range(32)]
            add_poly(pts, True)
        elif tag == "path":
            tokens = token_re.findall(elem.attrib.get("d", ""))
            i = 0
            cmd = None
            x = y = 0.0
            start = (0.0, 0.0)
            points = []

            def number():
                nonlocal i
                value = float(tokens[i])
                i += 1
                return value

            def flush(close=False):
                nonlocal points
                add_poly(points, close)
                points = []

            while i < len(tokens):
                if tokens[i].isalpha():
                    cmd = tokens[i]
                    i += 1
                    if cmd in "Zz":
                        flush(True)
                        x, y = start
                        cmd = None
                        continue
                if cmd is None:
                    continue
                rel = cmd.islower()
                op = cmd.upper()
                if op in ("M", "L"):
                    nx, ny = number(), number()
                    if rel:
                        nx, ny = x + nx, y + ny
                    if op == "M":
                        flush()
                        x, y = nx, ny
                        start = (x, y)
                        points = [(x, y)]
                        cmd = "l" if rel else "L"
                    else:
                        x, y = nx, ny
                        points.append((x, y))
                elif op == "H":
                    nx = number()
                    x = x + nx if rel else nx
                    points.append((x, y))
                elif op == "V":
                    ny = number()
                    y = y + ny if rel else ny
                    points.append((x, y))
                elif op in ("C", "Q"):
                    x0, y0 = x, y
                    if op == "C":
                        vals = [number() for _ in range(6)]
                        c1 = (vals[0] + x0, vals[1] + y0) if rel else (vals[0], vals[1])
                        c2 = (vals[2] + x0, vals[3] + y0) if rel else (vals[2], vals[3])
                        end = (vals[4] + x0, vals[5] + y0) if rel else (vals[4], vals[5])
                        for step in range(1, 13):
                            t = step / 12.0
                            mt = 1.0 - t
                            px = mt**3*x0 + 3*mt**2*t*c1[0] + 3*mt*t**2*c2[0] + t**3*end[0]
                            py = mt**3*y0 + 3*mt**2*t*c1[1] + 3*mt*t**2*c2[1] + t**3*end[1]
                            points.append((px, py))
                    else:
                        vals = [number() for _ in range(4)]
                        c1 = (vals[0] + x0, vals[1] + y0) if rel else (vals[0], vals[1])
                        end = (vals[2] + x0, vals[3] + y0) if rel else (vals[2], vals[3])
                        for step in range(1, 13):
                            t = step / 12.0
                            mt = 1.0 - t
                            px = mt*mt*x0 + 2*mt*t*c1[0] + t*t*end[0]
                            py = mt*mt*y0 + 2*mt*t*c1[1] + t*t*end[1]
                            points.append((px, py))
                    x, y = end
                else:
                    # Unsupported SVG command: fail safely instead of drawing
                    # a malformed icon.
                    cache[identifier] = None
                    return None
            flush()

    cache[identifier] = segments
    return segments


def _bflow_depot_draw_vector_icon(identifier, x, y, size):
    """Render external 24x24 SVG line art through Blender's GPU line shader."""
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader

        source_segments = _bflow_depot_svg_icon_segments(identifier)
        if not source_segments:
            return False

        scale = float(size) / 24.0
        vertices = []
        for x1, y1, x2, y2 in source_segments:
            vertices.append((float(x) + x1 * scale, float(y) + (24.0 - y1) * scale))
            vertices.append((float(x) + x2 * scale, float(y) + (24.0 - y2) * scale))

        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        batch = batch_for_shader(shader, "LINES", {"pos": vertices})
        shader.bind()
        shader.uniform_float("color", (0.92, 0.95, 0.98, 1.0))
        old_width = None
        try:
            old_width = gpu.state.line_width_get()
            gpu.state.line_width_set(1.45)
        except Exception:
            old_width = None
        try:
            batch.draw(shader)
        finally:
            if old_width is not None:
                try:
                    gpu.state.line_width_set(old_width)
                except Exception:
                    pass
        return True
    except Exception as exc:
        print(f"[B-Flow] SVG icon draw failed for {identifier}: {exc}")
        return False


def _bflow_depot_rect(x, y, w, h, color):
    import gpu
    from gpu_extras.batch import batch_for_shader
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(
        shader, 'TRI_FAN',
        {"pos": [(x, y), (x+w, y), (x+w, y+h), (x, y+h)]}
    )
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)

def _bflow_depot_text(x, y, value, size=13, color=(0.95, 0.95, 0.95, 1.0)):
    import blf
    blf.position(0, x, y, 0)
    blf.size(0, size)
    blf.color(0, *color)
    blf.draw(0, value)

def _bflow_depot_available_width(area, window_region):
    """Reserve the right-side N-panel width so the depot never draws beneath it."""
    ui_region = next(
        (r for r in area.regions if r.type == 'UI' and r.width > 1),
        None
    )
    if ui_region is not None:
        right_edge = ui_region.x - window_region.x
        if right_edge > 100:
            return min(window_region.width, right_edge)
    return window_region.width


def _bflow_draw_bottom_depot(operator, context):
    # SpaceNodeEditor draw handlers are shared by every node-editor instance,
    # including Shader Editor and Compositor. Only draw in the exact area that
    # launched this modal operator, and only while it is editing B-Flow's tree.
    current_area = bpy.context.area
    current_space = bpy.context.space_data
    area = operator._area
    if (
        area is None
        or current_area is None
        or current_area.type != 'NODE_EDITOR'
        or current_area.as_pointer() != area.as_pointer()
        or getattr(current_space, "tree_type", "") != "BFLOW_Tree"
        or getattr(getattr(current_space, "edit_tree", None), "bl_idname", "") != "BFLOW_Tree"
    ):
        return

    import gpu

    # The tile matching the active Operator Stack item remains highlighted.
    active_identifier = None
    tree = bpy.data.node_groups.get(getattr(operator, "_tree_name", ""))
    active_node = tree.nodes.active if tree and tree.bl_idname == "BFLOW_Tree" else None
    if active_node and active_node.bl_idname == "BFLOW_EventNode":
        active_index = int(getattr(active_node, "active_operator", -1))
        if 0 <= active_index < len(active_node.operator_items):
            active_identifier = active_node.operator_items[active_index].operator_type
    if area is None:
        return
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    if region is None:
        return

    width = _bflow_depot_available_width(area, region)
    if width < 220:
        return

    x0, y0 = 8, 8
    cols = 5 if width >= 700 else (4 if width >= 540 else (3 if width >= 380 else 2))
    gap, tile_h = 6, 24
    rows = (len(_BFLOW_DEPOT_ITEMS) + cols - 1) // cols
    height = 62 + rows * (tile_h + 3)
    panel_w = width - 16
    usable = panel_w - 24
    tile_w = max(76, (usable - (cols-1)*gap) / cols)

    gpu.state.blend_set('ALPHA')
    _bflow_depot_rect(x0, y0, panel_w, height, (0.055, 0.065, 0.08, 0.96))
    _bflow_depot_text(x0+12, y0+height-22, "B-FLOW  |  OPERATOR DEPOT", 14)
    _bflow_depot_text(
        x0+12, y0+height-40,
        "Click an operator to add it to the selected Event", 11,
        (0.72, 0.76, 0.82, 1.0)
    )

    close_w, close_h = 22, 20
    close_x = x0 + panel_w - close_w - 7
    close_y = y0 + height - close_h - 5
    _bflow_depot_rect(close_x, close_y, close_w, close_h, (0.38, 0.13, 0.13, 1.0))
    _bflow_depot_text(close_x+7, close_y+4, "X", 12)
    operator._close_rect = (close_x, close_y, close_w, close_h)

    operator._tile_rects = []
    for i, (identifier, label) in enumerate(_BFLOW_DEPOT_ITEMS):
        col, row = i % cols, i // cols
        x = x0 + 12 + col*(tile_w+gap)
        y = y0 + 7 + row*(tile_h+3)
        is_active = identifier == active_identifier
        is_hover = identifier == getattr(operator, "_hover_identifier", None)

        if is_active and is_hover:
            tile_color = (0.18, 0.50, 0.68, 1.0)
        elif is_active:
            tile_color = (0.12, 0.36, 0.54, 1.0)
        elif is_hover:
            tile_color = (0.25, 0.34, 0.46, 1.0)
        else:
            tile_color = (0.16, 0.22, 0.30, 1.0)

        _bflow_depot_rect(x, y, tile_w, tile_h, tile_color)
        if is_active:
            # A slim accent bar makes the active tile easy to identify.
            _bflow_depot_rect(x, y, 3, tile_h, (0.30, 0.72, 0.95, 1.0))
        icon_drawn = _bflow_depot_draw_vector_icon(identifier, x + 5, y + 4, 16)
        text_x = x + 27 if icon_drawn else x + 8
        _bflow_depot_text(text_x, y+7, label, 10)
        operator._tile_rects.append((x, y, tile_w, tile_h, identifier, label))
    gpu.state.blend_set('NONE')

class BFLOW_OT_bottom_depot(Operator):
    """Display a draggable Operator Depot along the bottom of the Node Editor."""
    bl_idname = "bflow.bottom_depot"
    bl_label = "Show Bottom Operator Depot"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        area = context.area
        space = context.space_data
        if (not area or area.type != 'NODE_EDITOR' or
                not getattr(space, "edit_tree", None) or
                space.edit_tree.bl_idname != "BFLOW_Tree"):
            self.report({'WARNING'}, "Open the B-Flow Event Editor first")
            return {'CANCELLED'}

        self._area = area
        self._tree_name = space.edit_tree.name
        self._area_key = (
            context.window.as_pointer() if context.window else 0,
            area.as_pointer(),
        )
        # Track this running modal Depot, and mark the editor as initialized
        # so closing it manually will not make the startup timer reopen it.
        _BFLOW_DEPOT_ACTIVE_AREAS.add(self._area_key)
        _BFLOW_DEPOT_INITIALIZED_AREAS.add(self._area_key)
        self._drag_identifier = None
        self._drag_label = ""
        self._mouse_region = (0, 0)
        self._tile_rects = []
        self._hover_identifier = None
        self._draw_handle = bpy.types.SpaceNodeEditor.draw_handler_add(
            _bflow_draw_bottom_depot, (self, context), 'WINDOW', 'POST_PIXEL'
        )
        context.window_manager.modal_handler_add(self)
        area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        handle = getattr(self, "_draw_handle", None)
        if handle is not None:
            bpy.types.SpaceNodeEditor.draw_handler_remove(handle, 'WINDOW')
            self._draw_handle = None

        area_key = getattr(self, "_area_key", None)
        if area_key is not None:
            _BFLOW_DEPOT_ACTIVE_AREAS.discard(area_key)

        # Closing the bottom window also folds the right-side command list.
        # The user can reopen both together from the foldout header.
        try:
            if context.scene and hasattr(context.scene, "bflow_depot_commands_expanded"):
                context.scene.bflow_depot_commands_expanded = False
        except Exception:
            pass

        if getattr(self, "_area", None):
            self._area.tag_redraw()

    def modal(self, context, event):
        area = self._area
        if area is None:
            self._finish(context)
            return {'CANCELLED'}

        # The modal handler also receives events from other editors in the
        # same window. Do not react to clicks/keys outside the B-Flow graph.
        event_area = context.area
        if event_area is not None and event_area.as_pointer() != area.as_pointer():
            return {'PASS_THROUGH'}

        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._finish(context)
            return {'CANCELLED'}

        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        if region is None:
            return {'PASS_THROUGH'}

        mx, my = event.mouse_x-region.x, event.mouse_y-region.y

        if event.type == 'MOUSEMOVE':
            hover_identifier = None
            for x, y, w, h, identifier, label in self._tile_rects:
                if x <= mx <= x+w and y <= my <= y+h:
                    hover_identifier = identifier
                    break
            if hover_identifier != getattr(self, "_hover_identifier", None):
                self._hover_identifier = hover_identifier
                area.tag_redraw()
            return {'PASS_THROUGH'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            close_rect = getattr(self, "_close_rect", None)
            if close_rect:
                x, y, w, h = close_rect
                if x <= mx <= x+w and y <= my <= y+h:
                    self._finish(context)
                    return {'CANCELLED'}

            for x, y, w, h, identifier, label in self._tile_rects:
                if x <= mx <= x+w and y <= my <= y+h:
                    tree = bpy.data.node_groups.get(self._tree_name)
                    node = tree.nodes.active if tree and tree.bl_idname == "BFLOW_Tree" else None
                    if node and node.bl_idname == "BFLOW_EventNode":
                        item = node.operator_items.add()
                        item.operator_type = identifier
                        if item.operator_type == "FORCE" and len(item.force_fields) == 0:
                            item.force_fields.add()
                        if item.operator_type == "RAND_COLOR":
                            _bflow_seed_rand_color_palette(item)
                        if item.operator_type == "SHAPE_INSTANCE":
                            _bflow_seed_shape_instance_list(item)
                        node.active_operator = len(node.operator_items)-1
                        self._hover_identifier = identifier
                        _bflow_schedule_live_update(node, context)
                        area.tag_redraw()
                    else:
                        self.report({'INFO'}, "Select an Event node first")
                    return {'RUNNING_MODAL'}

        # No drag interaction: let Blender handle ordinary node selection,
        # panning, zooming and node editing.
        return {'PASS_THROUGH'}


class BFLOW_OT_toggle_depot_commands(Operator):
    bl_idname = "bflow.toggle_depot_commands"
    bl_label = "Show Bottom Operator Depot"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        scene = context.scene
        scene.bflow_depot_commands_expanded = not scene.bflow_depot_commands_expanded

        if scene.bflow_depot_commands_expanded:
            area = context.area
            window = context.window
            if area and area.type == 'NODE_EDITOR' and window:
                node_space = area.spaces.active
                tree = getattr(node_space, "edit_tree", None)
                if tree and tree.bl_idname == "BFLOW_Tree":
                    window_region = next(
                        (region for region in area.regions if region.type == 'WINDOW'),
                        None
                    )
                    if window_region:
                        try:
                            area_key = (window.as_pointer(), area.as_pointer())
                            if area_key not in _BFLOW_DEPOT_ACTIVE_AREAS:
                                with bpy.context.temp_override(
                                    window=window,
                                    area=area,
                                    region=window_region,
                                ):
                                    bpy.ops.bflow.bottom_depot('INVOKE_DEFAULT')
                        except Exception as exc:
                            print("[B-Flow] Could not reopen bottom Depot:", exc)

        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


class BFLOW_OT_add_depot_operator(Operator):
    bl_idname = "bflow.add_depot_operator"
    bl_label = "Add Operator to Selected Event"

    operator_type: EnumProperty(name="Operator", items=OPERATOR_ITEMS, default="BIRTH")

    def execute(self, context):
        tree, node = active_event(context)
        if not tree or not node:
            self.report({'WARNING'}, "Select an Event node first")
            return {'CANCELLED'}
        item = node.operator_items.add()
        item.operator_type = self.operator_type
        if item.operator_type == "FORCE" and len(item.force_fields) == 0:
            item.force_fields.add()
        if item.operator_type == "RAND_COLOR":
            _bflow_seed_rand_color_palette(item)
        node.active_operator = len(node.operator_items)-1
        tree.nodes.active = node
        _bflow_schedule_live_update(node, context)
        return {'FINISHED'}


class BFLOW_OT_select_operator(Operator):
    bl_idname = "bflow.select_operator"
    bl_label = "Select Operator"
    tree_name: StringProperty()
    node_name: StringProperty()
    index: IntProperty()

    def execute(self, context):
        tree, node = get_event(self.tree_name, self.node_name)
        if not node or self.index >= len(node.operator_items):
            return {'CANCELLED'}
        node.active_operator = self.index
        tree.nodes.active = node
        return {'FINISHED'}

class BFLOW_OT_remove_operator(Operator):
    bl_idname = "bflow.remove_operator"
    bl_label = "Remove Operator"
    tree_name: StringProperty()
    node_name: StringProperty()
    index: IntProperty()

    def execute(self, context):
        tree, node = get_event(self.tree_name, self.node_name)
        if not node or self.index >= len(node.operator_items):
            return {'CANCELLED'}
        node.operator_items.remove(self.index)
        node.active_operator = min(node.active_operator, max(0, len(node.operator_items)-1))
        _bflow_schedule_live_update(node, context)
        return {'FINISHED'}

class BFLOW_OT_move_operator(Operator):
    bl_idname = "bflow.move_operator"
    bl_label = "Move Operator"
    tree_name: StringProperty()
    node_name: StringProperty()
    index: IntProperty()
    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        tree, node = get_event(self.tree_name, self.node_name)
        if not node:
            return {'CANCELLED'}
        target = self.index + (-1 if self.direction == "UP" else 1)
        if self.index < 0 or self.index >= len(node.operator_items) or target < 0 or target >= len(node.operator_items):
            return {'CANCELLED'}
        node.operator_items.move(self.index, target)
        node.active_operator = target
        _bflow_schedule_live_update(node, context)
        return {'FINISHED'}

class BFLOW_OT_toggle_operator(Operator):
    bl_idname = "bflow.toggle_operator"
    bl_label = "Enable / Disable Operator"
    tree_name: StringProperty()
    node_name: StringProperty()
    index: IntProperty()

    def execute(self, context):
        tree, node = get_event(self.tree_name, self.node_name)
        if not node or self.index >= len(node.operator_items):
            return {'CANCELLED'}
        node.operator_items[self.index].enabled = not node.operator_items[self.index].enabled
        _bflow_schedule_live_update(node, context)
        return {'FINISHED'}

class BFLOW_OT_migrate_legacy(Operator):
    bl_idname = "bflow.migrate_legacy"
    bl_label = "Migrate Old Operator Nodes"
    bl_description = "Move legacy standalone Operator nodes into the nearest Event stack"

    def execute(self, context):
        tree = context.scene.bflow_graph
        if not tree:
            self.report({'WARNING'}, "No B-Flow graph selected")
            return {'CANCELLED'}
        events = [n for n in tree.nodes if n.bl_idname == "BFLOW_EventNode"]
        legacy = [n for n in tree.nodes if n.bl_idname == "BFLOW_OperatorNode"]
        if not events or not legacy:
            self.report({'INFO'}, "No legacy Operator nodes to migrate")
            return {'FINISHED'}
        migrated = 0
        for old in list(legacy):
            nearest = min(events, key=lambda e: (e.location - old.location).length)
            item = nearest.operator_items.add()
            item.operator_type = old.operator_type
            for attr in ("count", "speed", "gravity", "direction", "lifetime",
                         "threshold", "size", "random_amount", "child_count",
                         "speed_randomness", "collision_height", "bounce", "bounce_randomness",
                         "friction", "friction_randomness", "particle_radius",
                         "collider", "source_object", "curve_object", "material"):
                if hasattr(old, attr) and hasattr(item, attr):
                    try:
                        setattr(item, attr, getattr(old, attr))
                    except Exception:
                        pass
            migrated += 1
            tree.nodes.remove(old)
        self.report({'INFO'}, f"Migrated {migrated} Operators into Event stacks")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Geometry Nodes simulation compiler (integrated into Event Stack)
# ---------------------------------------------------------------------------

def _bflow_assign_node_editor(area, tree):
    """Turn an Area into the B-Flow node editor and attach the graph."""
    area.type = 'NODE_EDITOR'
    area.ui_type = 'BFLOW_Tree'
    node_space = next(
        (space for space in area.spaces if space.type == 'NODE_EDITOR'),
        None
    )
    if node_space is None:
        raise RuntimeError("Blender did not create a Node Editor space")
    node_space.tree_type = 'BFLOW_Tree'
    node_space.node_tree = tree
    return True


def _bflow_activate_sidebar_category(window_ptr, area_ptr, attempts=None):
    """Select the B-Flow N-panel tab after Blender has registered its categories."""
    if attempts is None:
        attempts = {"count": 0}
    attempts["count"] += 1

    try:
        window = next(
            (win for win in bpy.context.window_manager.windows
             if win.as_pointer() == window_ptr),
            None,
        )
        if window is None or window.screen is None:
            return None
        area = next(
            (a for a in window.screen.areas
             if a.as_pointer() == area_ptr and a.type == 'NODE_EDITOR'),
            None,
        )
        if area is None:
            return None
        space = next(
            (sp for sp in area.spaces if sp.type == 'NODE_EDITOR'),
            None,
        )
        if space is None or getattr(space, "tree_type", "") != 'BFLOW_Tree':
            return None

        space.show_region_ui = True
        ui_region = next((r for r in area.regions if r.type == 'UI'), None)
        if ui_region is None or not hasattr(ui_region, "active_panel_category"):
            return 0.15 if attempts["count"] < 20 else None

        # Panel categories are generated only after the UI region has drawn.
        # Retrying on subsequent UI cycles avoids assigning the tab too early,
        # which leaves Blender on its previous tab (often Group).
        try:
            ui_region.active_panel_category = "B-Flow"
            ui_region.tag_redraw()
            area.tag_redraw()
            return None
        except (TypeError, ValueError, RuntimeError):
            if attempts["count"] < 20:
                return 0.15
            raise
    except Exception as exc:
        print("[B-Flow] Could not activate the B-Flow sidebar:", exc)
        return None


def _bflow_prepare_editor_ui(window, area):
    """Open the B-Flow sidebar tab and bottom Operator Depot for an editor."""
    if window is None or area is None or area.type != 'NODE_EDITOR':
        return False
    space = area.spaces.active
    if getattr(space, "tree_type", "") != 'BFLOW_Tree':
        return False

    # Open the N-panel now, then select its B-Flow category on a later UI
    # cycle. Blender creates the category enum only after panel registration
    # has been drawn once in this editor.
    try:
        space.show_region_ui = True
        window_ptr = window.as_pointer()
        area_ptr = area.as_pointer()
        bpy.app.timers.register(
            lambda: _bflow_activate_sidebar_category(window_ptr, area_ptr),
            first_interval=0.25,
        )
    except Exception as exc:
        print("[B-Flow] Could not schedule B-Flow sidebar activation:", exc)

    # Start the floating bottom Depot only after Blender has created the
    # Node Editor's WINDOW region. The timer-based startup remains as a fallback.
    try:
        area_key = (window.as_pointer(), area.as_pointer())
        if area_key not in _BFLOW_DEPOT_ACTIVE_AREAS:
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region is not None:
                with bpy.context.temp_override(window=window, area=area, region=region):
                    result = bpy.ops.bflow.bottom_depot('INVOKE_DEFAULT')
                if 'RUNNING_MODAL' in result:
                    _BFLOW_DEPOT_ACTIVE_AREAS.add(area_key)
                    _BFLOW_DEPOT_INITIALIZED_AREAS.add(area_key)
    except Exception as exc:
        print("[B-Flow] Could not open the bottom Operator Depot:", exc)
    area.tag_redraw()
    return True


def _bflow_frame_initial_event_above_depot(window_ptr, area_ptr, tree_name, node_name, attempts=None):
    """Frame the new Event node and shift it above the floating bottom Depot."""
    if attempts is None:
        attempts = {"count": 0}
    attempts["count"] += 1
    try:
        window = next(
            (win for win in bpy.context.window_manager.windows
             if win.as_pointer() == window_ptr),
            None,
        )
        if window is None or window.screen is None:
            return None
        area = next(
            (a for a in window.screen.areas
             if a.as_pointer() == area_ptr and a.type == 'NODE_EDITOR'),
            None,
        )
        if area is None:
            return 0.15 if attempts["count"] < 12 else None
        space = area.spaces.active
        tree = bpy.data.node_groups.get(tree_name)
        node = tree.nodes.get(node_name) if tree else None
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        if (not region or not node or getattr(space, "edit_tree", None) != tree):
            return 0.15 if attempts["count"] < 12 else None

        # Center the initial Event node first. Then move the visible view upward
        # by roughly half the Depot's occupied height, leaving the node clear
        # of the floating overlay while keeping the Depot anchored at the bottom.
        with bpy.context.temp_override(window=window, area=area, region=region):
            try:
                bpy.ops.node.view_selected()
            except Exception:
                pass

        try:
            view = region.view2d
            cur = tuple(view.cur)
            view_width = cur[2] - cur[0]
            view_height = cur[3] - cur[1]
            if view_width > 0 and view_height > 0:
                # The first framing pass can leave the Event too small in a
                # narrow editor. Zoom in around the current view center so the
                # node title and operator labels are comfortably readable.
                zoom = 0.74
                center_x = (cur[0] + cur[2]) * 0.5
                center_y = (cur[1] + cur[3]) * 0.5
                half_w = view_width * zoom * 0.5
                half_h = view_height * zoom * 0.5
                left, right = center_x - half_w, center_x + half_w
                bottom, top = center_y - half_h, center_y + half_h

                # Reserve substantially more vertical room for the floating
                # Depot. This larger offset keeps the entire initial Event
                # above the overlay even in a narrow, two-column layout.
                shift = (top - bottom) * 0.48
                view.cur = (left, bottom - shift, right, top - shift)
        except Exception as exc:
            print("[B-Flow] Could not zoom/offset the initial Event above the Depot:", exc)

        area.tag_redraw()
        return None
    except Exception as exc:
        print("[B-Flow] Could not frame the initial Event:", exc)
        return None


def _bflow_open_split_layout(context, tree):
    """Split once, then convert the right-hand Area after Blender updates the screen."""
    window = context.window
    if window is None or window.screen is None:
        return False

    screen = window.screen

    # If the B-Flow editor already exists, simply attach the requested graph.
    existing = next(
        (
            area for area in screen.areas
            if area.type == 'NODE_EDITOR'
            and (
                getattr(area, "ui_type", "") == 'BFLOW_Tree'
                or getattr(area.spaces.active, "tree_type", "") == 'BFLOW_Tree'
            )
        ),
        None
    )
    if existing is not None:
        try:
            _bflow_assign_node_editor(existing, tree)
            _bflow_prepare_editor_ui(window, existing)
            initial_event = next(
                (n for n in tree.nodes if n.bl_idname == "BFLOW_EventNode"), None
            )
            if initial_event is not None:
                window_ptr = window.as_pointer()
                area_ptr = existing.as_pointer()
                tree_name = tree.name
                node_name = initial_event.name
                bpy.app.timers.register(
                    lambda: _bflow_frame_initial_event_above_depot(
                        window_ptr, area_ptr, tree_name, node_name
                    ),
                    first_interval=0.65,
                )
            return True
        except Exception as exc:
            print("[B-Flow] Could not attach graph to existing editor:", exc)
            return False

    # Recover gracefully from the previous layout attempt: if two 3D Views
    # already exist, convert the rightmost one instead of splitting again.
    view_areas = sorted(
        [area for area in screen.areas if area.type == 'VIEW_3D'],
        key=lambda area: area.x
    )
    if len(view_areas) >= 2:
        try:
            _bflow_assign_node_editor(view_areas[-1], tree)
            _bflow_prepare_editor_ui(window, view_areas[-1])
            target_area = view_areas[-1]
            initial_event = next(
                (n for n in tree.nodes if n.bl_idname == "BFLOW_EventNode"), None
            )
            if initial_event is not None:
                window_ptr = window.as_pointer()
                area_ptr = target_area.as_pointer()
                tree_name = tree.name
                node_name = initial_event.name
                bpy.app.timers.register(
                    lambda: _bflow_frame_initial_event_above_depot(
                        window_ptr, area_ptr, tree_name, node_name
                    ),
                    first_interval=0.65,
                )
            return True
        except Exception as exc:
            print("[B-Flow] Could not convert right-hand 3D View:", exc)
            return False

    source_area = context.area
    screen_area_ptrs = {area.as_pointer() for area in screen.areas}
    source_ptr = source_area.as_pointer() if source_area is not None else None
    if source_ptr not in screen_area_ptrs:
        source_area = next((area for area in screen.areas if area.type == 'VIEW_3D'), None)
    if source_area is None:
        print("[B-Flow] No source Area available for workspace split.")
        return False

    # Store only primitive values in the timer; do not retain invalidatable RNA refs.
    window_ptr = window.as_pointer()
    bounds = (source_area.x, source_area.y, source_area.width, source_area.height)
    source_region = next((region for region in source_area.regions if region.type == 'WINDOW'), None)
    if source_region is None:
        return False

    try:
        with bpy.context.temp_override(window=window, area=source_area, region=source_region):
            bpy.ops.screen.area_split(direction='VERTICAL', factor=0.5)
    except Exception as exc:
        print("[B-Flow] Could not split editor area:", exc)
        return False

    tree_name = tree.name
    attempts = {"count": 0}

    def _finish_layout():
        attempts["count"] += 1
        target_window = next(
            (win for win in bpy.context.window_manager.windows
             if win.as_pointer() == window_ptr),
            None
        )
        if target_window is None or target_window.screen is None:
            return None

        x, y, width, height = bounds
        overlapping = [
            area for area in target_window.screen.areas
            if area.x < x + width
            and area.x + area.width > x
            and area.y < y + height
            and area.y + area.height > y
        ]
        overlapping.sort(key=lambda area: area.x)

        # After the split there should be two areas in the original rectangle.
        if len(overlapping) >= 2:
            right_area = overlapping[-1]
            target_tree = bpy.data.node_groups.get(tree_name)
            if target_tree is None:
                return None
            try:
                _bflow_assign_node_editor(right_area, target_tree)
                _bflow_prepare_editor_ui(target_window, right_area)
                initial_event = next(
                    (n for n in target_tree.nodes if n.bl_idname == "BFLOW_EventNode"),
                    None,
                )
                if initial_event is not None:
                    right_area_ptr = right_area.as_pointer()
                    target_window_ptr = target_window.as_pointer()
                    target_tree_name = target_tree.name
                    initial_event_name = initial_event.name
                    bpy.app.timers.register(
                        lambda: _bflow_frame_initial_event_above_depot(
                            target_window_ptr, right_area_ptr,
                            target_tree_name, initial_event_name
                        ),
                        first_interval=0.65,
                    )
                # Keep the left side as a 3D Viewport.
                left_area = overlapping[0]
                if left_area.type != 'VIEW_3D':
                    left_area.type = 'VIEW_3D'
                return None
            except Exception as exc:
                print("[B-Flow] Deferred editor conversion failed:", exc)
                return None

        # Blender may finish the screen split on the next UI cycle.
        if attempts["count"] < 12:
            return 0.1
        print("[B-Flow] Timed out waiting for the split Areas.")
        return None

    try:
        bpy.app.timers.register(_finish_layout, first_interval=0.15)
    except Exception as exc:
        print("[B-Flow] Could not schedule editor conversion:", exc)
        return False
    return True


def _bflow_rigidbody_override(context):
    """Find a usable area/region override for Blender rigid-body operators."""
    window = context.window
    if window is None:
        return None

    screen = window.screen
    # Prefer the 3D View because Blender's physics operators are primarily
    # designed around Object Mode / View3D context. Fall back to the current
    # editor only when the split layout has no 3D View area.
    candidate_areas = [area for area in screen.areas if area.type == 'VIEW_3D']
    if context.area is not None and context.area not in candidate_areas:
        candidate_areas.append(context.area)
    candidate_areas.extend(
        area for area in screen.areas
        if area not in candidate_areas and area.type in {'NODE_EDITOR', 'OUTLINER'}
    )

    for area in candidate_areas:
        if area.type not in {'VIEW_3D', 'NODE_EDITOR', 'OUTLINER'}:
            continue
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        if region is not None:
            return {
                "window": window,
                "area": area,
                "region": region,
                "scene": context.scene,
                "view_layer": context.view_layer,
            }
    return None


def _bflow_add_rigidbody_batch(context, objects, body_type):
    """Add Blender rigid-body settings to a batch of selected objects."""
    objects = [obj for obj in objects if obj is not None and obj.name in bpy.data.objects]
    if not objects:
        return True

    override = _bflow_rigidbody_override(context)
    if override is None:
        raise RuntimeError("Could not find a Blender editor area for Rigid Body operators.")

    view_layer = context.view_layer
    old_active = view_layer.objects.active
    old_selected = list(context.selected_objects)

    try:
        for selected in old_selected:
            try:
                selected.select_set(False)
            except Exception:
                pass

        for obj in objects:
            obj.select_set(True)
        view_layer.objects.active = objects[0]

        with bpy.context.temp_override(
            **override,
            object=objects[0],
            active_object=objects[0],
            selected_objects=objects,
            selected_editable_objects=objects,
        ):
            result = bpy.ops.rigidbody.objects_add(type=body_type)

        if 'FINISHED' not in result:
            raise RuntimeError(f"Blender did not add {body_type.title()} Rigid Bodies.")
        return True
    finally:
        for obj in objects:
            try:
                obj.select_set(False)
            except Exception:
                pass
        for selected in old_selected:
            if selected.name in bpy.data.objects:
                try:
                    selected.select_set(True)
                except Exception:
                    pass
        try:
            view_layer.objects.active = old_active if old_active and old_active.name in bpy.data.objects else None
        except Exception:
            pass


def _bflow_remove_rigidbody_batch(context, objects):
    """Remove Rigid Body settings from a batch of objects."""
    objects = [
        obj for obj in objects
        if obj is not None and obj.name in bpy.data.objects and obj.rigid_body is not None
    ]
    if not objects:
        return True

    override = _bflow_rigidbody_override(context)
    if override is None:
        raise RuntimeError("Could not find a Blender editor area for Rigid Body removal.")

    view_layer = context.view_layer
    old_active = view_layer.objects.active
    old_selected = list(context.selected_objects)
    try:
        for selected in old_selected:
            try:
                selected.select_set(False)
            except Exception:
                pass
        for obj in objects:
            obj.select_set(True)
        view_layer.objects.active = objects[0]

        with bpy.context.temp_override(
            **override,
            object=objects[0],
            active_object=objects[0],
            selected_objects=objects,
            selected_editable_objects=objects,
        ):
            result = bpy.ops.rigidbody.objects_remove()

        if 'FINISHED' not in result:
            raise RuntimeError("Blender did not remove the generated Passive rigid bodies.")
        return True
    finally:
        for obj in objects:
            try:
                obj.select_set(False)
            except Exception:
                pass
        for selected in old_selected:
            if selected.name in bpy.data.objects:
                try:
                    selected.select_set(True)
                except Exception:
                    pass
        try:
            view_layer.objects.active = old_active if old_active and old_active.name in bpy.data.objects else None
        except Exception:
            pass


def _bflow_ensure_rigidbody_collection(scene, world, attribute_name, collection_name):
    """Ensure a Rigid Body World collection exists, is assigned, and is linked.

    Blender can create a RigidBodyWorld datablock without assigning its
    collection slots. The native rigid-body operators then fail with
    'Rigid Body World has no collection'. B-Flow owns only collections that
    it creates here; an existing user-assigned collection is preserved.
    """
    collection = getattr(world, attribute_name, None)
    if collection is None:
        scene_tag = scene.name

        # Reuse a B-Flow collection already created for this scene, if present.
        collection = next(
            (
                candidate for candidate in bpy.data.collections
                if candidate.get("bflow_rb_collection_role") == attribute_name
                and candidate.get("bflow_rb_collection_scene") == scene_tag
            ),
            None,
        )

        if collection is None:
            collection = bpy.data.collections.new(collection_name)
            collection["bflow_rb_collection_role"] = attribute_name
            collection["bflow_rb_collection_scene"] = scene_tag

        # Rigid Body World collections must also be part of the scene tree.
        if not any(child == collection for child in scene.collection.children):
            scene.collection.children.link(collection)

        try:
            setattr(world, attribute_name, collection)
        except Exception as exc:
            raise RuntimeError(
                f"Could not assign {collection_name} to Rigid Body World: {exc}"
            )

    # A pre-existing user collection may be assigned but not linked to this
    # scene's collection tree. Link it without changing its contents.
    if not any(child == collection for child in scene.collection.children):
        try:
            scene.collection.children.link(collection)
        except Exception as exc:
            raise RuntimeError(
                f"Rigid Body World collection '{collection.name}' is not linked "
                f"to this scene: {exc}"
            )

    assigned = getattr(world, attribute_name, None)
    if assigned is None:
        raise RuntimeError(
            f"Rigid Body World still has no {attribute_name} collection."
        )
    return assigned


def _bflow_ensure_rigidbody_world(context):
    scene = context.scene
    if scene.rigidbody_world is None:
        override = _bflow_rigidbody_override(context)
        if override is None:
            raise RuntimeError("Could not find an editor area to create Rigid Body World.")

        with bpy.context.temp_override(**override):
            result = bpy.ops.rigidbody.world_add()

        if 'FINISHED' not in result or scene.rigidbody_world is None:
            raise RuntimeError("Blender could not create the Rigid Body World.")

    world = scene.rigidbody_world

    # Always repair missing collection slots, including a World that already
    # existed before B-Flow was installed or that was created incompletely.
    _bflow_ensure_rigidbody_collection(
        scene,
        world,
        "collection",
        "B-Flow Rigid Body Objects",
    )
    _bflow_ensure_rigidbody_collection(
        scene,
        world,
        "constraints",
        "B-Flow Rigid Body Constraints",
    )

    if world.collection is None:
        raise RuntimeError("Rigid Body World has no object collection after initialization.")
    return world


def _bflow_collect_preview_instances_at_current_frame(
    context,
    preview,
    copy_mesh_data=False,
):
    """Collect evaluated B-Flow instances with stable keys and optional mesh snapshots.

    Evaluated Mesh datablocks obtained from depsgraph object instances can be
    freed when the scene frame changes. When copy_mesh_data=True, make one
    persistent bpy.data.meshes copy per evaluated instance immediately,
    while that instance is still valid. Never return an evaluated Mesh pointer
    to code that will change the timeline frame.
    """
    depsgraph = context.evaluated_depsgraph_get()
    context.view_layer.update()
    preview_eval = preview.evaluated_get(depsgraph)

    instances = []
    fallback_counts = {}

    for instance in depsgraph.object_instances:
        if not getattr(instance, "is_instance", False):
            continue

        parent = getattr(instance, "parent", None)
        parent_matches = (parent == preview_eval)
        if not parent_matches:
            try:
                parent_matches = getattr(parent, "original", None) == preview
            except Exception:
                parent_matches = False
        if not parent_matches:
            continue

        instance_obj = getattr(instance, "object", None)
        if instance_obj is None or instance_obj.type != 'MESH' or instance_obj.data is None:
            continue

        try:
            matrix = instance.matrix_world.copy()

            # Use original datablock identity for matching the same instanced
            # source across frames. Do not use the evaluated Mesh pointer:
            # evaluated meshes are temporary and can be reallocated per frame.
            original_obj = getattr(instance_obj, "original", None)
            original_data = getattr(original_obj, "data", None) if original_obj else None
            if original_data is not None and original_data.bl_rna.identifier == "Mesh":
                mesh_key = int(original_data.as_pointer())
            elif original_obj is not None:
                mesh_key = int(original_obj.as_pointer())
            else:
                # Last-resort stable key for unusual Geometry Nodes instances.
                mesh_key = (str(instance_obj.name), int(instance_obj.type == 'MESH'))

            persistent_id = tuple(
                int(value) for value in getattr(instance, "persistent_id", ())
            )
            random_id = int(getattr(instance, "random_id", -1))

            if persistent_id:
                instance_key = (mesh_key, persistent_id, random_id)
            else:
                ordinal = fallback_counts.get(mesh_key, 0)
                fallback_counts[mesh_key] = ordinal + 1
                instance_key = (mesh_key, "ORDINAL", ordinal)

            mesh_snapshot = None
            if copy_mesh_data:
                # IMPORTANT: copy the evaluated mesh PER INSTANCE, not per
                # original datablock key. Geometry Nodes can emit different
                # Shape Instance geometries whose depsgraph instances share
                # the same original object/data identity. Caching by mesh_key
                # collapses Torus/Cone/Suzanne into whichever mesh is visited
                # first. Keep mesh_key only for temporal identity matching;
                # the actual evaluated instance mesh must remain independent.
                mesh_snapshot = instance_obj.data.copy()
                mesh_snapshot.name = (
                    f"B-Flow RB Snapshot {len(instances) + 1:04d}"
                )
                mesh_snapshot["bflow_rb_snapshot_mesh"] = True
                mesh_snapshot["bflow_rb_source_instance_key"] = str(instance_key)

            instances.append((matrix, mesh_key, mesh_snapshot, instance_key))
        except ReferenceError:
            # Evaluated object/mesh was invalidated during iteration; skip it
            # rather than returning a dangling StructRNA into the build stage.
            continue
        except Exception as exc:
            print("[B-Flow] Could not collect an evaluated particle instance:", exc)
            continue

    return instances



def _bflow_capture_instance_colors(context, preview, instances):
    """Read the evaluated per-instance bflow_color values before RB conversion.

    A temporary Realize Instances modifier exposes the instancer attribute on
    realized mesh vertices. Each source instance is matched back to those
    vertices by its transformed source-mesh coordinates.
    """
    if not instances:
        return [None] * 0

    scene = context.scene
    depsgraph = context.evaluated_depsgraph_get()
    temp_group = None
    temp_modifier = None
    realized_mesh = None
    preview_eval_ref = None
    original_frame = int(scene.frame_current)
    original_subframe = float(scene.frame_subframe)

    try:
        # Build a tiny pass-through Geometry Nodes group that realizes only
        # the final display instances; the original B-Flow node group is untouched.
        temp_group = bpy.data.node_groups.new(
            "B-Flow RB Color Capture (Temporary)", "GeometryNodeTree"
        )
        temp_group.interface.new_socket(
            name="Geometry", in_out='INPUT', socket_type="NodeSocketGeometry"
        )
        temp_group.interface.new_socket(
            name="Geometry", in_out='OUTPUT', socket_type="NodeSocketGeometry"
        )
        nodes = temp_group.nodes
        links = temp_group.links
        group_in = nodes.new("NodeGroupInput")
        group_out = nodes.new("NodeGroupOutput")
        realize = nodes.new("GeometryNodeRealizeInstances")
        links.new(group_in.outputs["Geometry"], realize.inputs["Geometry"])
        links.new(realize.outputs["Geometry"], group_out.inputs["Geometry"])

        temp_modifier = preview.modifiers.new(
            name="B-Flow RB Color Capture (Temporary)", type='NODES'
        )
        temp_modifier.node_group = temp_group

        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()
        preview_eval = preview.evaluated_get(depsgraph)
        preview_eval_ref = preview_eval
        try:
            realized_mesh = preview_eval.to_mesh(
                preserve_all_data_layers=True, depsgraph=depsgraph
            )
        except TypeError:
            realized_mesh = preview_eval.to_mesh()

        color_attribute = (
            realized_mesh.attributes.get("bflow_color")
            if realized_mesh is not None else None
        )
        if color_attribute is None or color_attribute.domain != 'POINT':
            return [None] * len(instances)

        # KDTree lookup uses world-space coordinates so object transforms and
        # per-instance transforms are both respected.
        from mathutils.kdtree import KDTree
        preview_matrix = preview.matrix_world.copy()
        vertex_count = len(realized_mesh.vertices)
        if vertex_count == 0:
            return [None] * len(instances)

        kd = KDTree(vertex_count)
        for index, vertex in enumerate(realized_mesh.vertices):
            kd.insert(preview_matrix @ vertex.co, index)
        kd.balance()

        colors = [tuple(item.color) for item in color_attribute.data]
        captured = []
        for (
            matrix, _mesh_key, source_mesh, _instance_key,
            *_motion
        ) in instances:
            if source_mesh is None or len(source_mesh.vertices) == 0:
                captured.append(None)
                continue

            samples = []
            # Match every source vertex to the corresponding realized vertex.
            # The nearest-neighbor strategy is resilient to arbitrary source
            # object origins and does not assume particle order == mesh order.
            for vertex in source_mesh.vertices:
                world_co = matrix @ vertex.co
                _co, nearest_index, distance = kd.find(world_co)
                # The realized Geometry Nodes mesh and depsgraph instance
                # matrices can differ by small floating-point tolerances.
                # Always use the nearest realized vertex; a hard 1e-3 cutoff
                # incorrectly left some particles without their Rand Color.
                if nearest_index is not None:
                    samples.append(colors[nearest_index])

            if not samples:
                captured.append(None)
                continue

            count = float(len(samples))
            captured.append(tuple(
                sum(sample[channel] for sample in samples) / count
                for channel in range(4)
            ))
        return captured

    except Exception as exc:
        print("[B-Flow] Could not capture per-instance rigid-body colors:", exc)
        return [None] * len(instances)

    finally:
        if preview_eval_ref is not None and realized_mesh is not None:
            try:
                preview_eval_ref.to_mesh_clear()
            except Exception:
                pass
        if preview is not None and temp_modifier is not None:
            try:
                preview.modifiers.remove(temp_modifier)
            except Exception:
                pass
        if temp_group is not None:
            try:
                bpy.data.node_groups.remove(temp_group)
            except Exception:
                pass
        try:
            scene.frame_set(original_frame, subframe=original_subframe)
            context.view_layer.update()
        except Exception:
            pass


def _bflow_rigidbody_mesh_with_particle_color(source_mesh, color):
    """Create an independent RB mesh carrying its own per-vertex particle color."""
    mesh = source_mesh.copy()
    mesh.name = f"{source_mesh.name} RB Colored"
    if color is None:
        return mesh

    attribute = mesh.attributes.get("bflow_color")
    if attribute is None or attribute.domain != 'POINT' or attribute.data_type != 'FLOAT_COLOR':
        if attribute is not None:
            try:
                mesh.attributes.remove(attribute)
            except Exception:
                pass
        attribute = mesh.attributes.new(
            name="bflow_color", type='FLOAT_COLOR', domain='POINT'
        )
    for entry in attribute.data:
        entry.color = color
    return mesh


def _bflow_rigidbody_material(material, build_id, material_cache):
    """Make one RB-safe material copy that reads bflow_color from mesh geometry."""
    if material is None:
        return None
    key = material.as_pointer()
    if key in material_cache:
        return material_cache[key]

    copied = material.copy()
    copied.name = f"{material.name} B-Flow RB"
    copied["bflow_rb_material_owner"] = build_id
    copied.use_nodes = True
    if copied.node_tree:
        for node in copied.node_tree.nodes:
            if (
                node.bl_idname == "ShaderNodeAttribute"
                and node.attribute_name == "bflow_color"
                and node.attribute_type == 'INSTANCER'
            ):
                node.attribute_type = 'GEOMETRY'
    material_cache[key] = copied
    return copied


def _bflow_motion_between_matrices(matrix_from, matrix_to, fps):
    """Compute world-space linear and angular velocity from two transforms."""
    location_from, rotation_from, _scale_from = matrix_from.decompose()
    location_to, rotation_to, _scale_to = matrix_to.decompose()

    frame_rate = max(float(fps), 0.0001)
    linear_velocity = (location_to - location_from) * frame_rate

    # q_to * inverse(q_from) is a world-space orientation delta.
    delta_rotation = rotation_to @ rotation_from.inverted()
    delta_rotation.normalize()

    # Pick the shortest quaternion arc to avoid a spurious >180 degree turn.
    if delta_rotation.w < 0.0:
        delta_rotation = Quaternion((
            -delta_rotation.w,
            -delta_rotation.x,
            -delta_rotation.y,
            -delta_rotation.z,
        ))

    axis, angle = delta_rotation.to_axis_angle()
    angular_velocity = axis * (float(angle) * frame_rate)
    return linear_velocity, angular_velocity


def _bflow_collect_preview_instances_with_motion(
    context,
    preview,
    transfer_acceleration=True,
):
    """Sample current particle motion and estimate previous-frame acceleration.

    For particles present at N-2, N-1 and N:
      v_recent = (P[N] - P[N-1]) / dt
      v_older  = (P[N-1] - P[N-2]) / dt
      a_prev   = (v_recent - v_older) / dt
      v_at_N   = v_recent + 0.5 * a_prev * dt

    The half-step correction converts the average velocity over the last
    frame interval into an estimate of the instantaneous velocity at frame N.
    Angular velocity/acceleration are estimated from quaternion deltas in the
    same way. New particles without two previous samples fall back to the
    available one-frame velocity estimate.
    """
    scene = context.scene
    original_frame = int(scene.frame_current)
    original_subframe = float(scene.frame_subframe)

    previous2_instances = []
    previous_instances = []
    current_instances = []
    next_instances = []

    try:
        if original_frame > int(scene.frame_start) + 1:
            scene.frame_set(original_frame - 2)
            previous2_instances = _bflow_collect_preview_instances_at_current_frame(
                context, preview, copy_mesh_data=False
            )

        if original_frame > int(scene.frame_start):
            scene.frame_set(original_frame - 1)
            previous_instances = _bflow_collect_preview_instances_at_current_frame(
                context, preview, copy_mesh_data=False
            )

        scene.frame_set(original_frame)
        current_instances = _bflow_collect_preview_instances_at_current_frame(
            context, preview, copy_mesh_data=True
        )

        # Used only for particles born on the current frame that have no
        # previous transform from which an initial velocity can be estimated.
        if original_frame < int(scene.frame_end):
            scene.frame_set(original_frame + 1)
            next_instances = _bflow_collect_preview_instances_at_current_frame(
                context, preview, copy_mesh_data=False
            )
    finally:
        scene.frame_set(original_frame, subframe=original_subframe)
        context.view_layer.update()

    previous2_by_key = {entry[3]: entry[0] for entry in previous2_instances}
    previous_by_key = {entry[3]: entry[0] for entry in previous_instances}
    next_by_key = {entry[3]: entry[0] for entry in next_instances}

    fps = (
        float(scene.render.fps)
        / max(float(scene.render.fps_base), 0.0001)
    )
    fps = max(fps, 0.0001)
    dt = 1.0 / fps

    result = []
    sampled_previous = 0
    sampled_next = 0
    acceleration_samples = 0
    unmatched = 0

    for matrix, mesh_key, source_mesh, instance_key in current_instances:
        linear_velocity = Vector((0.0, 0.0, 0.0))
        angular_velocity = Vector((0.0, 0.0, 0.0))
        linear_acceleration = Vector((0.0, 0.0, 0.0))
        angular_acceleration = Vector((0.0, 0.0, 0.0))
        sample_direction = "NONE"

        previous_matrix = previous_by_key.get(instance_key)
        previous2_matrix = previous2_by_key.get(instance_key)
        next_matrix = next_by_key.get(instance_key)

        if previous2_matrix is not None and previous_matrix is not None:
            # Two measured velocity intervals allow acceleration estimation.
            recent_v, recent_w = _bflow_motion_between_matrices(
                previous_matrix, matrix, fps
            )
            older_v, older_w = _bflow_motion_between_matrices(
                previous2_matrix, previous_matrix, fps
            )

            if transfer_acceleration:
                linear_acceleration = (recent_v - older_v) * fps
                angular_acceleration = (recent_w - older_w) * fps
                linear_velocity = recent_v + linear_acceleration * (0.5 * dt)
                angular_velocity = recent_w + angular_acceleration * (0.5 * dt)
                sample_direction = "PREVIOUS_FRAME_ACCELERATION"
                acceleration_samples += 1
            else:
                linear_velocity = recent_v
                angular_velocity = recent_w
                sample_direction = "PREVIOUS_FRAME"
            sampled_previous += 1

        elif previous_matrix is not None:
            # Newborn or recently-emitted particles may not have two history
            # frames. Preserve the available measured velocity.
            linear_velocity, angular_velocity = _bflow_motion_between_matrices(
                previous_matrix, matrix, fps
            )
            sample_direction = "PREVIOUS_FRAME"
            sampled_previous += 1

        elif next_matrix is not None:
            # A particle first appearing on the current frame has no backward
            # interval. Use its first forward step as its initial velocity.
            linear_velocity, angular_velocity = _bflow_motion_between_matrices(
                matrix, next_matrix, fps
            )
            sample_direction = "NEXT_FRAME_NEWBORN"
            sampled_next += 1

        else:
            unmatched += 1

        result.append((
            matrix,
            mesh_key,
            source_mesh,
            instance_key,
            linear_velocity,
            angular_velocity,
            linear_acceleration,
            angular_acceleration,
            sample_direction,
        ))

    return result, {
        "previous": sampled_previous,
        "next": sampled_next,
        "acceleration_samples": acceleration_samples,
        "unmatched": unmatched,
        "fps": fps,
    }


def _bflow_get_object_action_fcurves(obj):
    """Get an object's F-curves using the Blender 5.x slotted Action API."""
    animation_data = obj.animation_data
    if animation_data is None or animation_data.action is None:
        raise RuntimeError(f"{obj.name} has no animation action for rigid-body handoff.")

    action = animation_data.action

    if bpy.app.version >= (5, 0, 0):
        # Blender 5.0 removed Action.fcurves. F-curves now live in the
        # Channelbag associated with the Action Slot.
        try:
            from bpy_extras.anim_utils import animdata_get_channelbag_for_assigned_slot
            channelbag = animdata_get_channelbag_for_assigned_slot(animation_data)
        except Exception:
            channelbag = None

        if channelbag is None:
            slot = getattr(animation_data, "action_slot", None)
            if slot is not None:
                for layer in action.layers:
                    for strip in layer.strips:
                        try:
                            channelbag = strip.channelbag(slot, ensure=False)
                        except Exception:
                            channelbag = None
                        if channelbag is not None:
                            break
                    if channelbag is not None:
                        break

        if channelbag is None:
            raise RuntimeError(
                f"Could not access Blender 5.x Action Channelbag for {obj.name}."
            )
        return channelbag.fcurves

    # Blender 4.4/4.5 still supports the legacy API.
    return action.fcurves


def _bflow_set_keyframe_interpolation(obj, data_path, interpolation):
    """Set F-curve interpolation and fail loudly if the curve is missing."""
    fcurves = _bflow_get_object_action_fcurves(obj)
    matching_curves = [
        fcurve for fcurve in fcurves
        if fcurve.data_path == data_path
    ]

    if not matching_curves:
        raise RuntimeError(
            f"Could not find keyframe curve '{data_path}' on {obj.name}; "
            "rigid-body velocity handoff would be unreliable."
        )

    for fcurve in matching_curves:
        for keyframe in fcurve.keyframe_points:
            keyframe.interpolation = interpolation
        fcurve.update()

    return len(matching_curves)


def _bflow_rotation_from_angular_motion(rotation, angular_velocity, angular_acceleration, elapsed):
    """Integrate a short world-space angular motion segment as an axis-angle delta."""
    angular = Vector(angular_velocity)
    angular_accel = Vector(angular_acceleration)
    rotation_vector = angular * elapsed + angular_accel * (0.5 * elapsed * elapsed)

    angle = rotation_vector.length
    if angle <= 1.0e-8:
        return rotation.copy()

    delta = Quaternion(rotation_vector.normalized(), angle)
    return delta @ rotation


def _bflow_seed_rigidbody_motion(
    obj,
    frame,
    fps,
    linear_velocity,
    angular_velocity,
    linear_acceleration=(0.0, 0.0, 0.0),
    angular_acceleration=(0.0, 0.0, 0.0),
    transfer_acceleration=True,
):
    """Transfer B-Flow motion to Bullet with a pre-rolled kinematic handoff.

    The Rigid Body World begins one frame before the first moving
    kinematic transform. A stationary pre-roll key is inserted at N-1,
    then movement is keyed from N through N+2. Kinematic is turned off
    at N+2 so Bullet inherits velocity from a longer two-segment motion lead-in.

    Blender 5.x slotted Action F-curves are explicitly set to Linear for
    transforms and Constant for the kinematic switch. If the curves cannot be
    found, this function fails instead of silently leaving Bezier easing.
    """
    rb = getattr(obj, "rigid_body", None)
    if rb is None:
        return False

    start_frame = int(frame)
    frame_rate = max(float(fps), 0.0001)
    dt = 1.0 / frame_rate

    position = obj.location.copy()
    obj.rotation_mode = 'QUATERNION'
    rotation = obj.rotation_quaternion.copy()
    scale = obj.scale.copy()

    linear = Vector(linear_velocity)
    angular = Vector(angular_velocity)
    linear_accel = Vector(linear_acceleration) if transfer_acceleration else Vector((0.0, 0.0, 0.0))
    angular_accel = Vector(angular_acceleration) if transfer_acceleration else Vector((0.0, 0.0, 0.0))

    # Frame N-1: pre-roll. This gives Bullet one evaluation frame before the
    # Animated object starts moving. It avoids the known initial-velocity loss
    # when the Rigid Body cache starts on the same frame as Animated motion.
    pre_roll_frame = start_frame - 1
    rb.kinematic = True
    obj.location = position
    obj.rotation_quaternion = rotation
    obj.scale = scale
    obj.keyframe_insert(data_path="location", frame=pre_roll_frame)
    obj.keyframe_insert(data_path="rotation_quaternion", frame=pre_roll_frame)
    obj.keyframe_insert(data_path="scale", frame=pre_roll_frame)
    obj.keyframe_insert(data_path="rigid_body.kinematic", frame=pre_roll_frame)

    # Frame N: exact B-Flow snapshot; still kinematic.
    obj.location = position
    obj.rotation_quaternion = rotation
    obj.scale = scale
    obj.keyframe_insert(data_path="location", frame=start_frame)
    obj.keyframe_insert(data_path="rotation_quaternion", frame=start_frame)
    obj.keyframe_insert(data_path="scale", frame=start_frame)
    obj.keyframe_insert(data_path="rigid_body.kinematic", frame=start_frame)

    # Frame N+1: first velocity segment; remain kinematic.
    t1 = dt
    obj.location = position + linear * t1 + linear_accel * (0.5 * t1 * t1)
    obj.rotation_quaternion = _bflow_rotation_from_angular_motion(
        rotation, angular, angular_accel, t1
    )
    obj.keyframe_insert(data_path="location", frame=start_frame + 1)
    obj.keyframe_insert(data_path="rotation_quaternion", frame=start_frame + 1)
    obj.keyframe_insert(data_path="scale", frame=start_frame + 1)
    rb.kinematic = True
    obj.keyframe_insert(data_path="rigid_body.kinematic", frame=start_frame + 1)

    # Frame N+2: second velocity segment, then hand control to Bullet.
    # Linear transform interpolation preserves a non-zero final velocity.
    t2 = 2.0 * dt
    obj.location = position + linear * t2 + linear_accel * (0.5 * t2 * t2)
    obj.rotation_quaternion = _bflow_rotation_from_angular_motion(
        rotation, angular, angular_accel, t2
    )
    obj.keyframe_insert(data_path="location", frame=start_frame + 2)
    obj.keyframe_insert(data_path="rotation_quaternion", frame=start_frame + 2)
    obj.keyframe_insert(data_path="scale", frame=start_frame + 2)
    rb.kinematic = False
    obj.keyframe_insert(data_path="rigid_body.kinematic", frame=start_frame + 2)

    # Use the correct Blender 5.x Action Channelbag F-curves. Do not suppress
    # errors: Bezier easing on the last segment would make the inherited
    # rigid-body velocity approach zero at the handoff frame.
    transform_curve_count = 0
    for path in ("location", "rotation_quaternion", "scale"):
        transform_curve_count += _bflow_set_keyframe_interpolation(
            obj, path, 'LINEAR'
        )
    kinematic_curve_count = _bflow_set_keyframe_interpolation(
        obj, "rigid_body.kinematic", 'CONSTANT'
    )

    if transform_curve_count < 10 or kinematic_curve_count < 1:
        raise RuntimeError(
            f"Incomplete handoff curves on {obj.name}: "
            f"transform={transform_curve_count}, kinematic={kinematic_curve_count}."
        )

    # Diagnostics for each generated rigid-body object.
    obj["bflow_rb_initial_linear_velocity"] = list(linear)
    obj["bflow_rb_initial_angular_velocity"] = list(angular)
    obj["bflow_rb_initial_linear_acceleration"] = list(linear_accel)
    obj["bflow_rb_initial_angular_acceleration"] = list(angular_accel)
    obj["bflow_rb_motion_handoff_frame"] = start_frame + 2
    obj["bflow_rb_motion_preroll_frame"] = pre_roll_frame
    obj["bflow_rb_motion_fcurve_count"] = transform_curve_count + kinematic_curve_count

    # Restore the Python-side transform to the snapshot frame N.
    obj.location = position
    obj.rotation_quaternion = rotation
    obj.scale = scale
    rb.kinematic = True
    obj.rotation_mode = 'QUATERNION'
    return True


def _bflow_remove_rigidbody_build(context, scene):
    """Remove only native rigid bodies generated by the current B-Flow build."""
    build_id = scene.get("bflow_rb_build_id", "")
    if not build_id:
        return 0

    # Remove B-Flow-generated active particles and the generated ground plane.
    generated_objects = [
        obj for obj in bpy.data.objects
        if obj.get("bflow_rb_owner", "") == build_id
    ]
    removed = 0
    meshes_to_check = set()
    for obj in generated_objects:
        if obj.type == 'MESH' and obj.data:
            meshes_to_check.add(obj.data)
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1

    # Remove Passive rigid-body settings that B-Flow added to user collider objects.
    generated_passives = [
        obj for obj in bpy.data.objects
        if obj.get("bflow_rb_passive_owner", "") == build_id
        and obj.rigid_body is not None
    ]
    if generated_passives:
        try:
            _bflow_remove_rigidbody_batch(context, generated_passives)
        except Exception as exc:
            print("[B-Flow] Could not remove generated passive rigid bodies:", exc)

    for obj in bpy.data.objects:
        if obj.get("bflow_rb_passive_owner", "") == build_id:
            try:
                del obj["bflow_rb_passive_owner"]
            except Exception:
                pass

    # Unlink only collider objects that were not in the Rigid Body World
    # collection before this B-Flow build. Preserve all pre-existing links.
    world = scene.rigidbody_world
    world_collection = world.collection if world else None
    for obj in bpy.data.objects:
        if obj.get("bflow_rb_world_link_owner", "") != build_id:
            continue
        if world_collection is not None and obj.name in world_collection.objects:
            try:
                world_collection.objects.unlink(obj)
            except Exception as exc:
                print("[B-Flow] Could not unlink generated collider membership:", exc)
        try:
            del obj["bflow_rb_world_link_owner"]
        except Exception:
            pass

    # Delete copied mesh datablocks only after all generated objects are removed.
    for mesh in meshes_to_check:
        try:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        except Exception:
            pass

    # Remove unused B-Flow Rigid Body material copies after generated objects.
    for material in list(bpy.data.materials):
        if material.get("bflow_rb_material_owner", "") == build_id and material.users == 0:
            try:
                bpy.data.materials.remove(material)
            except Exception:
                pass

    # Remove B-Flow-created Rigid Body collections only when they are empty.
    # Existing user collections are never removed.
    for collection in list(bpy.data.collections):
        if (
            collection.get("bflow_rb_collection_role") in {"collection", "constraints"}
            and collection.get("bflow_rb_collection_scene") == scene.name
            and len(collection.objects) == 0
            and len(collection.children) == 0
        ):
            try:
                for parent in list(bpy.data.collections):
                    if any(child == collection for child in parent.children):
                        parent.children.unlink(collection)
                bpy.data.collections.remove(collection)
            except Exception as exc:
                print("[B-Flow] Could not remove empty generated Rigid Body collection:", exc)

    # Restore the original B-Flow preview visibility.
    preview = bpy.data.objects.get("B-Flow Preview")
    if preview and preview.get("bflow_rb_build_id", "") == build_id:
        try:
            preview.hide_render = bool(preview.get("bflow_rb_prev_hide_render", False))
            preview.hide_set(bool(preview.get("bflow_rb_prev_hide_viewport", False)))
            for key in (
                "bflow_rb_build_id",
                "bflow_rb_prev_hide_render",
                "bflow_rb_prev_hide_viewport",
            ):
                if key in preview:
                    del preview[key]
        except Exception:
            pass

    # Restore the scene gravity and Rigid Body cache range saved before build.
    if scene.get("bflow_rb_saved_gravity", False):
        try:
            scene.use_gravity = bool(scene.get("bflow_rb_prev_use_gravity", True))
            old_gravity = scene.get("bflow_rb_prev_gravity", (0.0, 0.0, -9.81))
            scene.gravity = old_gravity
        except Exception:
            pass

    if scene.get("bflow_rb_saved_cache", False) and scene.rigidbody_world:
        try:
            cache = scene.rigidbody_world.point_cache
            cache.frame_start = int(scene.get("bflow_rb_prev_cache_start", scene.frame_start))
            cache.frame_end = int(scene.get("bflow_rb_prev_cache_end", scene.frame_end))
        except Exception:
            pass

    for key in (
        "bflow_rb_build_id",
        "bflow_rb_saved_gravity",
        "bflow_rb_prev_use_gravity",
        "bflow_rb_prev_gravity",
        "bflow_rb_saved_cache",
        "bflow_rb_prev_cache_start",
        "bflow_rb_prev_cache_end",
        "bflow_rb_count",
        "bflow_rb_frame",
        "bflow_rb_motion_transferred",
        "bflow_rb_motion_sampled_previous",
        "bflow_rb_motion_sampled_next",
        "bflow_rb_acceleration_samples",
        "bflow_rb_acceleration_transferred",
        "bflow_rb_motion_unmatched",
        "bflow_rb_motion_nonzero",
        "bflow_rb_motion_max_speed",
        "bflow_rb_motion_mean_speed",
        "bflow_rb_preroll_frame",
    ):
        if key in scene:
            try:
                del scene[key]
            except Exception:
                pass

    return removed


class BFLOW_OT_build_rigid_bodies(Operator):
    bl_idname = "bflow.build_rigid_bodies"
    bl_label = "Build Native Rigid Bodies"
    bl_description = "Convert the currently visible B-Flow particle instances into native Blender rigid-body objects"
    bl_options = {'REGISTER', 'UNDO'}

    tree_name: StringProperty()
    node_name: StringProperty()
    index: IntProperty(default=0)

    def execute(self, context):
        scene = context.scene
        tree, node = get_event(self.tree_name, self.node_name)
        if not node or self.index < 0 or self.index >= len(node.operator_items):
            self.report({'ERROR'}, "Select a valid Rigid Body operator first")
            return {'CANCELLED'}

        item = node.operator_items[self.index]
        if item.operator_type != "RIGID_BODY" or not item.enabled:
            self.report({'ERROR'}, "Enable the Rigid Body operator first")
            return {'CANCELLED'}

        if context.mode != 'OBJECT':
            self.report({'ERROR'}, "Switch to Object Mode before building native Rigid Bodies")
            return {'CANCELLED'}

        preview = bpy.data.objects.get("B-Flow Preview")
        if preview is None or not any(
            mod.type == 'NODES' and mod.node_group is not None
            for mod in preview.modifiers
        ):
            self.report({'ERROR'}, "Compile B-Flow first so B-Flow Preview contains particles")
            return {'CANCELLED'}

        # Do not mutate an existing baked Rigid Body cache. Blender protects
        # baked physics until the user explicitly deletes the bake.
        if (
            scene.rigidbody_world is not None
            and scene.rigidbody_world.point_cache
            and getattr(scene.rigidbody_world.point_cache, "is_baked", False)
        ):
            self.report({'ERROR'}, "Delete the Rigid Body World bake before rebuilding")
            return {'CANCELLED'}

        # Restore the Geometry Nodes preview if a previous native-RB snapshot
        # exists, then evaluate the current frame again before collecting instances.
        _bflow_remove_rigidbody_build(context, scene)
        context.view_layer.update()

        # Sample current transforms plus an adjacent frame to recover each
        # instance's actual world-space linear and angular velocity. This
        # captures the combined result of Speed, Gravity, collisions, Keep Dist,
        # Rotation and Spin instead of only copying operator parameter values.
        if item.rb_transfer_motion:
            instances, motion_stats = _bflow_collect_preview_instances_with_motion(
                context,
                preview,
                transfer_acceleration=item.rb_transfer_acceleration,
            )
        else:
            raw_instances = _bflow_collect_preview_instances_at_current_frame(
                context, preview, copy_mesh_data=True
            )
            instances = [
                (
                    matrix, mesh_key, source_mesh, instance_key,
                    Vector((0.0, 0.0, 0.0)),
                    Vector((0.0, 0.0, 0.0)),
                    Vector((0.0, 0.0, 0.0)),
                    Vector((0.0, 0.0, 0.0)),
                    "NONE",
                )
                for matrix, mesh_key, source_mesh, instance_key in raw_instances
            ]
            motion_stats = {
                "previous": 0,
                "next": 0,
                "acceleration_samples": 0,
                "unmatched": len(instances),
                "fps": (
                    float(scene.render.fps)
                    / max(float(scene.render.fps_base), 0.0001)
                ),
            }
        if not instances:
            self.report(
                {'ERROR'},
                "No visible particle instances. Compile and move to a frame with particles first."
            )
            return {'CANCELLED'}

        # Capture Rand Color before changing the preview/instance representation.
        # The temporary Realize Instances pass is removed immediately afterward.
        instance_colors = _bflow_capture_instance_colors(context, preview, instances)
        captured_color_count = sum(1 for color in instance_colors if color is not None)

        try:
            world = _bflow_ensure_rigidbody_world(context)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        if world.point_cache and getattr(world.point_cache, "is_baked", False):
            self.report({'ERROR'}, "Delete the existing Rigid Body World bake before rebuilding")
            return {'CANCELLED'}

        if not scene.get("bflow_rb_saved_cache", False):
            scene["bflow_rb_saved_cache"] = True
            scene["bflow_rb_prev_cache_start"] = int(world.point_cache.frame_start)
            scene["bflow_rb_prev_cache_end"] = int(world.point_cache.frame_end)
        # Blender's Animated -> Dynamic handoff can lose inherited velocity
        # when the Rigid Body cache starts on the same frame as the first
        # animated motion key. Start the cache one frame earlier (N-1);
        # Bullet now takes over at N+2 after two keyed velocity segments.
        snapshot_frame = int(scene.frame_current)
        motion_preroll_frame = max(int(scene.frame_start), snapshot_frame - 1)
        try:
            world.point_cache.frame_start = (
                motion_preroll_frame if item.rb_transfer_motion else snapshot_frame
            )
            world.point_cache.frame_end = int(scene.frame_end)
        except Exception as exc:
            print("[B-Flow] Could not set rigid-body cache frame range:", exc)

        try:
            collection = _bflow_ensure_rigidbody_collection(
                scene,
                world,
                "collection",
                "B-Flow Rigid Body Objects",
            )
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        build_id = uuid.uuid4().hex
        scene["bflow_rb_build_id"] = build_id

        # Save and apply B-Flow Gravity to the scene's native Rigid Body World.
        if not scene.get("bflow_rb_saved_gravity", False):
            scene["bflow_rb_saved_gravity"] = True
            scene["bflow_rb_prev_use_gravity"] = bool(scene.use_gravity)
            scene["bflow_rb_prev_gravity"] = list(scene.gravity)

        gravity_vector = None
        ground_settings = None
        object_colliders = {}
        for event_node in tree.nodes:
            if event_node.bl_idname != "BFLOW_EventNode":
                continue
            for op in event_node.operator_items:
                if not op.enabled:
                    continue
                if op.operator_type == "GRAVITY" and gravity_vector is None:
                    gravity_vector = tuple(op.gravity)
                elif op.operator_type == "COLLISION" and ground_settings is None:
                    ground_settings = (op.collision_height, op.bounce, op.friction)
                elif op.operator_type == "OBJECT_COLLISION" and op.collider is not None:
                    # One collider may be referenced by multiple Event operators.
                    # Keep one native rigid-body entry per actual object datablock.
                    object_colliders.setdefault(
                        op.collider.as_pointer(),
                        (op.collider, op.object_bounce, op.object_friction)
                    )

        if gravity_vector is None:
            scene.use_gravity = False
        else:
            scene.use_gravity = True
            scene.gravity = gravity_vector

        # Each RB object receives its own mesh copy so its per-instance color
        # can be stored as a POINT-domain attribute without bleeding to siblings.
        generated = []
        generated_motion = []
        rb_material_cache = {}
        for instance_index, (
            matrix, mesh_key, source_mesh, instance_key,
            linear_velocity, angular_velocity,
            linear_acceleration, angular_acceleration, sample_direction
        ) in enumerate(instances, 1):
            if source_mesh is None:
                raise RuntimeError(
                    "Particle mesh snapshot is missing. Rebuild from the current frame."
                )

            particle_color = (
                instance_colors[instance_index - 1]
                if instance_index - 1 < len(instance_colors) else None
            )
            copied_mesh = _bflow_rigidbody_mesh_with_particle_color(
                source_mesh, particle_color
            )
            copied_mesh.name = f"B-Flow RB Mesh {instance_index:04d}"
            copied_mesh["bflow_rb_owner"] = build_id

            # Use RB-specific copies of the source materials. Their shader
            # graph reads bflow_color from the mesh rather than the lost
            # Geometry Nodes instancer context; original materials stay intact.
            for material_slot_index, material in enumerate(list(copied_mesh.materials)):
                if material is not None:
                    copied_mesh.materials[material_slot_index] = _bflow_rigidbody_material(
                        material, build_id, rb_material_cache
                    )

            rigid_obj = bpy.data.objects.new(
                f"B-Flow RB {instance_index:04d}",
                copied_mesh,
            )
            collection.objects.link(rigid_obj)
            rigid_obj.matrix_world = matrix
            rigid_obj["bflow_rb_owner"] = build_id
            rigid_obj["bflow_rb_index"] = instance_index
            rigid_obj["bflow_rb_instance_key"] = str(instance_key)
            generated.append(rigid_obj)
            generated_motion.append((
                linear_velocity,
                angular_velocity,
                linear_acceleration,
                angular_acceleration,
                sample_direction,
            ))

        try:
            _bflow_add_rigidbody_batch(context, generated, "ACTIVE")
        except Exception as exc:
            _bflow_remove_rigidbody_build(context, scene)
            self.report({'ERROR'}, f"Could not create Active Rigid Bodies: {exc}")
            return {'CANCELLED'}

        for rigid_obj in generated:
            rb = rigid_obj.rigid_body
            if rb is None:
                continue
            rb.mass = item.rb_mass
            rb.friction = item.rb_friction
            rb.restitution = item.rb_restitution
            rb.collision_shape = item.rb_collision_shape
            rb.use_margin = True
            rb.collision_margin = item.rb_collision_margin
            rb.linear_damping = item.rb_linear_damping
            rb.angular_damping = item.rb_angular_damping
            rb.use_deactivation = item.rb_use_deactivation
            rigid_obj["bflow_rb_particle_friction"] = float(item.rb_friction)
            rigid_obj["bflow_rb_angular_damping"] = float(item.rb_angular_damping)
            rigid_obj["bflow_rb_use_deactivation"] = bool(item.rb_use_deactivation)
            if hasattr(rb, "deactivate_linear_velocity"):
                rb.deactivate_linear_velocity = item.rb_deactivate_linear_velocity
            if hasattr(rb, "deactivate_angular_velocity"):
                rb.deactivate_angular_velocity = item.rb_deactivate_angular_velocity
            rigid_obj["bflow_rb_deactivate_linear_velocity"] = float(item.rb_deactivate_linear_velocity)
            rigid_obj["bflow_rb_deactivate_angular_velocity"] = float(item.rb_deactivate_angular_velocity)
            if hasattr(rb, "use_start_deactivated"):
                # The particles must begin active so transferred motion is not
                # discarded at the handoff frame.
                rb.use_start_deactivated = False

        motion_transferred = 0
        if item.rb_transfer_motion:
            for rigid_obj, motion_data in zip(generated, generated_motion):
                (
                    linear_velocity,
                    angular_velocity,
                    linear_acceleration,
                    angular_acceleration,
                    sample_direction,
                ) = motion_data
                try:
                    if _bflow_seed_rigidbody_motion(
                        rigid_obj,
                        scene.frame_current,
                        motion_stats["fps"],
                        linear_velocity,
                        angular_velocity,
                        linear_acceleration,
                        angular_acceleration,
                        item.rb_transfer_acceleration,
                    ):
                        rigid_obj["bflow_rb_motion_sample"] = sample_direction
                        motion_transferred += 1
                except Exception as exc:
                    print(
                        f"[B-Flow] Could not transfer motion to {rigid_obj.name}: {exc}"
                    )

            # Re-evaluate the starting frame so generated RB objects are posed
            # at the original B-Flow snapshot before playback begins.
            scene.frame_set(scene.frame_current)
            context.view_layer.update()

        # Every enabled Object Collision target must participate in this
        # Rigid Body World. Save its original collection membership before
        # Blender's rigidbody.objects_add operator potentially auto-links it.
        object_collider_collection_membership = {
            collider.as_pointer(): (collider.name in collection.objects)
            for collider, _, _ in object_colliders.values()
        }

        # Add B-Flow Object Collision objects as Passive rigid bodies if needed.
        passive_to_add = []
        passive_settings = {}
        for collider, bounce, friction in object_colliders.values():
            if collider.rigid_body is None:
                passive_to_add.append(collider)
                passive_settings[collider.name] = (bounce, friction)
            else:
                # Existing user rigid-body types and material values are kept.
                # Existing Active bodies are still collision-capable, but remain
                # dynamic rather than being silently converted to Passive.
                if collider.rigid_body.type == 'ACTIVE':
                    self.report(
                        {'WARNING'},
                        f"{collider.name} already has an Active Rigid Body; kept Active"
                    )

        # Create a passive ground plane for B-Flow Ground Collision.
        ground_plane = None
        if ground_settings is not None:
            ground_height, ground_bounce, ground_friction = ground_settings
            plane_mesh = bpy.data.meshes.new("B-Flow RB Ground Mesh")
            plane_mesh.from_pydata(
                [(-1000.0, -1000.0, ground_height),
                 (1000.0, -1000.0, ground_height),
                 (1000.0, 1000.0, ground_height),
                 (-1000.0, 1000.0, ground_height)],
                [],
                [(0, 1, 2, 3)],
            )
            plane_mesh.update()
            ground_plane = bpy.data.objects.new("B-Flow RB Ground", plane_mesh)
            collection.objects.link(ground_plane)
            ground_plane["bflow_rb_owner"] = build_id
            ground_plane["bflow_rb_ground"] = True
            passive_to_add.append(ground_plane)
            passive_settings[ground_plane.name] = (ground_bounce, ground_friction)

        # Mark user collider objects before adding Passive settings so Clear can
        # remove them even if Blender's batch operator only partially succeeds.
        for collider in passive_to_add:
            if collider != ground_plane:
                collider["bflow_rb_passive_owner"] = build_id

        try:
            if passive_to_add:
                _bflow_add_rigidbody_batch(context, passive_to_add, "PASSIVE")
        except Exception as exc:
            _bflow_remove_rigidbody_build(context, scene)
            self.report({'ERROR'}, f"Could not configure Passive collision objects: {exc}")
            return {'CANCELLED'}

        missing_passives = [obj.name for obj in passive_to_add if obj.rigid_body is None]
        if missing_passives:
            _bflow_remove_rigidbody_build(context, scene)
            self.report({'ERROR'}, "Rigid Body settings missing on: " + ", ".join(missing_passives))
            return {'CANCELLED'}

        for passive_obj in passive_to_add:
            bounce, friction = passive_settings.get(passive_obj.name, (0.0, 0.5))
            passive_obj.rigid_body.restitution = bounce
            passive_obj.rigid_body.friction = friction
            if passive_obj.type == 'MESH':
                if passive_obj.get("bflow_rb_ground", False):
                    # The generated ground is a large, flat static surface;
                    # triangle-mesh collision is appropriate here.
                    passive_obj.rigid_body.collision_shape = 'MESH'
                    passive_obj.rigid_body.use_margin = False
                else:
                    # User Object Collision targets use a convex hull by
                    # default. Concave triangle meshes can be unreliable for
                    # thin/open round meshes (e.g. circles/rings), while the
                    # convex hull gives Bullet a robust closed collision
                    # volume for ordinary round and polygonal mesh objects.
                    passive_obj.rigid_body.collision_shape = 'CONVEX_HULL'
                    passive_obj.rigid_body.use_margin = True
                    passive_obj.rigid_body.collision_margin = min(
                        max(float(item.rb_collision_margin), 0.001), 0.02
                    )

        # Existing rigid-body colliders also need the same collision-shape
        # configuration; previously only newly-created Passive bodies were
        # assigned a shape, leaving pre-existing colliders on Blender defaults.
        for collider, _bounce, _friction in object_colliders.values():
            if collider.type == 'MESH' and collider.rigid_body is not None:
                collider.rigid_body.collision_shape = 'CONVEX_HULL'
                collider.rigid_body.use_margin = True
                collider.rigid_body.collision_margin = min(
                    max(float(item.rb_collision_margin), 0.001), 0.02
                )

        # Explicitly include every enabled Object Collision target in the
        # collection assigned to Rigid Body World. Blender's manual states
        # objects outside this collection do not participate in the simulation.
        linked_collider_count = 0
        for collider, _bounce, _friction in object_colliders.values():
            was_linked_before = object_collider_collection_membership.get(
                collider.as_pointer(), False
            )
            if collider.name not in collection.objects:
                collection.objects.link(collider)
            if not was_linked_before:
                collider["bflow_rb_world_link_owner"] = build_id
                linked_collider_count += 1

            if collider.rigid_body is None:
                # Do not silently continue with an object that cannot collide.
                _bflow_remove_rigidbody_build(context, scene)
                self.report(
                    {'ERROR'},
                    f"Object Collision target '{collider.name}' has no Rigid Body"
                )
                return {'CANCELLED'}

        # Hide the Geometry Nodes preview to prevent duplicate visuals.
        preview["bflow_rb_build_id"] = build_id
        preview["bflow_rb_prev_hide_render"] = bool(preview.hide_render)
        try:
            preview["bflow_rb_prev_hide_viewport"] = bool(preview.hide_get())
        except Exception:
            preview["bflow_rb_prev_hide_viewport"] = False
        preview.hide_render = True
        preview.hide_set(True)

        scene["bflow_rb_build_id"] = build_id
        scene["bflow_rb_count"] = len(generated)
        scene["bflow_rb_frame"] = int(scene.frame_current)
        scene["bflow_rb_motion_transferred"] = int(motion_transferred)
        scene["bflow_rb_motion_sampled_previous"] = int(motion_stats["previous"])
        scene["bflow_rb_motion_sampled_next"] = int(motion_stats["next"])
        scene["bflow_rb_acceleration_samples"] = int(motion_stats["acceleration_samples"])
        scene["bflow_rb_acceleration_transferred"] = int(
            motion_stats["acceleration_samples"] if item.rb_transfer_acceleration else 0
        )
        scene["bflow_rb_motion_unmatched"] = int(motion_stats["unmatched"])

        sampled_speeds = [
            float(motion_data[0].length)
            for motion_data in generated_motion
        ]
        moving_particle_count = sum(
            1 for speed in sampled_speeds if speed > 1.0e-4
        )
        max_sampled_speed = max(sampled_speeds, default=0.0)
        mean_sampled_speed = (
            sum(sampled_speeds) / len(sampled_speeds)
            if sampled_speeds else 0.0
        )
        scene["bflow_rb_motion_nonzero"] = int(moving_particle_count)
        scene["bflow_rb_motion_max_speed"] = float(max_sampled_speed)
        scene["bflow_rb_motion_mean_speed"] = float(mean_sampled_speed)
        scene["bflow_rb_preroll_frame"] = int(motion_preroll_frame)

        # Ensure generated objects are in the Rigid Body World collection.
        for rigid_obj in generated + ([ground_plane] if ground_plane else []):
            if rigid_obj and collection not in rigid_obj.users_collection:
                try:
                    collection.objects.link(rigid_obj)
                except Exception:
                    pass

        # Prime playback from the cache's pre-roll frame. The user should press
        # Play from N-1 so Blender evaluates the kinematic motion segment before
        # turning the particles over to Bullet.
        if item.rb_transfer_motion and motion_preroll_frame < snapshot_frame:
            scene.frame_set(motion_preroll_frame)
            context.view_layer.update()

        self.report(
            {'INFO'},
            f"Built {len(generated)} particles; colors transferred={captured_color_count}/{len(generated)}; "
            f"motion transferred={motion_transferred}; moving={moving_particle_count}; "
            f"max speed={max_sampled_speed:.3f} m/s; "
            f"prev={motion_stats['previous']} next={motion_stats['next']} "
            f"accel={motion_stats['acceleration_samples']} "
            f"no-sample={motion_stats['unmatched']}; play from frame {scene.frame_current}"
        )
        return {'FINISHED'}


class BFLOW_OT_clear_rigid_bodies(Operator):
    bl_idname = "bflow.clear_rigid_bodies"
    bl_label = "Clear B-Flow Rigid Bodies"
    bl_description = "Remove B-Flow-generated rigid body objects and restore the Geometry Nodes preview"

    def execute(self, context):
        world = context.scene.rigidbody_world
        if (
            world is not None
            and world.point_cache
            and getattr(world.point_cache, "is_baked", False)
        ):
            self.report({'ERROR'}, "Delete the Rigid Body World bake before clearing B-Flow bodies")
            return {'CANCELLED'}

        count = _bflow_remove_rigidbody_build(context, context.scene)
        if count == 0:
            self.report({'INFO'}, "No B-Flow native Rigid Body build to clear")
        else:
            self.report({'INFO'}, f"Cleared {count} generated Rigid Body objects")
        return {'FINISHED'}


class BFLOW_OT_compile_simulation(Operator):
    bl_idname = "bflow.compile_simulation"
    bl_label = "Compile / Update Simulation"
    bl_description = "Compile explicit Birth, Speed and Gravity operators into a Geometry Nodes simulation"

    def execute(self, context):
        scene = context.scene
        tree = scene.bflow_graph
        if not tree:
            self.report({'ERROR'}, "Create or select a B-Flow graph first")
            return {'CANCELLED'}

        # Read enabled operators from Event stacks.
        # Birth is explicit: no enabled Birth operator means zero emission.
        birth_count = 0
        birth_start = 1
        birth_stop = 100
        # No Speed operator means particles start with zero velocity.
        speed_value = 0.0
        speed_direction = (0.0, 0.0, 1.0)
        speed_mode = "NORMAL"
        speed_randomness = 0.0
        # No Gravity operator means zero acceleration (no implicit world gravity).
        gravity = (0.0, 0.0, 0.0)
        keep_dist_enabled = False
        keep_distance = 0.25
        keep_dist_strength = 0.5
        emitter_object = None
        # Explicit Position Object override collected from the operator stack.
        # Initialize before scanning operators to avoid UnboundLocalError when
        # no Position Object operator is present or enabled.
        position_object_override = None
        ground_collision_enabled = False
        object_collision_enabled = False
        collision_enabled = False
        collision_height = 0.0
        collision_bounce = 0.65
        collision_bounce_randomness = 0.0
        collision_friction = 0.15
        collision_friction_randomness = 0.0
        particle_radius = 0.035
        collision_object = None
        object_collision_bounce = 0.85
        object_collision_friction = 0.0
        object_random_deflection = 0.25
        object_particle_radius = 0.035
        shape_instance_object = None
        shape_instance_choices = []
        shape_instance_scale = 1.0
        size_enabled = False
        size_mode = "FIXED"
        size_fixed = 1.0
        size_start = 1.0
        size_end = 2.0
        size_start_time = 0.0
        size_end_time = 30.0
        size_time_unit = "FRAMES"
        opacity_enabled = False
        opacity_mode = "PARTICLE_TIME"
        opacity_fixed = 1.0
        opacity_start = 1.0
        opacity_end = 0.0
        opacity_start_frame = 0
        opacity_end_frame = 30
        spin_enabled = False
        spin_axis = (0.0, 0.0, 1.0)
        spin_strength = 0.0
        spin_randomness = 0.5
        rotation_enabled = False
        rotation_offset = (0.0, 0.0, 0.0)
        rotation_randomness = 1.0
        rand_color_enabled = False
        rand_color_palette = []
        selected_force_fields = []
        delete_enabled = False
        delete_mode = "PARTICLE_TIME"
        delete_after = 30.0
        delete_time_unit = "FRAMES"

        events = sorted(
            [n for n in tree.nodes if n.bl_idname == "BFLOW_EventNode"],
            key=lambda n: n.event_id
        )
        found_birth = found_speed = found_gravity = found_delete = found_keep_dist = False
        for event_node in events:
            for item in event_node.operator_items:
                if not item.enabled:
                    continue
                if item.operator_type == "BIRTH" and not found_birth:
                    birth_count = item.count
                    birth_start = item.birth_start
                    birth_stop = item.birth_stop
                    found_birth = True
                elif item.operator_type == "DELETE" and not found_delete:
                    delete_enabled = True
                    delete_mode = item.delete_mode
                    delete_after = item.delete_after
                    delete_time_unit = item.delete_time_unit
                    found_delete = True
                elif item.operator_type == "SPEED" and not found_speed:
                    speed_value = item.speed
                    speed_direction = tuple(item.direction)
                    speed_mode = item.speed_mode
                    speed_randomness = item.speed_randomness
                    found_speed = True
                elif item.operator_type == "GRAVITY" and not found_gravity:
                    gravity = tuple(item.gravity)
                    found_gravity = True
                elif item.operator_type == "FORCE":
                    for force_entry in item.force_fields:
                        field_obj = force_entry.field_object
                        if field_obj is None or field_obj.field is None:
                            continue
                        selected_force_fields.append(
                            (field_obj, float(force_entry.influence))
                        )
                elif item.operator_type == "KEEP_DIST" and not found_keep_dist:
                    keep_dist_enabled = True
                    keep_distance = item.keep_distance
                    keep_dist_strength = item.keep_dist_strength
                    found_keep_dist = True
                elif item.operator_type == "COLLISION":
                    ground_collision_enabled = True
                    collision_enabled = True
                    collision_height = item.collision_height
                    collision_bounce = item.bounce
                    collision_bounce_randomness = item.bounce_randomness
                    collision_friction = item.friction
                    collision_friction_randomness = item.friction_randomness
                    particle_radius = item.particle_radius
                elif item.operator_type == "OBJECT_COLLISION":
                    object_collision_enabled = True
                    if item.collider is not None:
                        collision_object = item.collider
                    object_collision_bounce = item.object_bounce
                    object_collision_friction = item.object_friction
                    object_random_deflection = item.object_random_deflection
                    object_particle_radius = item.object_particle_radius
                elif item.operator_type == "SHAPE_INSTANCE":
                    shape_instance_scale = item.shape_scale
                    configured_choices = [
                        (entry.object, float(entry.percentage))
                        for entry in item.shape_instance_entries
                        if entry.object is not None and float(entry.percentage) > 0.0
                    ]
                    if configured_choices:
                        shape_instance_choices = configured_choices
                        shape_instance_object = configured_choices[0][0]
                    elif item.shape_object is not None:
                        # Backward compatibility for graphs created before weighted shapes.
                        shape_instance_object = item.shape_object
                        shape_instance_choices = [(item.shape_object, 100.0)]
                elif item.operator_type == "SIZE" and not size_enabled:
                    size_enabled = True
                    size_mode = item.size_mode
                    size_fixed = item.size_fixed
                    size_start = item.size_start
                    size_end = item.size_end
                    size_start_time = item.size_start_time
                    size_end_time = max(item.size_end_time, item.size_start_time + 0.001)
                    size_time_unit = item.size_time_unit
                elif item.operator_type == "OPACITY" and not opacity_enabled:
                    opacity_enabled = True
                    opacity_mode = item.opacity_mode
                    opacity_fixed = item.opacity_fixed
                    opacity_start = item.opacity_start
                    opacity_end = item.opacity_end
                    opacity_start_frame = item.opacity_start_frame
                    opacity_end_frame = item.opacity_end_frame
                elif item.operator_type == "SPIN":
                    spin_enabled = True
                    spin_axis = tuple(item.direction)
                    spin_strength = item.strength
                    spin_randomness = item.spin_randomness
                elif item.operator_type == "ROTATION":
                    rotation_enabled = True
                    rotation_offset = tuple(item.rotation_offset)
                    rotation_randomness = item.rotation_randomness
                elif item.operator_type == "RAND_COLOR" and not rand_color_enabled:
                    palette = [
                        (tuple(entry.color), float(entry.percentage))
                        for entry in item.rand_color_entries
                        if float(entry.percentage) > 0.0
                    ]
                    if palette and sum(weight for _color, weight in palette) > 0.0:
                        rand_color_enabled = True
                        rand_color_palette = palette
                elif (
                    item.operator_type in {"POSITION_OBJECT", "POSITION_VOLUME"}
                    and item.source_object is not None
                    and position_object_override is None
                ):
                    # An explicitly assigned Position Object must take priority
                    # over the graph's captured default emitter.
                    position_object_override = item.source_object

        # Explicit Position Object operators override the emitter captured
        # when the graph was created. The captured object is only a fallback.
        # Do NOT use the current active object during compilation: selecting an
        # unrelated mesh must not silently change the particle emitter.
        if position_object_override is not None:
            emitter_object = position_object_override

        if emitter_object is None:
            candidate = getattr(scene, "bflow_default_emitter", None)
            if (
                candidate is not None
                and candidate.name in bpy.data.objects
                and candidate.type == 'MESH'
                and candidate.name != "B-Flow Preview"
            ):
                emitter_object = candidate

        # Create or reuse the preview host.
        obj = bpy.data.objects.get("B-Flow Preview")
        if obj is None:
            mesh = bpy.data.meshes.new("B-Flow Preview Mesh")
            obj = bpy.data.objects.new("B-Flow Preview", mesh)
            context.collection.objects.link(obj)

        group_name = "B-Flow GN Simulation"
        group = bpy.data.node_groups.get(group_name)
        if group is None or group.bl_idname != "GeometryNodeTree":
            group = bpy.data.node_groups.new(group_name, "GeometryNodeTree")
        else:
            group.nodes.clear()

        # Rebuild the GN interface.
        try:
            for interface_item in list(group.interface.items_tree):
                if interface_item.item_type == 'SOCKET':
                    group.interface.remove(interface_item)
        except Exception:
            pass

        group.interface.new_socket(
            name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry'
        )
        group.interface.new_socket(
            name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry'
        )

        nodes, links = group.nodes, group.links

        # Rebuild only B-Flow-managed display assets. Original Shape Instance
        # objects and their original materials are never modified.
        _bflow_remove_generated_opacity_assets(obj.name)
        display_shape_object = shape_instance_object
        display_shape_objects = []
        particle_display_material = None
        if shape_instance_choices:
            for source_shape, _weight in shape_instance_choices:
                display_shape = source_shape
                if opacity_enabled or rand_color_enabled:
                    display_shape = _bflow_make_particle_shape_source(
                        source_shape,
                        obj,
                        scene,
                        opacity_enabled=opacity_enabled,
                        rand_color_enabled=rand_color_enabled,
                    )
                    if display_shape is None:
                        self.report(
                            {'WARNING'},
                            f"Could not create a display copy for {source_shape.name}; using original object."
                        )
                        display_shape = source_shape
                display_shape_objects.append(display_shape)
            display_shape_object = display_shape_objects[0] if display_shape_objects else shape_instance_object
        elif opacity_enabled or rand_color_enabled:
            if shape_instance_object is None:
                particle_display_material = _bflow_make_default_particle_material(
                    obj,
                    opacity_enabled=opacity_enabled,
                    rand_color_enabled=rand_color_enabled,
                )

        # If OPACITY was removed from the Event stack, strip its old material
        # wrapper from every persistent B-Flow material belonging to this preview.
        if not opacity_enabled:
            for managed_material in list(bpy.data.materials):
                if (
                    managed_material.get("bflow_display_owner") == obj.name
                    or managed_material.get("bflow_opacity_owner") == obj.name
                ):
                    _bflow_disable_opacity_on_material(managed_material, obj.name)

        # Birth positions are distributed over an object's surface.
        # If no emitter object is assigned/selected, use a spherical fallback.
        if emitter_object is not None:
            emitter_info = nodes.new("GeometryNodeObjectInfo")
            emitter_info.label = "POSITION OBJECT"
            emitter_info.location = (-1100, 220)
            emitter_info.transform_space = 'RELATIVE'
            emitter_info.inputs["Object"].default_value = emitter_object
            emitter_geometry = emitter_info.outputs["Geometry"]
            try:
                dims = emitter_object.dimensions
                approx_area = max(2.0 * (dims.x*dims.y + dims.y*dims.z + dims.x*dims.z), 0.01)
            except Exception:
                approx_area = 6.0
        else:
            emitter_mesh = nodes.new("GeometryNodeMeshIcoSphere")
            emitter_mesh.label = "DEFAULT SPHERICAL EMITTER"
            emitter_mesh.location = (-1100, 220)
            emitter_mesh.inputs["Radius"].default_value = 1.0
            if " subdivisions" in emitter_mesh.inputs:
                emitter_mesh.inputs[" subdivisions"].default_value = 2
            elif "Subdivisions" in emitter_mesh.inputs:
                emitter_mesh.inputs["Subdivisions"].default_value = 2
            emitter_geometry = emitter_mesh.outputs["Mesh"]
            approx_area = 12.56

        distribute = nodes.new("GeometryNodeDistributePointsOnFaces")
        distribute.label = "B-FLOW / BIRTH — PER FRAME"
        distribute.location = (-850, 220)
        emission_frames = max(1, birth_stop - birth_start + 1)
        per_frame_density = (birth_count / emission_frames) / max(approx_area, 0.0001)
        distribute.inputs["Density"].default_value = per_frame_density
        if "Seed" in distribute.inputs:
            distribute.inputs["Seed"].default_value = 17
        links.new(emitter_geometry, distribute.inputs["Mesh"])

        # Gate emission by the scene frame range. Density is zero outside Start..Stop.
        scene_time = nodes.new("GeometryNodeInputSceneTime")
        scene_time.label = "BIRTH FRAME CLOCK"
        scene_time.location = (-1250, -500)

        frame_after_start = nodes.new("ShaderNodeMath")
        frame_after_start.operation = 'GREATER_THAN'
        frame_after_start.label = "FRAME >= START"
        frame_after_start.location = (-1050, -500)
        frame_after_start.inputs[1].default_value = birth_start - 1
        links.new(scene_time.outputs["Frame"], frame_after_start.inputs[0])

        frame_before_stop = nodes.new("ShaderNodeMath")
        frame_before_stop.operation = 'LESS_THAN'
        frame_before_stop.label = "FRAME <= STOP"
        frame_before_stop.location = (-1050, -650)
        frame_before_stop.inputs[1].default_value = birth_stop + 1
        links.new(scene_time.outputs["Frame"], frame_before_stop.inputs[0])

        birth_window = nodes.new("ShaderNodeMath")
        birth_window.operation = 'MULTIPLY'
        birth_window.label = "BIRTH ACTIVE WINDOW"
        birth_window.location = (-850, -500)
        links.new(frame_after_start.outputs[0], birth_window.inputs[0])
        links.new(frame_before_stop.outputs[0], birth_window.inputs[1])

        density_gate = nodes.new("ShaderNodeMath")
        density_gate.operation = 'MULTIPLY'
        density_gate.label = "EMISSION DENSITY GATE"
        density_gate.location = (-650, -500)
        density_gate.inputs[0].default_value = per_frame_density
        links.new(birth_window.outputs[0], density_gate.inputs[1])
        links.new(density_gate.outputs[0], distribute.inputs["Density"])

        # Change the random seed with the frame so each emission step gets a new sample.
        seed_node = nodes.new("ShaderNodeMath")
        seed_node.operation = 'ADD'
        seed_node.label = "FRAME VARIED SEED"
        seed_node.location = (-850, -700)
        seed_node.inputs[1].default_value = 17
        links.new(scene_time.outputs["Frame"], seed_node.inputs[0])
        if "Seed" in distribute.inputs:
            links.new(seed_node.outputs[0], distribute.inputs["Seed"])

        # Optional per-particle random initial orientation. This is stored on
        # the birth geometry before entering the Simulation Zone, so it does
        # not get regenerated every frame.
        birth_geometry = distribute.outputs["Points"]
        if rotation_enabled:
            random_rotation = nodes.new("FunctionNodeRandomValue")
            random_rotation.data_type = 'FLOAT_VECTOR'
            random_rotation.label = "RANDOM INITIAL ROTATION"
            random_rotation.location = (-650, 520)
            random_rotation.inputs["Min"].default_value = (0.0, 0.0, 0.0)
            angle_limit = 6.283185307179586 * rotation_randomness
            random_rotation.inputs["Max"].default_value = (
                angle_limit, angle_limit, angle_limit
            )

            add_rotation_offset = nodes.new("ShaderNodeVectorMath")
            add_rotation_offset.operation = 'ADD'
            add_rotation_offset.label = "ADD ROTATION XYZ OFFSET"
            add_rotation_offset.location = (-430, 520)
            add_rotation_offset.inputs[1].default_value = rotation_offset
            links.new(random_rotation.outputs["Value"], add_rotation_offset.inputs[0])

            store_initial_rotation = nodes.new("GeometryNodeStoreNamedAttribute")
            store_initial_rotation.label = "STORE INITIAL ROTATION"
            store_initial_rotation.location = (-200, 500)
            store_initial_rotation.domain = 'POINT'
            store_initial_rotation.data_type = 'FLOAT_VECTOR'
            store_initial_rotation.inputs["Name"].default_value = "bflow_rotation"
            links.new(birth_geometry, store_initial_rotation.inputs["Geometry"])
            links.new(add_rotation_offset.outputs["Vector"], store_initial_rotation.inputs["Value"])
            birth_geometry = store_initial_rotation.outputs["Geometry"]

        # Pick a weighted palette color once at particle birth and store it
        # on the point. The shader reads it through the Instancer attribute,
        # so the selected color stays stable throughout the particle lifetime.
        if rand_color_enabled and rand_color_palette:
            total_weight = sum(weight for _color, weight in rand_color_palette)
            normalized_palette = [
                (color, weight / total_weight)
                for color, weight in rand_color_palette
            ]

            color_random = nodes.new("FunctionNodeRandomValue")
            color_random.data_type = 'FLOAT'
            color_random.label = "WEIGHTED RANDOM COLOR PICK"
            color_random.location = (-650, 900)
            color_random.inputs["Min"].default_value = 0.0
            color_random.inputs["Max"].default_value = 1.0
            if color_random.inputs.get("Seed"):
                color_random.inputs["Seed"].default_value = 173

            last_color_node = nodes.new("FunctionNodeInputColor")
            last_color_node.label = "PALETTE / FINAL COLOR"
            last_color_node.location = (-420, 1160)
            last_color_node.value = normalized_palette[-1][0]
            color_field_socket = last_color_node.outputs["Color"]

            cumulative_weight = 0.0
            thresholds = []
            for color_index, (_color, weight) in enumerate(normalized_palette[:-1]):
                cumulative_weight += weight
                thresholds.append((color_index, cumulative_weight))

            for color_index, threshold in reversed(thresholds):
                color_test = nodes.new("ShaderNodeMath")
                color_test.operation = 'LESS_THAN'
                color_test.label = f"COLOR {color_index + 1} WEIGHT THRESHOLD"
                color_test.location = (-410, 900 - color_index * 150)
                color_test.inputs[1].default_value = threshold
                links.new(color_random.outputs["Value"], color_test.inputs[0])

                color_switch = nodes.new("GeometryNodeSwitch")
                color_switch.input_type = 'RGBA'
                color_switch.label = f"SELECT PALETTE COLOR {color_index + 1}"
                color_switch.location = (-150, 900 - color_index * 150)
                color_switch.inputs["True"].default_value = normalized_palette[color_index][0]
                links.new(color_test.outputs[0], color_switch.inputs["Switch"])
                links.new(color_field_socket, color_switch.inputs["False"])
                color_field_socket = color_switch.outputs["Output"]

            store_particle_color = nodes.new("GeometryNodeStoreNamedAttribute")
            store_particle_color.label = "B-FLOW / STORE RANDOM PARTICLE COLOR"
            store_particle_color.location = (80, 900)
            store_particle_color.domain = 'POINT'
            store_particle_color.data_type = 'FLOAT_COLOR'
            store_particle_color.inputs["Name"].default_value = "bflow_color"
            links.new(birth_geometry, store_particle_color.inputs["Geometry"])
            links.new(color_field_socket, store_particle_color.inputs["Value"])
            birth_geometry = store_particle_color.outputs["Geometry"]

        # Choose a weighted Shape Instance object once at Birth and store its
        # index on each particle. This keeps each particle's mesh stable for life.
        if len(shape_instance_choices) > 1:
            valid_total = sum(weight for _object, weight in shape_instance_choices)
            normalized_shape_weights = [
                (source, weight / valid_total)
                for source, weight in shape_instance_choices
            ]

            shape_random = nodes.new("FunctionNodeRandomValue")
            shape_random.data_type = 'FLOAT'
            shape_random.label = "WEIGHTED RANDOM SHAPE PICK"
            shape_random.location = (-650, 1320)
            shape_random.inputs["Min"].default_value = 0.0
            shape_random.inputs["Max"].default_value = 1.0
            shape_id = nodes.new("GeometryNodeInputID")
            shape_id.label = "PARTICLE ID / SHAPE SEED"
            shape_id.location = (-860, 1320)
            if shape_random.inputs.get("ID") is not None:
                links.new(shape_id.outputs["ID"], shape_random.inputs["ID"])

            shape_index_socket = None
            cumulative_shape_weight = 0.0
            shape_thresholds = []
            for shape_index, (_source, weight) in enumerate(normalized_shape_weights[:-1]):
                cumulative_shape_weight += weight
                shape_thresholds.append((shape_index, cumulative_shape_weight))

            # Default to the last item; earlier entries claim their percentage ranges.
            for shape_index, threshold in reversed(shape_thresholds):
                shape_test = nodes.new("ShaderNodeMath")
                shape_test.operation = 'LESS_THAN'
                shape_test.label = f"SHAPE {shape_index + 1} WEIGHT THRESHOLD"
                shape_test.location = (-420, 1320 - shape_index * 150)
                shape_test.inputs[1].default_value = threshold
                links.new(shape_random.outputs["Value"], shape_test.inputs[0])

                shape_switch = nodes.new("GeometryNodeSwitch")
                shape_switch.input_type = 'INT'
                shape_switch.label = f"SELECT SHAPE {shape_index + 1}"
                shape_switch.location = (-180, 1320 - shape_index * 150)
                shape_switch.inputs["True"].default_value = shape_index
                shape_switch.inputs["False"].default_value = (
                    len(normalized_shape_weights) - 1
                    if shape_index_socket is None
                    else 0
                )
                links.new(shape_test.outputs[0], shape_switch.inputs["Switch"])
                if shape_index_socket is not None:
                    links.new(shape_index_socket, shape_switch.inputs["False"])
                shape_index_socket = shape_switch.outputs["Output"]

            if shape_index_socket is None:
                shape_index_socket = None

            store_shape_index = nodes.new("GeometryNodeStoreNamedAttribute")
            store_shape_index.label = "B-FLOW / STORE PARTICLE SHAPE INDEX"
            store_shape_index.location = (80, 1320)
            store_shape_index.domain = 'POINT'
            store_shape_index.data_type = 'INT'
            store_shape_index.inputs["Name"].default_value = "bflow_shape_index"
            links.new(birth_geometry, store_shape_index.inputs["Geometry"])
            if shape_index_socket is not None:
                links.new(shape_index_socket, store_shape_index.inputs["Value"])
            else:
                store_shape_index.inputs["Value"].default_value = len(normalized_shape_weights) - 1
            birth_geometry = store_shape_index.outputs["Geometry"]

        elif len(shape_instance_choices) == 1:
            store_shape_index = nodes.new("GeometryNodeStoreNamedAttribute")
            store_shape_index.label = "B-FLOW / STORE SINGLE SHAPE INDEX"
            store_shape_index.location = (80, 1320)
            store_shape_index.domain = 'POINT'
            store_shape_index.data_type = 'INT'
            store_shape_index.inputs["Name"].default_value = "bflow_shape_index"
            store_shape_index.inputs["Value"].default_value = 0
            links.new(birth_geometry, store_shape_index.inputs["Geometry"])
            birth_geometry = store_shape_index.outputs["Geometry"]

        # Create the initial velocity vector field used to seed every particle.
        initial_velocity = nodes.new("ShaderNodeVectorMath")
        initial_velocity.operation = 'SCALE'
        initial_velocity.label = "INITIAL SPEED — " + ("SURFACE NORMAL" if speed_mode == "NORMAL" else "CUSTOM")
        initial_velocity.location = (-420, -160)

        if speed_randomness > 0.0:
            speed_random = nodes.new("FunctionNodeRandomValue")
            speed_random.data_type = 'FLOAT'
            speed_random.label = "PER-PARTICLE SPEED RANDOM"
            speed_random.location = (-650, -420)
            speed_random.inputs["Min"].default_value = speed_value * (1.0 - speed_randomness)
            speed_random.inputs["Max"].default_value = speed_value * (1.0 + speed_randomness)
            links.new(speed_random.outputs["Value"], initial_velocity.inputs["Scale"])
        else:
            initial_velocity.inputs["Scale"].default_value = speed_value

        if speed_mode == "NORMAL":
            # Each particle uses its own emitter surface normal.
            links.new(distribute.outputs["Normal"], initial_velocity.inputs[0])
        else:
            initial_velocity.inputs[0].default_value = speed_direction

        # Birth is a two-stage state: a particle is created stationary,
        # while its configured Speed vector is stored separately and only
        # activated when the particle enters the Simulation Zone next step.
        store_velocity = nodes.new("GeometryNodeStoreNamedAttribute")
        store_velocity.label = "BIRTH VELOCITY = ZERO"
        store_velocity.location = (-480, 100)
        store_velocity.domain = 'POINT'
        store_velocity.data_type = 'FLOAT_VECTOR'
        store_velocity.inputs["Name"].default_value = "bflow_velocity"
        store_velocity.inputs["Value"].default_value = (0.0, 0.0, 0.0)
        links.new(birth_geometry, store_velocity.inputs["Geometry"])

        store_pending_velocity = nodes.new("GeometryNodeStoreNamedAttribute")
        store_pending_velocity.label = "STORE PENDING SPEED"
        store_pending_velocity.location = (-260, 100)
        store_pending_velocity.domain = 'POINT'
        store_pending_velocity.data_type = 'FLOAT_VECTOR'
        store_pending_velocity.inputs["Name"].default_value = "bflow_pending_velocity"
        links.new(store_velocity.outputs["Geometry"], store_pending_velocity.inputs["Geometry"])
        links.new(initial_velocity.outputs["Vector"], store_pending_velocity.inputs["Value"])

        store_velocity_started = nodes.new("GeometryNodeStoreNamedAttribute")
        store_velocity_started.label = "SPEED NOT STARTED YET"
        store_velocity_started.location = (-40, 100)
        store_velocity_started.domain = 'POINT'
        store_velocity_started.data_type = 'BOOLEAN'
        store_velocity_started.inputs["Name"].default_value = "bflow_velocity_started"
        store_velocity_started.inputs["Value"].default_value = False
        links.new(store_pending_velocity.outputs["Geometry"], store_velocity_started.inputs["Geometry"])

        # Store the exact scene frame on every newborn particle. Movement is
        # gated against this immutable per-particle value, rather than relying
        # only on a simulation boolean that may be evaluated in the same step.
        store_birth_frame = nodes.new("GeometryNodeStoreNamedAttribute")
        store_birth_frame.label = "STORE PARTICLE BIRTH FRAME"
        store_birth_frame.location = (180, 100)
        store_birth_frame.domain = 'POINT'
        store_birth_frame.data_type = 'FLOAT'
        store_birth_frame.inputs["Name"].default_value = "bflow_birth_frame"
        links.new(store_velocity_started.outputs["Geometry"], store_birth_frame.inputs["Geometry"])
        links.new(scene_time.outputs["Frame"], store_birth_frame.inputs["Value"])

        # Empty initial state; new Birth points are joined into the simulation each frame.
        empty_mesh = nodes.new("GeometryNodeMeshLine")
        empty_mesh.label = "EMPTY INITIAL PARTICLES"
        empty_mesh.location = (-450, 650)
        empty_mesh.inputs["Count"].default_value = 0

        sim_in = nodes.new("GeometryNodeSimulationInput")
        sim_in.location = (-180, 100)
        sim_out = nodes.new("GeometryNodeSimulationOutput")
        sim_out.location = (720, 100)
        sim_in.pair_with_output(sim_out)
        links.new(empty_mesh.outputs["Mesh"], sim_in.inputs["Geometry"])

        # Existing particles enter the simulation update first.
        # Newly born particles are appended only after movement/collision so
        # they remain exactly at the emitter position on their birth frame.

        read_velocity = nodes.new("GeometryNodeInputNamedAttribute")
        read_velocity.data_type = 'FLOAT_VECTOR'
        read_velocity.location = (40, -180)
        read_velocity.inputs["Name"].default_value = "bflow_velocity"

        gravity_dt = nodes.new("ShaderNodeVectorMath")
        gravity_dt.operation = 'SCALE'
        gravity_dt.location = (40, -420)
        gravity_dt.inputs[0].default_value = gravity

        add_acceleration = nodes.new("ShaderNodeVectorMath")
        add_acceleration.operation = 'ADD'
        add_acceleration.location = (260, -160)

        update_velocity = nodes.new("GeometryNodeStoreNamedAttribute")
        update_velocity.label = "Integrate Velocity"
        update_velocity.location = (480, 100)
        update_velocity.domain = 'POINT'
        update_velocity.data_type = 'FLOAT_VECTOR'
        update_velocity.inputs["Name"].default_value = "bflow_velocity"

        move_dt = nodes.new("ShaderNodeVectorMath")
        move_dt.operation = 'SCALE'
        move_dt.location = (480, -300)

        move_particles = nodes.new("GeometryNodeSetPosition")
        move_particles.label = "Integrate Position"
        move_particles.location = (700, -180)

        dt_socket = sim_in.outputs.get("Delta Time")
        if dt_socket is None:
            self.report({'ERROR'}, "Blender did not expose Simulation Delta Time")
            return {'CANCELLED'}

        # Activate pending Speed only when the particle is processed by the
        # Simulation Zone. New births are appended after the current simulation
        # step, so their first processing occurs on the following frame.
        read_velocity_started = nodes.new("GeometryNodeInputNamedAttribute")
        read_velocity_started.data_type = 'BOOLEAN'
        read_velocity_started.label = "READ SPEED STARTED STATE"
        read_velocity_started.location = (40, -650)
        read_velocity_started.inputs["Name"].default_value = "bflow_velocity_started"

        read_pending_velocity = nodes.new("GeometryNodeInputNamedAttribute")
        read_pending_velocity.data_type = 'FLOAT_VECTOR'
        read_pending_velocity.label = "READ PENDING SPEED"
        read_pending_velocity.location = (40, -850)
        read_pending_velocity.inputs["Name"].default_value = "bflow_pending_velocity"

        activate_speed = nodes.new("GeometryNodeSwitch")
        activate_speed.input_type = 'VECTOR'
        activate_speed.label = "ACTIVATE SPEED AFTER BIRTH"
        activate_speed.location = (260, -650)
        links.new(read_velocity_started.outputs["Attribute"], activate_speed.inputs["Switch"])
        links.new(read_pending_velocity.outputs["Attribute"], activate_speed.inputs["False"])
        links.new(read_velocity.outputs["Attribute"], activate_speed.inputs["True"])

        dt_socket = sim_in.outputs.get("Delta Time")
        if dt_socket is None:
            self.report({'ERROR'}, "Blender did not expose Simulation Delta Time")
            return {'CANCELLED'}

        # Named Attribute is a field node; it does not take Geometry input.
        links.new(dt_socket, gravity_dt.inputs["Scale"])
        links.new(activate_speed.outputs["Output"], add_acceleration.inputs[0])

        # Selected native Blender Force Fields are translated into equivalent
        # Geometry Nodes acceleration fields. Their object transforms/settings
        # are sampled at compile time; recompile after changing them.
        acceleration_source = gravity_dt.outputs["Vector"]
        force_position = nodes.new("GeometryNodeInputPosition")
        force_position.label = "PARTICLE POSITION FOR FORCE FIELDS"
        force_position.location = (40, -1450)

        for force_index, (field_obj, influence) in enumerate(selected_force_fields):
            settings = field_obj.field
            field_type = getattr(settings, "type", "FORCE")
            strength_value = float(getattr(settings, "strength", 1.0)) * influence
            info = nodes.new("GeometryNodeObjectInfo")
            info.label = f"NATIVE FIELD: {field_obj.name} ({field_type})"
            info.location = (40 + force_index * 260, -1750)
            info.transform_space = 'ORIGINAL'
            info.inputs["Object"].default_value = field_obj

            scale_node = nodes.new("ShaderNodeVectorMath")
            scale_node.operation = 'SCALE'
            scale_node.location = (260 + force_index * 260, -1750)
            scale_node.inputs["Scale"].default_value = strength_value

            if field_type == 'FORCE':
                toward = nodes.new("ShaderNodeVectorMath")
                toward.operation = 'SUBTRACT'
                toward.location = (260 + force_index * 260, -1950)
                links.new(info.outputs["Location"], toward.inputs[0])
                links.new(force_position.outputs["Position"], toward.inputs[1])
                normalize = nodes.new("ShaderNodeVectorMath")
                normalize.operation = 'NORMALIZE'
                normalize.location = (440 + force_index * 260, -1950)
                links.new(toward.outputs["Vector"], normalize.inputs[0])
                links.new(normalize.outputs["Vector"], scale_node.inputs[0])
            elif field_type == 'WIND':
                axis = nodes.new("ShaderNodeVectorRotate")
                _bflow_set_vector_rotate_euler(axis)
                axis.location = (260 + force_index * 260, -1950)
                axis.inputs["Vector"].default_value = (0.0, 0.0, 1.0)
                links.new(info.outputs["Rotation"], axis.inputs["Rotation"])
                links.new(axis.outputs["Vector"], scale_node.inputs[0])
            elif field_type == 'VORTEX':
                radial = nodes.new("ShaderNodeVectorMath")
                radial.operation = 'SUBTRACT'
                radial.location = (260 + force_index * 260, -1950)
                links.new(force_position.outputs["Position"], radial.inputs[0])
                links.new(info.outputs["Location"], radial.inputs[1])
                axis = nodes.new("ShaderNodeVectorRotate")
                _bflow_set_vector_rotate_euler(axis)
                axis.location = (260 + force_index * 260, -2150)
                axis.inputs["Vector"].default_value = (0.0, 0.0, 1.0)
                links.new(info.outputs["Rotation"], axis.inputs["Rotation"])
                cross = nodes.new("ShaderNodeVectorMath")
                cross.operation = 'CROSS_PRODUCT'
                cross.location = (440 + force_index * 260, -1950)
                links.new(axis.outputs["Vector"], cross.inputs[0])
                links.new(radial.outputs["Vector"], cross.inputs[1])
                normalize = nodes.new("ShaderNodeVectorMath")
                normalize.operation = 'NORMALIZE'
                normalize.location = (620 + force_index * 260, -1950)
                links.new(cross.outputs["Vector"], normalize.inputs[0])
                links.new(normalize.outputs["Vector"], scale_node.inputs[0])
            elif field_type == 'DRAG':
                negative_velocity = nodes.new("ShaderNodeVectorMath")
                negative_velocity.operation = 'SCALE'
                negative_velocity.location = (260 + force_index * 260, -1950)
                negative_velocity.inputs["Scale"].default_value = -1.0
                links.new(activate_speed.outputs["Output"], negative_velocity.inputs[0])
                links.new(negative_velocity.outputs["Vector"], scale_node.inputs[0])
            elif field_type == 'TURBULENCE':
                noise = nodes.new("ShaderNodeTexNoise")
                noise.location = (260 + force_index * 260, -2150)
                noise.inputs["Scale"].default_value = max(0.001, float(getattr(settings, "noise", 0.0)) or 1.0)
                links.new(force_position.outputs["Position"], noise.inputs["Vector"])
                center_noise = nodes.new("ShaderNodeVectorMath")
                center_noise.operation = 'SUBTRACT'
                center_noise.location = (440 + force_index * 260, -2150)
                center_noise.inputs[1].default_value = (0.5, 0.5, 0.5)
                links.new(noise.outputs["Color"], center_noise.inputs[0])
                links.new(center_noise.outputs["Vector"], scale_node.inputs[0])
            else:
                self.report({'WARNING'}, f"Force Field '{field_obj.name}' type '{field_type}' is not supported yet")
                continue

            scaled = scale_node.outputs["Vector"]
            force_sum = nodes.new("ShaderNodeVectorMath")
            force_sum.operation = 'ADD'
            force_sum.location = (760 + force_index * 260, -1650)
            links.new(acceleration_source, force_sum.inputs[0])
            links.new(scaled, force_sum.inputs[1])
            acceleration_source = force_sum.outputs["Vector"]

        links.new(acceleration_source, add_acceleration.inputs[1])
        # Preserve the pre-integration position. Object Collision casts
        # forward from this position along this step's travel segment, rather
        # than trying to infer the entry point by casting backward from the
        # already-moved particle.
        previous_position = nodes.new("GeometryNodeInputPosition")
        previous_position.label = "PREVIOUS STEP POSITION"
        previous_position.location = (40, -1120)

        store_previous_position = nodes.new("GeometryNodeStoreNamedAttribute")
        store_previous_position.label = "STORE PREVIOUS POSITION"
        store_previous_position.location = (260, 100)
        store_previous_position.domain = 'POINT'
        store_previous_position.data_type = 'FLOAT_VECTOR'
        store_previous_position.inputs["Name"].default_value = "bflow_previous_position"
        links.new(sim_in.outputs["Geometry"], store_previous_position.inputs["Geometry"])
        links.new(previous_position.outputs["Position"], store_previous_position.inputs["Value"])

        links.new(store_previous_position.outputs["Geometry"], update_velocity.inputs["Geometry"])
        links.new(add_acceleration.outputs["Vector"], update_velocity.inputs["Value"])
        # IMPORTANT: the first simulation evaluation of a newly emitted
        # particle must initialize its stored velocity but must not translate
        # its position. The previous version selected Pending Speed as the
        # integration velocity when the flag was False, which still moved the
        # particle on its birth frame.
        movement_velocity = nodes.new("GeometryNodeSwitch")
        movement_velocity.input_type = 'VECTOR'
        movement_velocity.label = "NO MOVEMENT ON BIRTH STEP"
        movement_velocity.location = (470, -520)
        movement_velocity.inputs["False"].default_value = (0.0, 0.0, 0.0)
        read_birth_frame = nodes.new("GeometryNodeInputNamedAttribute")
        read_birth_frame.data_type = 'FLOAT'
        read_birth_frame.label = "READ PARTICLE BIRTH FRAME"
        read_birth_frame.location = (260, -920)
        read_birth_frame.inputs["Name"].default_value = "bflow_birth_frame"

        frame_after_birth = nodes.new("ShaderNodeMath")
        frame_after_birth.operation = 'GREATER_THAN'
        frame_after_birth.label = "MOVE ONLY AFTER BIRTH FRAME"
        frame_after_birth.location = (470, -760)
        links.new(scene_time.outputs["Frame"], frame_after_birth.inputs[0])
        links.new(read_birth_frame.outputs["Attribute"], frame_after_birth.inputs[1])

        links.new(frame_after_birth.outputs[0], movement_velocity.inputs["Switch"])
        links.new(add_acceleration.outputs["Vector"], movement_velocity.inputs["True"])

        links.new(movement_velocity.outputs["Output"], move_dt.inputs[0])
        links.new(dt_socket, move_dt.inputs["Scale"])
        links.new(update_velocity.outputs["Geometry"], move_particles.inputs["Geometry"])
        links.new(move_dt.outputs["Vector"], move_particles.inputs["Offset"])

        final_geometry = move_particles.outputs["Geometry"]

        mark_velocity_started = nodes.new("GeometryNodeStoreNamedAttribute")
        mark_velocity_started.label = "MARK SPEED AS STARTED"
        mark_velocity_started.location = (900, 100)
        mark_velocity_started.domain = 'POINT'
        mark_velocity_started.data_type = 'BOOLEAN'
        mark_velocity_started.inputs["Name"].default_value = "bflow_velocity_started"
        mark_velocity_started.inputs["Value"].default_value = True
        links.new(final_geometry, mark_velocity_started.inputs["Geometry"])
        final_geometry = mark_velocity_started.outputs["Geometry"]

        # KEEP DIST: soft pairwise particle separation.
        # This is a positional correction, not a rigid-body collision. The
        # exact all-pairs approach scales as N*N, so keep particle counts
        # moderate when this operator is enabled.
        if keep_dist_enabled and keep_dist_strength > 0.0:
            keep_source_geometry = final_geometry

            keep_owner_index = nodes.new("GeometryNodeInputIndex")
            keep_owner_index.label = "SOURCE PARTICLE INDEX"
            keep_owner_index.location = (980, -1700)

            store_keep_owner = nodes.new("GeometryNodeStoreNamedAttribute")
            store_keep_owner.label = "STORE KEEP DIST OWNER ID"
            store_keep_owner.location = (1180, 100)
            store_keep_owner.domain = 'POINT'
            store_keep_owner.data_type = 'INT'
            store_keep_owner.inputs["Name"].default_value = "bflow_keepdist_owner_id"
            links.new(keep_source_geometry, store_keep_owner.inputs["Geometry"])
            links.new(keep_owner_index.outputs["Index"], store_keep_owner.inputs["Value"])

            keep_domain_size = nodes.new("GeometryNodeAttributeDomainSize")
            keep_domain_size.label = "COUNT PARTICLES"
            keep_domain_size.location = (980, -1940)
            keep_domain_size.component = 'POINTCLOUD'
            links.new(store_keep_owner.outputs["Geometry"], keep_domain_size.inputs["Geometry"])

            duplicate_pairs = nodes.new("GeometryNodeDuplicateElements")
            duplicate_pairs.label = "BUILD PARTICLE PAIRS (N x N)"
            duplicate_pairs.location = (1410, 100)
            duplicate_pairs.domain = 'POINT'
            links.new(store_keep_owner.outputs["Geometry"], duplicate_pairs.inputs["Geometry"])
            links.new(keep_domain_size.outputs["Point Count"], duplicate_pairs.inputs["Amount"])

            # The original point position is the source of this pair; the
            # Duplicate Index selects the candidate neighbor from the source set.
            keep_current_position = nodes.new("GeometryNodeInputPosition")
            keep_current_position.label = "CURRENT PARTICLE POSITION"
            keep_current_position.location = (1180, -2180)

            sample_neighbor_position = nodes.new("GeometryNodeSampleIndex")
            sample_neighbor_position.label = "SAMPLE NEIGHBOR POSITION"
            sample_neighbor_position.location = (1640, -1940)
            sample_neighbor_position.data_type = 'FLOAT_VECTOR'
            sample_neighbor_position.domain = 'POINT'
            sample_neighbor_position.clamp = True
            links.new(store_keep_owner.outputs["Geometry"], sample_neighbor_position.inputs["Geometry"])
            links.new(keep_current_position.outputs["Position"], sample_neighbor_position.inputs["Value"])
            links.new(duplicate_pairs.outputs["Duplicate Index"], sample_neighbor_position.inputs["Index"])

            pair_delta = nodes.new("ShaderNodeVectorMath")
            pair_delta.operation = 'SUBTRACT'
            pair_delta.label = "PARTICLE TO NEIGHBOR VECTOR"
            pair_delta.location = (1870, -1700)
            links.new(keep_current_position.outputs["Position"], pair_delta.inputs[0])
            links.new(sample_neighbor_position.outputs["Value"], pair_delta.inputs[1])

            pair_distance = nodes.new("ShaderNodeVectorMath")
            pair_distance.operation = 'LENGTH'
            pair_distance.label = "PAIR DISTANCE"
            pair_distance.location = (2080, -1700)
            links.new(pair_delta.outputs["Vector"], pair_distance.inputs[0])

            # Identify self-pairs so each particle does not repel itself.
            read_owner_id = nodes.new("GeometryNodeInputNamedAttribute")
            read_owner_id.data_type = 'INT'
            read_owner_id.label = "READ SOURCE PARTICLE ID"
            read_owner_id.location = (1640, -2180)
            read_owner_id.inputs["Name"].default_value = "bflow_keepdist_owner_id"

            compare_not_self = nodes.new("FunctionNodeCompare")
            compare_not_self.data_type = 'INT'
            compare_not_self.operation = 'NOT_EQUAL'
            compare_not_self.label = "IGNORE SELF PAIR"
            compare_not_self.location = (1870, -2180)
            links.new(read_owner_id.outputs["Attribute"], compare_not_self.inputs["A"])
            links.new(duplicate_pairs.outputs["Duplicate Index"], compare_not_self.inputs["B"])

            compare_inside_distance = nodes.new("FunctionNodeCompare")
            compare_inside_distance.data_type = 'FLOAT'
            compare_inside_distance.operation = 'LESS_THAN'
            compare_inside_distance.label = "INSIDE KEEP DISTANCE"
            compare_inside_distance.location = (2300, -1700)
            compare_inside_distance.inputs["B"].default_value = keep_distance
            links.new(pair_distance.outputs["Value"], compare_inside_distance.inputs["A"])

            valid_pair = nodes.new("FunctionNodeBooleanMath")
            valid_pair.operation = 'AND'
            valid_pair.label = "VALID NEIGHBOR PAIR"
            valid_pair.location = (2520, -1880)
            links.new(compare_not_self.outputs["Result"], valid_pair.inputs[0])
            links.new(compare_inside_distance.outputs["Result"], valid_pair.inputs[1])

            # When two particles occupy the exact same position, use an
            # index-based opposite X direction to break the symmetry.
            near_zero_distance = nodes.new("FunctionNodeCompare")
            near_zero_distance.data_type = 'FLOAT'
            near_zero_distance.operation = 'LESS_THAN'
            near_zero_distance.label = "COINCIDENT PARTICLES"
            near_zero_distance.location = (2300, -2110)
            near_zero_distance.inputs["B"].default_value = 0.000001
            links.new(pair_distance.outputs["Value"], near_zero_distance.inputs["A"])

            owner_before_neighbor = nodes.new("FunctionNodeCompare")
            owner_before_neighbor.data_type = 'INT'
            owner_before_neighbor.operation = 'LESS_THAN'
            owner_before_neighbor.label = "PAIR ORDER"
            owner_before_neighbor.location = (2080, -2380)
            links.new(read_owner_id.outputs["Attribute"], owner_before_neighbor.inputs["A"])
            links.new(duplicate_pairs.outputs["Duplicate Index"], owner_before_neighbor.inputs["B"])

            fallback_direction = nodes.new("GeometryNodeSwitch")
            fallback_direction.input_type = 'VECTOR'
            fallback_direction.label = "COINCIDENT PAIR FALLBACK"
            fallback_direction.location = (2300, -2380)
            fallback_direction.inputs["False"].default_value = (-1.0, 0.0, 0.0)
            fallback_direction.inputs["True"].default_value = (1.0, 0.0, 0.0)
            links.new(owner_before_neighbor.outputs["Result"], fallback_direction.inputs["Switch"])

            normalized_pair_direction = nodes.new("ShaderNodeVectorMath")
            normalized_pair_direction.operation = 'NORMALIZE'
            normalized_pair_direction.label = "NORMALIZE SEPARATION"
            normalized_pair_direction.location = (2300, -1500)
            links.new(pair_delta.outputs["Vector"], normalized_pair_direction.inputs[0])

            choose_pair_direction = nodes.new("GeometryNodeSwitch")
            choose_pair_direction.input_type = 'VECTOR'
            choose_pair_direction.label = "SAFE SEPARATION DIRECTION"
            choose_pair_direction.location = (2520, -1480)
            links.new(near_zero_distance.outputs["Result"], choose_pair_direction.inputs["Switch"])
            links.new(normalized_pair_direction.outputs["Vector"], choose_pair_direction.inputs["False"])
            links.new(fallback_direction.outputs["Output"], choose_pair_direction.inputs["True"])

            distance_deficit = nodes.new("ShaderNodeMath")
            distance_deficit.operation = 'SUBTRACT'
            distance_deficit.label = "DISTANCE OVERLAP"
            distance_deficit.location = (2520, -1700)
            distance_deficit.inputs[0].default_value = keep_distance
            links.new(pair_distance.outputs["Value"], distance_deficit.inputs[1])

            non_negative_deficit = nodes.new("ShaderNodeMath")
            non_negative_deficit.operation = 'MAXIMUM'
            non_negative_deficit.location = (2730, -1700)
            non_negative_deficit.inputs[1].default_value = 0.0
            links.new(distance_deficit.outputs[0], non_negative_deficit.inputs[0])

            pair_push = nodes.new("ShaderNodeVectorMath")
            pair_push.operation = 'SCALE'
            pair_push.label = "PAIRWISE REPULSION"
            pair_push.location = (2940, -1480)
            links.new(choose_pair_direction.outputs["Output"], pair_push.inputs[0])
            links.new(non_negative_deficit.outputs[0], pair_push.inputs["Scale"])

            gate_pair_push = nodes.new("GeometryNodeSwitch")
            gate_pair_push.input_type = 'VECTOR'
            gate_pair_push.label = "IGNORE DISTANT / SELF PAIRS"
            gate_pair_push.location = (3150, -1480)
            gate_pair_push.inputs["False"].default_value = (0.0, 0.0, 0.0)
            links.new(valid_pair.outputs["Boolean"], gate_pair_push.inputs["Switch"])
            links.new(pair_push.outputs["Vector"], gate_pair_push.inputs["True"])

            # Sum all neighbor pushes belonging to each original point.
            sum_pair_push = nodes.new("GeometryNodeAccumulateField")
            sum_pair_push.data_type = 'FLOAT_VECTOR'
            sum_pair_push.domain = 'POINT'
            sum_pair_push.label = "SUM NEIGHBOR PUSHES"
            sum_pair_push.location = (3370, -1480)
            links.new(gate_pair_push.outputs["Output"], sum_pair_push.inputs["Value"])
            links.new(read_owner_id.outputs["Attribute"], sum_pair_push.inputs["Group ID"])

            count_pair = nodes.new("GeometryNodeSwitch")
            count_pair.input_type = 'FLOAT'
            count_pair.label = "COUNT CLOSE NEIGHBORS"
            count_pair.location = (3150, -1880)
            count_pair.inputs["False"].default_value = 0.0
            count_pair.inputs["True"].default_value = 1.0
            links.new(valid_pair.outputs["Boolean"], count_pair.inputs["Switch"])

            sum_pair_count = nodes.new("GeometryNodeAccumulateField")
            sum_pair_count.data_type = 'FLOAT'
            sum_pair_count.domain = 'POINT'
            sum_pair_count.label = "SUM NEIGHBOR COUNT"
            sum_pair_count.location = (3370, -1880)
            links.new(count_pair.outputs["Output"], sum_pair_count.inputs["Value"])
            links.new(read_owner_id.outputs["Attribute"], sum_pair_count.inputs["Group ID"])

            safe_neighbor_count = nodes.new("ShaderNodeMath")
            safe_neighbor_count.operation = 'MAXIMUM'
            safe_neighbor_count.label = "SAFE NEIGHBOR COUNT"
            safe_neighbor_count.location = (3580, -1880)
            safe_neighbor_count.inputs[1].default_value = 1.0
            links.new(sum_pair_count.outputs["Total"], safe_neighbor_count.inputs[0])

            # Split overlap correction evenly between the two particles,
            # average across neighbors, then apply user strength.
            half_strength = nodes.new("ShaderNodeMath")
            half_strength.operation = 'MULTIPLY'
            half_strength.label = "HALF KEEP DIST STRENGTH"
            half_strength.location = (3580, -2110)
            half_strength.inputs[0].default_value = keep_dist_strength
            half_strength.inputs[1].default_value = 0.5

            correction_factor = nodes.new("ShaderNodeMath")
            correction_factor.operation = 'DIVIDE'
            correction_factor.label = "AVERAGE REPULSION"
            correction_factor.location = (3790, -1880)
            links.new(half_strength.outputs[0], correction_factor.inputs[0])
            links.new(safe_neighbor_count.outputs[0], correction_factor.inputs[1])

            averaged_pair_push = nodes.new("ShaderNodeVectorMath")
            averaged_pair_push.operation = 'SCALE'
            averaged_pair_push.label = "AVERAGED SEPARATION OFFSET"
            averaged_pair_push.location = (4000, -1480)
            links.new(sum_pair_push.outputs["Total"], averaged_pair_push.inputs[0])
            links.new(correction_factor.outputs[0], averaged_pair_push.inputs["Scale"])

            store_keep_offset = nodes.new("GeometryNodeStoreNamedAttribute")
            store_keep_offset.label = "STORE KEEP DIST OFFSET"
            store_keep_offset.location = (4220, 100)
            store_keep_offset.domain = 'POINT'
            store_keep_offset.data_type = 'FLOAT_VECTOR'
            store_keep_offset.inputs["Name"].default_value = "bflow_keepdist_offset"
            links.new(duplicate_pairs.outputs["Geometry"], store_keep_offset.inputs["Geometry"])
            links.new(averaged_pair_push.outputs["Vector"], store_keep_offset.inputs["Value"])

            # Duplicate Index 0 is the representative for each source point.
            # Remove all other duplicates after the accumulated field is stored.
            duplicate_index_nonzero = nodes.new("FunctionNodeCompare")
            duplicate_index_nonzero.data_type = 'INT'
            duplicate_index_nonzero.operation = 'GREATER_THAN'
            duplicate_index_nonzero.label = "KEEP ONE COPY PER PARTICLE"
            duplicate_index_nonzero.location = (4000, -2110)
            duplicate_index_nonzero.inputs["B"].default_value = 0
            links.new(duplicate_pairs.outputs["Duplicate Index"], duplicate_index_nonzero.inputs["A"])

            remove_extra_pairs = nodes.new("GeometryNodeDeleteGeometry")
            remove_extra_pairs.label = "COLLAPSE PAIRS BACK TO PARTICLES"
            remove_extra_pairs.location = (4440, 100)
            remove_extra_pairs.domain = 'POINT'
            links.new(store_keep_offset.outputs["Geometry"], remove_extra_pairs.inputs["Geometry"])
            links.new(duplicate_index_nonzero.outputs["Result"], remove_extra_pairs.inputs["Selection"])

            read_keep_offset = nodes.new("GeometryNodeInputNamedAttribute")
            read_keep_offset.data_type = 'FLOAT_VECTOR'
            read_keep_offset.label = "READ KEEP DIST OFFSET"
            read_keep_offset.location = (4220, -1700)
            read_keep_offset.inputs["Name"].default_value = "bflow_keepdist_offset"

            apply_keep_offset = nodes.new("GeometryNodeSetPosition")
            apply_keep_offset.label = "KEEP DIST / SOFT SEPARATION"
            apply_keep_offset.location = (4660, 100)
            links.new(remove_extra_pairs.outputs["Geometry"], apply_keep_offset.inputs["Geometry"])
            links.new(read_keep_offset.outputs["Attribute"], apply_keep_offset.inputs["Offset"])
            final_geometry = apply_keep_offset.outputs["Geometry"]

        # SPIN: accumulate Euler rotation per simulation step on each particle.
        if spin_enabled:
            previous_rotation = nodes.new("GeometryNodeInputNamedAttribute")
            previous_rotation.data_type = 'FLOAT_VECTOR'
            previous_rotation.label = "READ PARTICLE ROTATION"
            previous_rotation.location = (650, 100)

            previous_rotation.inputs["Name"].default_value = "bflow_rotation"

            spin_axis_node = nodes.new("ShaderNodeCombineXYZ")
            spin_axis_node.location = (650, -180)
            spin_axis_node.inputs["X"].default_value = spin_axis[0]
            spin_axis_node.inputs["Y"].default_value = spin_axis[1]
            spin_axis_node.inputs["Z"].default_value = spin_axis[2]

            spin_speed = nodes.new("ShaderNodeMath")
            spin_speed.operation = 'MULTIPLY'
            spin_speed.label = "SPIN SPEED WITH RANDOMNESS"
            spin_speed.location = (850, -360)
            spin_speed.inputs[0].default_value = spin_strength

            if spin_randomness > 0.0:
                spin_random = nodes.new("FunctionNodeRandomValue")
                spin_random.data_type = 'FLOAT'
                spin_random.label = "PER-PARTICLE SPIN VARIATION"
                spin_random.location = (650, -520)
                spin_random.inputs["Min"].default_value = 1.0 - spin_randomness
                spin_random.inputs["Max"].default_value = 1.0 + spin_randomness
                links.new(spin_random.outputs["Value"], spin_speed.inputs[1])
            else:
                spin_speed.inputs[1].default_value = 1.0

            spin_step = nodes.new("ShaderNodeMath")
            spin_step.operation = 'MULTIPLY'
            spin_step.location = (1050, -300)
            links.new(spin_speed.outputs[0], spin_step.inputs[0])
            links.new(sim_in.outputs["Delta Time"], spin_step.inputs[1])

            spin_delta = nodes.new("ShaderNodeVectorMath")
            spin_delta.operation = 'SCALE'
            spin_delta.location = (1250, -100)
            links.new(spin_axis_node.outputs["Vector"], spin_delta.inputs[0])
            links.new(spin_step.outputs[0], spin_delta.inputs["Scale"])

            accumulated_rotation = nodes.new("ShaderNodeVectorMath")
            accumulated_rotation.operation = 'ADD'
            accumulated_rotation.location = (1460, 100)
            links.new(previous_rotation.outputs["Attribute"], accumulated_rotation.inputs[0])
            links.new(spin_delta.outputs["Vector"], accumulated_rotation.inputs[1])

            store_rotation = nodes.new("GeometryNodeStoreNamedAttribute")
            store_rotation.label = "STORE PARTICLE ROTATION"
            store_rotation.location = (1650, 100)
            store_rotation.domain = 'POINT'
            store_rotation.data_type = 'FLOAT_VECTOR'
            store_rotation.inputs["Name"].default_value = "bflow_rotation"
            links.new(final_geometry, store_rotation.inputs["Geometry"])
            links.new(accumulated_rotation.outputs["Vector"], store_rotation.inputs["Value"])
            final_geometry = store_rotation.outputs["Geometry"]

        if ground_collision_enabled:
            # Ground Collision: horizontal ground-plane collision in Z.
            position = nodes.new("GeometryNodeInputPosition")
            position.location = (850, -520)
            separate_pos = nodes.new("ShaderNodeSeparateXYZ")
            separate_pos.location = (1030, -520)
            links.new(position.outputs["Position"], separate_pos.inputs["Vector"])

            clamp_z = nodes.new("ShaderNodeMath")
            clamp_z.operation = 'MAXIMUM'
            clamp_z.location = (1210, -620)
            clamp_z.inputs[1].default_value = collision_height + particle_radius
            links.new(separate_pos.outputs["Z"], clamp_z.inputs[0])

            combine_pos = nodes.new("ShaderNodeCombineXYZ")
            combine_pos.location = (1400, -520)
            links.new(separate_pos.outputs["X"], combine_pos.inputs["X"])
            links.new(separate_pos.outputs["Y"], combine_pos.inputs["Y"])
            links.new(clamp_z.outputs[0], combine_pos.inputs["Z"])

            hit_test = nodes.new("ShaderNodeMath")
            hit_test.operation = 'LESS_THAN'
            hit_test.location = (1210, -820)
            hit_test.inputs[1].default_value = collision_height + particle_radius
            links.new(separate_pos.outputs["Z"], hit_test.inputs[0])

            # Cache the hit flag on the pre-clamped geometry so the test is
            # evaluated before the position is pushed back above the plane.
            store_hit = nodes.new("GeometryNodeStoreNamedAttribute")
            store_hit.label = "CACHE GROUND HIT"
            store_hit.location = (1400, 100)
            store_hit.domain = 'POINT'
            store_hit.data_type = 'BOOLEAN'
            store_hit.inputs["Name"].default_value = "bflow_ground_hit"
            links.new(final_geometry, store_hit.inputs["Geometry"])
            links.new(hit_test.outputs[0], store_hit.inputs["Value"])

            ground_collision_geometry = store_hit.outputs["Geometry"]

            # DEL / After Collision: remove Ground-hit points immediately,
            # using the exact same pre-clamp hit test that drives Ground Bounce.
            if delete_enabled and delete_mode == "AFTER_COLLISION":
                delete_ground_hit = nodes.new("GeometryNodeDeleteGeometry")
                delete_ground_hit.label = "DEL / GROUND HIT — IMMEDIATE"
                delete_ground_hit.location = (1510, 100)
                delete_ground_hit.domain = 'POINT'
                links.new(ground_collision_geometry, delete_ground_hit.inputs["Geometry"])
                links.new(hit_test.outputs[0], delete_ground_hit.inputs["Selection"])
                ground_collision_geometry = delete_ground_hit.outputs["Geometry"]

            set_collision_position = nodes.new("GeometryNodeSetPosition")
            set_collision_position.label = "GROUND COLLISION — POSITION"
            set_collision_position.location = (1700, 100)
            links.new(ground_collision_geometry, set_collision_position.inputs["Geometry"])
            links.new(combine_pos.outputs["Vector"], set_collision_position.inputs["Position"])

            read_hit = nodes.new("GeometryNodeInputNamedAttribute")
            read_hit.data_type = 'BOOLEAN'
            read_hit.location = (1220, -1420)
            read_hit.inputs["Name"].default_value = "bflow_ground_hit"
            ground_collision_hit_field = read_hit.outputs["Attribute"]

            current_v = nodes.new("ShaderNodeSeparateXYZ")
            current_v.location = (850, -980)
            links.new(add_acceleration.outputs["Vector"], current_v.inputs["Vector"])

            if collision_friction_randomness > 0.0:
                friction_random = nodes.new("FunctionNodeRandomValue")
                friction_random.data_type = 'FLOAT'
                friction_random.label = "PER-PARTICLE FRICTION"
                friction_random.location = (820, -1320)
                friction_random.inputs["Min"].default_value = max(0.0, collision_friction - collision_friction_randomness)
                friction_random.inputs["Max"].default_value = min(1.0, collision_friction + collision_friction_randomness)
                friction_factor = nodes.new("ShaderNodeMath")
                friction_factor.operation = 'SUBTRACT'
                friction_factor.inputs[0].default_value = 1.0
                friction_factor.location = (1030, -1320)
                links.new(friction_random.outputs["Value"], friction_factor.inputs[1])
            else:
                friction_factor = None

            damp_x = nodes.new("ShaderNodeMath")
            damp_x.operation = 'MULTIPLY'
            damp_x.location = (1030, -960)
            if friction_factor:
                links.new(friction_factor.outputs[0], damp_x.inputs[1])
            else:
                damp_x.inputs[1].default_value = 1.0 - collision_friction
            links.new(current_v.outputs["X"], damp_x.inputs[0])

            damp_y = nodes.new("ShaderNodeMath")
            damp_y.operation = 'MULTIPLY'
            damp_y.location = (1030, -1080)
            if friction_factor:
                links.new(friction_factor.outputs[0], damp_y.inputs[1])
            else:
                damp_y.inputs[1].default_value = 1.0 - collision_friction
            links.new(current_v.outputs["Y"], damp_y.inputs[0])

            bounce_z = nodes.new("ShaderNodeMath")
            bounce_z.operation = 'MULTIPLY'
            bounce_z.location = (1030, -1200)
            if collision_bounce_randomness > 0.0:
                bounce_random = nodes.new("FunctionNodeRandomValue")
                bounce_random.data_type = 'FLOAT'
                bounce_random.label = "PER-PARTICLE BOUNCE"
                bounce_random.location = (820, -1500)
                # Generate a negative multiplier directly because the vertical
                # velocity must be reflected when it hits the ground.
                bounce_random.inputs["Min"].default_value = -min(1.0, collision_bounce + collision_bounce_randomness)
                bounce_random.inputs["Max"].default_value = -max(0.0, collision_bounce - collision_bounce_randomness)
                links.new(bounce_random.outputs["Value"], bounce_z.inputs[1])
            else:
                bounce_z.inputs[1].default_value = -collision_bounce
            links.new(current_v.outputs["Z"], bounce_z.inputs[0])

            reflected_v = nodes.new("ShaderNodeCombineXYZ")
            reflected_v.location = (1220, -1030)
            links.new(damp_x.outputs[0], reflected_v.inputs["X"])
            links.new(damp_y.outputs[0], reflected_v.inputs["Y"])
            links.new(bounce_z.outputs[0], reflected_v.inputs["Z"])

            choose_velocity = nodes.new("GeometryNodeSwitch")
            choose_velocity.input_type = 'VECTOR'
            choose_velocity.label = "BOUNCE / FRICTION"
            choose_velocity.location = (1420, -980)
            links.new(read_hit.outputs["Attribute"], choose_velocity.inputs["Switch"])
            links.new(add_acceleration.outputs["Vector"], choose_velocity.inputs["False"])
            links.new(reflected_v.outputs["Vector"], choose_velocity.inputs["True"])

            store_collision_velocity = nodes.new("GeometryNodeStoreNamedAttribute")
            store_collision_velocity.label = "STORE COLLISION VELOCITY"
            store_collision_velocity.location = (1830, 100)
            store_collision_velocity.domain = 'POINT'
            store_collision_velocity.data_type = 'FLOAT_VECTOR'
            store_collision_velocity.inputs["Name"].default_value = "bflow_velocity"
            links.new(set_collision_position.outputs["Geometry"], store_collision_velocity.inputs["Geometry"])
            links.new(choose_velocity.outputs["Output"], store_collision_velocity.inputs["Value"])
            final_geometry = store_collision_velocity.outputs["Geometry"]

        # V0.6.8: prevent sticky contact after bounce.
        # Collision response uses a bounded normal impulse and separate
        # tangential damping. This avoids amplifying tangential velocity and
        # prevents the object bounce value from affecting ground collision.
        if object_collision_enabled and collision_object is not None:
            collider_info = nodes.new("GeometryNodeObjectInfo")
            collider_info.label = "OBJECT COLLIDER"
            collider_info.location = (850, 1050)
            collider_info.transform_space = 'RELATIVE'
            collider_info.inputs["Object"].default_value = collision_object

            current_position = nodes.new("GeometryNodeInputPosition")
            current_position.location = (850, 800)

            velocity_field = nodes.new("GeometryNodeInputNamedAttribute")
            velocity_field.data_type = 'FLOAT_VECTOR'
            velocity_field.location = (850, 500)
            velocity_field.inputs["Name"].default_value = "bflow_velocity"

            # Sweep the full movement segment from the previous point position.
            # The old implementation cast backward from the already-moved point;
            # for thick closed meshes this could hit the exit face and reject it
            # as "not approaching". Forward casting finds the entry face instead.
            previous_position_field = nodes.new("GeometryNodeInputNamedAttribute")
            previous_position_field.data_type = 'FLOAT_VECTOR'
            previous_position_field.label = "READ PREVIOUS STEP POSITION"
            previous_position_field.location = (850, 650)
            previous_position_field.inputs["Name"].default_value = "bflow_previous_position"

            normalized_direction = nodes.new("ShaderNodeVectorMath")
            normalized_direction.operation = 'NORMALIZE'
            normalized_direction.label = "NORMALIZED TRAVEL DIRECTION"
            normalized_direction.location = (1080, 500)
            links.new(velocity_field.outputs["Attribute"], normalized_direction.inputs[0])

            velocity_length = nodes.new("ShaderNodeVectorMath")
            velocity_length.operation = 'LENGTH'
            velocity_length.location = (1080, 300)
            links.new(velocity_field.outputs["Attribute"], velocity_length.inputs[0])

            travel_distance = nodes.new("ShaderNodeMath")
            travel_distance.operation = 'MULTIPLY'
            travel_distance.location = (1290, 300)
            links.new(velocity_length.outputs["Value"], travel_distance.inputs[0])
            links.new(sim_in.outputs["Delta Time"], travel_distance.inputs[1])

            ray_length = nodes.new("ShaderNodeMath")
            ray_length.operation = 'ADD'
            ray_length.label = "TRAVEL DISTANCE + PARTICLE RADIUS"
            ray_length.location = (1500, 300)
            links.new(travel_distance.outputs[0], ray_length.inputs[0])
            ray_length.inputs[1].default_value = object_particle_radius

            raycast = nodes.new("GeometryNodeRaycast")
            raycast.label = "FORWARD SWEPT PARTICLE COLLISION"
            raycast.location = (1720, 850)
            links.new(collider_info.outputs["Geometry"], raycast.inputs["Target Geometry"])
            links.new(previous_position_field.outputs["Attribute"], raycast.inputs["Source Position"])
            links.new(normalized_direction.outputs["Vector"], raycast.inputs["Ray Direction"])
            links.new(ray_length.outputs[0], raycast.inputs["Ray Length"])

            # Also catch particles already resting within their radius of the surface.
            proximity = nodes.new("GeometryNodeProximity")
            proximity.target_element = 'FACES'
            proximity.label = "CLOSE SURFACE FALLBACK"
            proximity.location = (1720, 1200)
            links.new(collider_info.outputs["Geometry"], proximity.inputs["Geometry"])
            links.new(current_position.outputs["Position"], proximity.inputs["Sample Position"])

            close_enough = nodes.new("ShaderNodeMath")
            close_enough.operation = 'LESS_THAN'
            close_enough.location = (1940, 1200)
            links.new(proximity.outputs["Distance"], close_enough.inputs[0])
            close_enough.inputs[1].default_value = object_particle_radius

            hit_or_close = nodes.new("FunctionNodeBooleanMath")
            hit_or_close.operation = 'OR'
            hit_or_close.location = (2150, 1000)
            links.new(raycast.outputs["Is Hit"], hit_or_close.inputs[0])
            links.new(close_enough.outputs[0], hit_or_close.inputs[1])

            # Prefer raycast hit data; proximity is only a stationary-contact fallback.
            contact_position = nodes.new("GeometryNodeSwitch")
            contact_position.input_type = 'VECTOR'
            contact_position.label = "CONTACT POSITION"
            contact_position.location = (2150, 800)
            links.new(raycast.outputs["Is Hit"], contact_position.inputs["Switch"])
            links.new(proximity.outputs["Position"], contact_position.inputs["False"])
            links.new(raycast.outputs["Hit Position"], contact_position.inputs["True"])

            contact_normal = nodes.new("GeometryNodeSwitch")
            contact_normal.input_type = 'VECTOR'
            contact_normal.label = "CONTACT NORMAL"
            contact_normal.location = (2150, 600)
            links.new(raycast.outputs["Is Hit"], contact_normal.inputs["Switch"])
            # Fallback normal sampled from the collider mesh.
            collider_normal = nodes.new("GeometryNodeInputNormal")
            collider_normal.location = (1720, 500)
            sample_normal = nodes.new("GeometryNodeSampleNearestSurface")
            sample_normal.data_type = 'FLOAT_VECTOR'
            sample_normal.location = (1940, 500)
            links.new(collider_info.outputs["Geometry"], sample_normal.inputs["Mesh"])
            links.new(collider_normal.outputs["Normal"], sample_normal.inputs["Value"])
            links.new(current_position.outputs["Position"], sample_normal.inputs["Sample Position"])
            links.new(sample_normal.outputs["Value"], contact_normal.inputs["False"])
            links.new(raycast.outputs["Hit Normal"], contact_normal.inputs["True"])

            radius_offset = nodes.new("ShaderNodeVectorMath")
            radius_offset.operation = 'SCALE'
            radius_offset.location = (2370, 800)
            links.new(contact_normal.outputs["Output"], radius_offset.inputs[0])
            radius_offset.inputs["Scale"].default_value = object_particle_radius

            resolved_position = nodes.new("ShaderNodeVectorMath")
            resolved_position.operation = 'ADD'
            resolved_position.location = (2580, 800)
            links.new(contact_position.outputs["Output"], resolved_position.inputs[0])
            links.new(radius_offset.outputs["Vector"], resolved_position.inputs[1])

            choose_position = nodes.new("GeometryNodeSwitch")
            choose_position.input_type = 'VECTOR'
            choose_position.label = "APPLY COLLISION POSITION"
            choose_position.location = (2790, 800)
            links.new(current_position.outputs["Position"], choose_position.inputs["False"])
            links.new(resolved_position.outputs["Vector"], choose_position.inputs["True"])

            set_object_collision_position = nodes.new("GeometryNodeSetPosition")
            set_object_collision_position.label = "RESOLVE OBJECT COLLISION"
            set_object_collision_position.location = (3000, 100)
            links.new(final_geometry, set_object_collision_position.inputs["Geometry"])
            links.new(choose_position.outputs["Output"], set_object_collision_position.inputs["Position"])

            # Reflect the velocity only when it points into the contact surface.
            dot_velocity_normal = nodes.new("ShaderNodeVectorMath")
            dot_velocity_normal.operation = 'DOT_PRODUCT'
            dot_velocity_normal.location = (2370, 350)
            links.new(velocity_field.outputs["Attribute"], dot_velocity_normal.inputs[0])
            links.new(contact_normal.outputs["Output"], dot_velocity_normal.inputs[1])

            approaching = nodes.new("ShaderNodeMath")
            approaching.operation = 'LESS_THAN'
            approaching.location = (2580, 350)
            approaching.inputs[1].default_value = 0.0
            links.new(dot_velocity_normal.outputs["Value"], approaching.inputs[0])

            # A contact is active only while the particle is moving into
            # the surface. Do not keep snapping particles back to the surface
            # after they have already bounced and are moving away.
            active_collision = nodes.new("FunctionNodeBooleanMath")
            active_collision.operation = 'AND'
            active_collision.label = "CONTACT AND APPROACHING"
            active_collision.location = (2790, 520)
            links.new(hit_or_close.outputs["Boolean"], active_collision.inputs[0])
            links.new(approaching.outputs[0], active_collision.inputs[1])

            # DEL / After Collision: delete directly from the same contact +
            # approaching signal that activates Object Collision response.
            # This avoids a delayed Named Attribute field evaluation.
            if delete_enabled and delete_mode == "AFTER_COLLISION":
                delete_object_hit = nodes.new("GeometryNodeDeleteGeometry")
                delete_object_hit.label = "DEL / OBJECT HIT — IMMEDIATE"
                delete_object_hit.location = (2890, 100)
                delete_object_hit.domain = 'POINT'
                links.new(final_geometry, delete_object_hit.inputs["Geometry"])
                links.new(active_collision.outputs["Boolean"], delete_object_hit.inputs["Selection"])
                final_geometry = delete_object_hit.outputs["Geometry"]

                # Set Position was created earlier in the compiler and already
                # had the pre-delete geometry connected. Rewire its Geometry
                # input explicitly so deleted points cannot re-enter the chain.
                for old_link in list(set_object_collision_position.inputs["Geometry"].links):
                    links.remove(old_link)
                links.new(final_geometry, set_object_collision_position.inputs["Geometry"])

            links.new(active_collision.outputs["Boolean"], choose_position.inputs["Switch"])


            # Split velocity into normal and tangential components.
            # Restitution affects only normal velocity; friction affects only
            # tangential velocity.
            normal_component = nodes.new("ShaderNodeVectorMath")
            normal_component.operation = 'SCALE'
            normal_component.label = "NORMAL VELOCITY"
            normal_component.location = (2580, 100)
            links.new(contact_normal.outputs["Output"], normal_component.inputs[0])
            links.new(dot_velocity_normal.outputs["Value"], normal_component.inputs["Scale"])

            tangent_component = nodes.new("ShaderNodeVectorMath")
            tangent_component.operation = 'SUBTRACT'
            tangent_component.label = "TANGENTIAL VELOCITY"
            tangent_component.location = (2790, 80)
            links.new(velocity_field.outputs["Attribute"], tangent_component.inputs[0])
            links.new(normal_component.outputs["Vector"], tangent_component.inputs[1])

            damp_tangent = nodes.new("ShaderNodeVectorMath")
            damp_tangent.operation = 'SCALE'
            damp_tangent.label = "TANGENTIAL FRICTION"
            damp_tangent.location = (3000, 50)
            links.new(tangent_component.outputs["Vector"], damp_tangent.inputs[0])
            friction_factor = nodes.new("ShaderNodeMath")
            friction_factor.operation = 'SUBTRACT'
            friction_factor.location = (2790, -180)
            friction_factor.inputs[0].default_value = 1.0
            friction_factor.inputs[1].default_value = object_collision_friction
            links.new(friction_factor.outputs[0], damp_tangent.inputs["Scale"])

            bounced_normal = nodes.new("ShaderNodeVectorMath")
            bounced_normal.operation = 'SCALE'
            bounced_normal.label = "NORMAL RESTITUTION"
            bounced_normal.location = (3000, -180)
            links.new(normal_component.outputs["Vector"], bounced_normal.inputs[0])
            negative_restitution = nodes.new("ShaderNodeMath")
            negative_restitution.operation = 'MULTIPLY'
            negative_restitution.location = (2790, -400)
            negative_restitution.inputs[0].default_value = -1.0
            negative_restitution.inputs[1].default_value = object_collision_bounce
            links.new(negative_restitution.outputs[0], bounced_normal.inputs["Scale"])

            combined_velocity = nodes.new("ShaderNodeVectorMath")
            combined_velocity.operation = 'ADD'
            combined_velocity.label = "BOUNCE + FRICTION"
            combined_velocity.location = (3210, 100)
            links.new(damp_tangent.outputs["Vector"], combined_velocity.inputs[0])
            links.new(bounced_normal.outputs["Vector"], combined_velocity.inputs[1])

            # Add deterministic per-particle random deflection along the
            # collision plane. The normal bounce remains controlled by
            # Restitution; this only adds sideways scatter.
            final_object_velocity = combined_velocity.outputs["Vector"]
            if object_random_deflection > 0.0:
                random_vector = nodes.new("FunctionNodeRandomValue")
                random_vector.data_type = 'FLOAT_VECTOR'
                random_vector.label = "PER-PARTICLE RANDOM DEFLECTION"
                random_vector.location = (3210, -520)
                random_vector.inputs["Min"].default_value = (-1.0, -1.0, -1.0)
                random_vector.inputs["Max"].default_value = (1.0, 1.0, 1.0)

                particle_id = nodes.new("GeometryNodeInputID")
                particle_id.label = "PARTICLE ID"
                particle_id.location = (3000, -650)
                if random_vector.inputs.get("ID") is not None:
                    links.new(particle_id.outputs["ID"], random_vector.inputs["ID"])

                random_tangent = nodes.new("ShaderNodeVectorMath")
                random_tangent.operation = 'CROSS_PRODUCT'
                random_tangent.label = "RANDOM TANGENT DIRECTION"
                random_tangent.location = (3420, -440)
                links.new(random_vector.outputs["Value"], random_tangent.inputs[0])
                links.new(contact_normal.outputs["Output"], random_tangent.inputs[1])

                normalized_random_tangent = nodes.new("ShaderNodeVectorMath")
                normalized_random_tangent.operation = 'NORMALIZE'
                normalized_random_tangent.label = "NORMALIZE RANDOM TANGENT"
                normalized_random_tangent.location = (3630, -440)
                links.new(random_tangent.outputs["Vector"], normalized_random_tangent.inputs[0])

                impact_speed = nodes.new("ShaderNodeMath")
                impact_speed.operation = 'ABSOLUTE'
                impact_speed.label = "NORMAL IMPACT SPEED"
                impact_speed.location = (3210, -760)
                links.new(dot_velocity_normal.outputs["Value"], impact_speed.inputs[0])

                deflection_strength = nodes.new("ShaderNodeMath")
                deflection_strength.operation = 'MULTIPLY'
                deflection_strength.label = "RANDOM DEFLECTION STRENGTH"
                deflection_strength.location = (3420, -760)
                deflection_strength.inputs[1].default_value = object_random_deflection
                links.new(impact_speed.outputs[0], deflection_strength.inputs[0])

                random_impulse = nodes.new("ShaderNodeVectorMath")
                random_impulse.operation = 'SCALE'
                random_impulse.label = "RANDOM SIDEWAYS IMPULSE"
                random_impulse.location = (3840, -440)
                links.new(normalized_random_tangent.outputs["Vector"], random_impulse.inputs[0])
                links.new(deflection_strength.outputs[0], random_impulse.inputs["Scale"])

                add_random_deflection = nodes.new("ShaderNodeVectorMath")
                add_random_deflection.operation = 'ADD'
                add_random_deflection.label = "ADD RANDOM DEFLECTION"
                add_random_deflection.location = (4050, 100)
                links.new(combined_velocity.outputs["Vector"], add_random_deflection.inputs[0])
                links.new(random_impulse.outputs["Vector"], add_random_deflection.inputs[1])
                final_object_velocity = add_random_deflection.outputs["Vector"]

            choose_object_velocity = nodes.new("GeometryNodeSwitch")
            choose_object_velocity.input_type = 'VECTOR'
            choose_object_velocity.label = "OBJECT COLLISION RESPONSE"
            choose_object_velocity.location = (3420, 360)
            links.new(active_collision.outputs["Boolean"], choose_object_velocity.inputs["Switch"])
            links.new(velocity_field.outputs["Attribute"], choose_object_velocity.inputs["False"])
            links.new(final_object_velocity, choose_object_velocity.inputs["True"])

            choose_hit_velocity = nodes.new("GeometryNodeSwitch")
            choose_hit_velocity.input_type = 'VECTOR'
            choose_hit_velocity.location = (3210, 360)
            links.new(active_collision.outputs["Boolean"], choose_hit_velocity.inputs["Switch"])
            links.new(velocity_field.outputs["Attribute"], choose_hit_velocity.inputs["False"])
            links.new(choose_object_velocity.outputs["Output"], choose_hit_velocity.inputs["True"])

            store_object_velocity = nodes.new("GeometryNodeStoreNamedAttribute")
            store_object_velocity.label = "STORE OBJECT COLLISION VELOCITY"
            store_object_velocity.location = (3420, 100)
            store_object_velocity.domain = 'POINT'
            store_object_velocity.data_type = 'FLOAT_VECTOR'
            store_object_velocity.inputs["Name"].default_value = "bflow_velocity"
            links.new(set_object_collision_position.outputs["Geometry"], store_object_velocity.inputs["Geometry"])
            links.new(choose_hit_velocity.outputs["Output"], store_object_velocity.inputs["Value"])

            final_geometry = store_object_velocity.outputs["Geometry"]
        
        # Append this frame's Birth points after the existing particles have
        # completed their integration and collision response. This prevents a
        # particle born on frame N from moving during frame N itself.
        accumulate_births = nodes.new("GeometryNodeJoinGeometry")
        accumulate_births.label = "APPEND NEW BIRTHS AFTER SIMULATION"
        accumulate_births.location = (3650, 100)
        links.new(final_geometry, accumulate_births.inputs["Geometry"])
        links.new(store_birth_frame.outputs["Geometry"], accumulate_births.inputs["Geometry"])

        simulation_geometry = accumulate_births.outputs["Geometry"]

        if delete_enabled and delete_mode != "AFTER_COLLISION":
            # Time-based DEL is evaluated on the completed frame geometry.
            # After Collision is handled directly inside Ground/Object Collision
            # branches using their own exact hit selections.
            if delete_mode == "REAL_TIME":
                if delete_time_unit == "SECONDS":
                    current_delete_time = scene_time.outputs["Seconds"]
                else:
                    current_delete_time = scene_time.outputs["Frame"]
            else:
                read_delete_birth = nodes.new("GeometryNodeInputNamedAttribute")
                read_delete_birth.data_type = 'FLOAT'
                read_delete_birth.label = "READ PARTICLE BIRTH FRAME"
                read_delete_birth.location = (4100, -900)
                read_delete_birth.inputs["Name"].default_value = "bflow_birth_frame"

                particle_age_frames = nodes.new("ShaderNodeMath")
                particle_age_frames.operation = 'SUBTRACT'
                particle_age_frames.label = "PARTICLE AGE (FRAMES)"
                particle_age_frames.location = (4310, -900)
                links.new(scene_time.outputs["Frame"], particle_age_frames.inputs[0])
                links.new(read_delete_birth.outputs["Attribute"], particle_age_frames.inputs[1])
                current_delete_time = particle_age_frames.outputs[0]

                if delete_time_unit == "SECONDS":
                    fps = max(
                        float(context.scene.render.fps) /
                        max(float(context.scene.render.fps_base), 0.0001),
                        0.0001,
                    )
                    age_seconds = nodes.new("ShaderNodeMath")
                    age_seconds.operation = 'DIVIDE'
                    age_seconds.label = "PARTICLE AGE (SECONDS)"
                    age_seconds.location = (4520, -900)
                    age_seconds.inputs[1].default_value = fps
                    links.new(current_delete_time, age_seconds.inputs[0])
                    current_delete_time = age_seconds.outputs[0]

            below_delete_time = nodes.new("ShaderNodeMath")
            below_delete_time.operation = 'LESS_THAN'
            below_delete_time.label = "TIME BEFORE DELETE THRESHOLD"
            below_delete_time.location = (4730, -700)
            links.new(current_delete_time, below_delete_time.inputs[0])
            below_delete_time.inputs[1].default_value = delete_after

            reached_delete_time = nodes.new("FunctionNodeBooleanMath")
            reached_delete_time.operation = 'NOT'
            reached_delete_time.label = "DELETE WHEN TIME REACHED"
            reached_delete_time.location = (4940, -700)
            links.new(below_delete_time.outputs[0], reached_delete_time.inputs[0])

            delete_geometry = nodes.new("GeometryNodeDeleteGeometry")
            delete_geometry.label = "B-FLOW / DEL — DELETE PARTICLES"
            delete_geometry.location = (5150, 100)
            delete_geometry.domain = 'POINT'
            links.new(simulation_geometry, delete_geometry.inputs["Geometry"])
            links.new(reached_delete_time.outputs["Boolean"], delete_geometry.inputs["Selection"])
            simulation_geometry = delete_geometry.outputs["Geometry"]

        links.new(simulation_geometry, sim_out.inputs["Geometry"])

        # SIZE affects display instances only. Simulation points and all
        # collision radii remain unchanged.
        size_factor_socket = None

        if not size_enabled or size_mode == "FIXED":
            fixed_factor = size_fixed if size_enabled else 1.0
            size_constant = nodes.new("ShaderNodeMath")
            size_constant.operation = 'MULTIPLY'
            size_constant.label = "B-FLOW / FIXED SIZE"
            size_constant.location = (930, -760)
            size_constant.inputs[0].default_value = fixed_factor
            size_constant.inputs[1].default_value = 1.0
            size_factor_socket = size_constant.outputs[0]

        else:
            size_clock = nodes.new("GeometryNodeInputSceneTime")
            size_clock.label = "B-FLOW / SIZE CLOCK"
            size_clock.location = (900, -980)

            if size_mode == "REAL_TIME":
                # Global timeline elapsed since the scene's playback start.
                elapsed_frames = nodes.new("ShaderNodeMath")
                elapsed_frames.operation = 'SUBTRACT'
                elapsed_frames.label = "REAL TIME ELAPSED"
                elapsed_frames.location = (1110, -980)
                elapsed_frames.inputs[1].default_value = float(scene.frame_start)
                links.new(size_clock.outputs["Frame"], elapsed_frames.inputs[0])
                elapsed_socket = elapsed_frames.outputs[0]

            else:
                # Each particle uses the immutable frame stored at Birth.
                particle_birth = nodes.new("GeometryNodeInputNamedAttribute")
                particle_birth.data_type = 'FLOAT'
                particle_birth.label = "PARTICLE BIRTH TIME"
                particle_birth.location = (1110, -1190)
                particle_birth.inputs["Name"].default_value = "bflow_birth_frame"

                elapsed_frames = nodes.new("ShaderNodeMath")
                elapsed_frames.operation = 'SUBTRACT'
                elapsed_frames.label = "PARTICLE AGE (FRAMES)"
                elapsed_frames.location = (1320, -980)
                links.new(size_clock.outputs["Frame"], elapsed_frames.inputs[0])
                links.new(particle_birth.outputs["Attribute"], elapsed_frames.inputs[1])
                elapsed_socket = elapsed_frames.outputs[0]

            if size_time_unit == "SECONDS":
                fps_value = max(
                    float(scene.render.fps) / max(float(scene.render.fps_base), 0.0001),
                    0.0001,
                )
                elapsed_seconds = nodes.new("ShaderNodeMath")
                elapsed_seconds.operation = 'DIVIDE'
                elapsed_seconds.label = "SIZE TIME IN SECONDS"
                elapsed_seconds.location = (1530, -980)
                elapsed_seconds.inputs[1].default_value = fps_value
                links.new(elapsed_socket, elapsed_seconds.inputs[0])
                elapsed_socket = elapsed_seconds.outputs[0]

            size_time_offset = nodes.new("ShaderNodeMath")
            size_time_offset.operation = 'SUBTRACT'
            size_time_offset.label = "SIZE TIME AFTER START"
            size_time_offset.location = (1740, -1160)
            size_time_offset.inputs[1].default_value = float(size_start_time)
            links.new(elapsed_socket, size_time_offset.inputs[0])

            size_time_range = nodes.new("ShaderNodeMath")
            size_time_range.operation = 'SUBTRACT'
            size_time_range.label = "SIZE TIME RANGE"
            size_time_range.location = (1740, -1320)
            size_time_range.inputs[0].default_value = float(size_end_time)
            size_time_range.inputs[1].default_value = float(size_start_time)

            progress = nodes.new("ShaderNodeMath")
            progress.operation = 'DIVIDE'
            progress.label = "SIZE ANIMATION PROGRESS"
            progress.location = (1950, -980)
            progress.inputs[1].default_value = max(float(size_end_time) - float(size_start_time), 0.001)
            links.new(size_time_offset.outputs[0], progress.inputs[0])

            clamp_min = nodes.new("ShaderNodeMath")
            clamp_min.operation = 'MAXIMUM'
            clamp_min.label = "CLAMP SIZE PROGRESS MIN"
            clamp_min.location = (1950, -980)
            clamp_min.inputs[1].default_value = 0.0
            links.new(progress.outputs[0], clamp_min.inputs[0])

            clamp_max = nodes.new("ShaderNodeMath")
            clamp_max.operation = 'MINIMUM'
            clamp_max.label = "CLAMP SIZE PROGRESS MAX"
            clamp_max.location = (2160, -980)
            clamp_max.inputs[1].default_value = 1.0
            links.new(clamp_min.outputs[0], clamp_max.inputs[0])

            size_delta = nodes.new("ShaderNodeMath")
            size_delta.operation = 'SUBTRACT'
            size_delta.label = "SIZE RANGE"
            size_delta.location = (1740, -1190)
            size_delta.inputs[0].default_value = size_end
            size_delta.inputs[1].default_value = size_start

            size_lerp = nodes.new("ShaderNodeMath")
            size_lerp.operation = 'MULTIPLY'
            size_lerp.label = "INTERPOLATE SIZE"
            size_lerp.location = (2370, -980)
            links.new(clamp_max.outputs[0], size_lerp.inputs[0])
            links.new(size_delta.outputs[0], size_lerp.inputs[1])

            size_result = nodes.new("ShaderNodeMath")
            size_result.operation = 'ADD'
            size_result.label = "FINAL PARTICLE SIZE"
            size_result.location = (2580, -980)
            size_result.inputs[1].default_value = size_start
            links.new(size_lerp.outputs[0], size_result.inputs[0])
            size_factor_socket = size_result.outputs[0]

        # OPACITY is stored on display points. The shader's Attribute node
        # reads this through the Instancer domain, allowing each particle
        # instance to fade independently without changing physics/collision.
        display_geometry = sim_out.outputs["Geometry"]
        if opacity_enabled:
            if opacity_mode == "FIXED":
                opacity_value = nodes.new("ShaderNodeMath")
                opacity_value.operation = 'MULTIPLY'
                opacity_value.label = "B-FLOW / FIXED OPACITY"
                opacity_value.location = (900, -1460)
                opacity_value.inputs[0].default_value = opacity_fixed
                opacity_value.inputs[1].default_value = 1.0
                opacity_field_socket = opacity_value.outputs[0]
            else:
                opacity_clock = nodes.new("GeometryNodeInputSceneTime")
                opacity_clock.label = "B-FLOW / OPACITY CLOCK"
                opacity_clock.location = (900, -1660)

                if opacity_mode == "REAL_TIME":
                    # Real Time uses absolute scene frame numbers.
                    opacity_elapsed_socket = opacity_clock.outputs["Frame"]
                else:
                    # Particle Time uses each particle's age since Birth.
                    opacity_birth = nodes.new("GeometryNodeInputNamedAttribute")
                    opacity_birth.data_type = 'FLOAT'
                    opacity_birth.label = "PARTICLE BIRTH FRAME"
                    opacity_birth.location = (1110, -1870)
                    opacity_birth.inputs["Name"].default_value = "bflow_birth_frame"

                    opacity_age = nodes.new("ShaderNodeMath")
                    opacity_age.operation = 'SUBTRACT'
                    opacity_age.label = "PARTICLE AGE (FRAMES)"
                    opacity_age.location = (1320, -1660)
                    links.new(opacity_clock.outputs["Frame"], opacity_age.inputs[0])
                    links.new(opacity_birth.outputs["Attribute"], opacity_age.inputs[1])
                    opacity_elapsed_socket = opacity_age.outputs[0]

                opacity_since_start = nodes.new("ShaderNodeMath")
                opacity_since_start.operation = 'SUBTRACT'
                opacity_since_start.label = "FRAMES SINCE OPACITY START"
                opacity_since_start.location = (1530, -1660)
                opacity_since_start.inputs[1].default_value = float(opacity_start_frame)
                links.new(opacity_elapsed_socket, opacity_since_start.inputs[0])

                opacity_frame_range = nodes.new("ShaderNodeMath")
                opacity_frame_range.operation = 'SUBTRACT'
                opacity_frame_range.label = "OPACITY FRAME RANGE"
                opacity_frame_range.location = (1530, -1870)
                opacity_frame_range.inputs[0].default_value = float(opacity_end_frame)
                opacity_frame_range.inputs[1].default_value = float(opacity_start_frame)

                opacity_progress = nodes.new("ShaderNodeMath")
                opacity_progress.operation = 'DIVIDE'
                opacity_progress.label = "OPACITY FADE PROGRESS"
                opacity_progress.location = (1740, -1660)
                links.new(opacity_since_start.outputs[0], opacity_progress.inputs[0])
                links.new(opacity_frame_range.outputs[0], opacity_progress.inputs[1])

                opacity_min = nodes.new("ShaderNodeMath")
                opacity_min.operation = 'MAXIMUM'
                opacity_min.label = "CLAMP OPACITY MIN"
                opacity_min.location = (1950, -1660)
                opacity_min.inputs[1].default_value = 0.0
                links.new(opacity_progress.outputs[0], opacity_min.inputs[0])

                opacity_max = nodes.new("ShaderNodeMath")
                opacity_max.operation = 'MINIMUM'
                opacity_max.label = "CLAMP OPACITY MAX"
                opacity_max.location = (2160, -1660)
                opacity_max.inputs[1].default_value = 1.0
                links.new(opacity_min.outputs[0], opacity_max.inputs[0])

                opacity_delta = nodes.new("ShaderNodeMath")
                opacity_delta.operation = 'SUBTRACT'
                opacity_delta.label = "OPACITY RANGE"
                opacity_delta.location = (1740, -1870)
                opacity_delta.inputs[0].default_value = opacity_end
                opacity_delta.inputs[1].default_value = opacity_start

                opacity_scale = nodes.new("ShaderNodeMath")
                opacity_scale.operation = 'MULTIPLY'
                opacity_scale.label = "INTERPOLATE OPACITY"
                opacity_scale.location = (2370, -1660)
                links.new(opacity_max.outputs[0], opacity_scale.inputs[0])
                links.new(opacity_delta.outputs[0], opacity_scale.inputs[1])

                opacity_result = nodes.new("ShaderNodeMath")
                opacity_result.operation = 'ADD'
                opacity_result.label = "FINAL OPACITY"
                opacity_result.location = (2580, -1660)
                opacity_result.inputs[1].default_value = opacity_start
                links.new(opacity_scale.outputs[0], opacity_result.inputs[0])
                opacity_field_socket = opacity_result.outputs[0]

            store_opacity = nodes.new("GeometryNodeStoreNamedAttribute")
            store_opacity.label = "B-FLOW / STORE PARTICLE OPACITY"
            store_opacity.location = (2800, 100)
            store_opacity.domain = 'POINT'
            store_opacity.data_type = 'FLOAT'
            store_opacity.inputs["Name"].default_value = "bflow_opacity"
            links.new(display_geometry, store_opacity.inputs["Geometry"])
            links.new(opacity_field_socket, store_opacity.inputs["Value"])
            display_geometry = store_opacity.outputs["Geometry"]

        # Preserve Shape Instance's own scale and multiply it by the Size
        # operator's per-particle display factor.
        display_base_scale = shape_instance_scale if shape_instance_object is not None else 1.0
        display_scale = nodes.new("ShaderNodeMath")
        display_scale.operation = 'MULTIPLY'
        display_scale.label = "B-FLOW / DISPLAY SCALE"
        display_scale.location = (2790, -760)
        display_scale.inputs[1].default_value = display_base_scale
        links.new(size_factor_socket, display_scale.inputs[0])

        display_scale_vector = nodes.new("ShaderNodeCombineXYZ")
        display_scale_vector.label = "UNIFORM PARTICLE SCALE"
        display_scale_vector.location = (3000, -760)
        links.new(display_scale.outputs[0], display_scale_vector.inputs["X"])
        links.new(display_scale.outputs[0], display_scale_vector.inputs["Y"])
        links.new(display_scale.outputs[0], display_scale_vector.inputs["Z"])

        if shape_instance_object is not None:
            shape_collection = nodes.new("GeometryNodeGeometryToInstance")
            shape_collection.label = "B-FLOW / SHAPE OBJECT LIBRARY"
            shape_collection.location = (700, -260)

            for shape_index, display_shape in enumerate(display_shape_objects or [display_shape_object]):
                shape_info = nodes.new("GeometryNodeObjectInfo")
                shape_info.label = f"SHAPE OBJECT {shape_index + 1}"
                shape_info.location = (430, -260 - shape_index * 220)
                # Keep source object local geometry; ignore its scene placement.
                shape_info.transform_space = 'ORIGINAL'
                shape_info.inputs["Object"].default_value = display_shape
                links.new(shape_info.outputs["Geometry"], shape_collection.inputs["Geometry"])

            instance = nodes.new("GeometryNodeInstanceOnPoints")
            instance.label = "WEIGHTED CUSTOM SHAPE INSTANCES"
            instance.location = (1160, 100)
            links.new(display_geometry, instance.inputs["Points"])
            links.new(shape_collection.outputs["Instances"], instance.inputs["Instance"])

            if len(shape_instance_choices) > 1:
                instance.inputs["Pick Instance"].default_value = True
                shape_index_attribute = nodes.new("GeometryNodeInputNamedAttribute")
                shape_index_attribute.data_type = 'INT'
                shape_index_attribute.label = "PARTICLE SHAPE INDEX"
                shape_index_attribute.location = (940, -700)
                shape_index_attribute.inputs["Name"].default_value = "bflow_shape_index"
                links.new(shape_index_attribute.outputs["Attribute"], instance.inputs["Instance Index"])

            if rotation_enabled or spin_enabled:
                display_rotation = nodes.new("GeometryNodeInputNamedAttribute")
                display_rotation.data_type = 'FLOAT_VECTOR'
                display_rotation.location = (940, -520)
                display_rotation.inputs["Name"].default_value = "bflow_rotation"
                links.new(display_rotation.outputs["Attribute"], instance.inputs["Rotation"])
            links.new(display_scale_vector.outputs["Vector"], instance.inputs["Scale"])
        else:
            ico = nodes.new("GeometryNodeMeshIcoSphere")
            ico.location = (940, -260)
            ico.inputs["Radius"].default_value = particle_radius
            if " subdivisions" in ico.inputs:
                ico.inputs[" subdivisions"].default_value = 1
            elif "Subdivisions" in ico.inputs:
                ico.inputs["Subdivisions"].default_value = 1

            instance = nodes.new("GeometryNodeInstanceOnPoints")
            instance.location = (1160, 100)
            links.new(display_geometry, instance.inputs["Points"])
            if particle_display_material is not None:
                set_ico_material = nodes.new("GeometryNodeSetMaterial")
                set_ico_material.label = "B-FLOW / PARTICLE DISPLAY MATERIAL"
                set_ico_material.location = (1080, -280)
                set_ico_material.inputs["Material"].default_value = particle_display_material
                links.new(ico.outputs["Mesh"], set_ico_material.inputs["Geometry"])
                links.new(set_ico_material.outputs["Geometry"], instance.inputs["Instance"])
            else:
                links.new(ico.outputs["Mesh"], instance.inputs["Instance"])
            if rotation_enabled or spin_enabled:
                display_rotation = nodes.new("GeometryNodeInputNamedAttribute")
                display_rotation.data_type = 'FLOAT_VECTOR'
                display_rotation.location = (940, -520)
                display_rotation.inputs["Name"].default_value = "bflow_rotation"
                links.new(display_rotation.outputs["Attribute"], instance.inputs["Rotation"])
            links.new(display_scale_vector.outputs["Vector"], instance.inputs["Scale"])

        output = nodes.new("NodeGroupOutput")
        output.location = (1400, 100)
        links.new(instance.outputs["Instances"], output.inputs["Geometry"])

        group["bflow_version"] = "1.52"
        group["bflow_engine"] = "Event Stack -> surface emission -> Simulation Zone -> velocity/gravity + cached ground-hit bounce"
        group["bflow_birth_count"] = birth_count
        group["bflow_birth_start"] = birth_start
        group["bflow_birth_stop"] = birth_stop
        group["bflow_speed"] = speed_value
        group["bflow_gravity"] = gravity
        group["bflow_keep_dist_enabled"] = keep_dist_enabled
        group["bflow_keep_distance"] = keep_distance
        group["bflow_keep_dist_strength"] = keep_dist_strength
        group["bflow_object_random_deflection"] = object_random_deflection
        group["bflow_size_enabled"] = size_enabled
        group["bflow_size_mode"] = size_mode
        group["bflow_size_fixed"] = size_fixed
        group["bflow_size_start"] = size_start
        group["bflow_size_end"] = size_end
        group["bflow_size_start_time"] = size_start_time
        group["bflow_size_end_time"] = size_end_time
        group["bflow_size_time_unit"] = size_time_unit
        group["bflow_opacity_enabled"] = opacity_enabled
        group["bflow_rand_color_enabled"] = rand_color_enabled
        group["bflow_rand_color_palette_size"] = len(rand_color_palette)
        group["bflow_opacity_mode"] = opacity_mode
        group["bflow_opacity_fixed"] = opacity_fixed
        group["bflow_opacity_start"] = opacity_start
        group["bflow_opacity_end"] = opacity_end
        group["bflow_opacity_start_frame"] = opacity_start_frame
        group["bflow_opacity_end_frame"] = opacity_end_frame

        modifier = next((m for m in obj.modifiers if m.type == 'NODES'), None)
        if modifier is None:
            modifier = obj.modifiers.new("B-Flow Simulation", 'NODES')
        modifier.node_group = group

        # Simulation Zones only evaluate from the scene playback start.
        # If Birth is scheduled on frame 0 while the scene starts at frame 1,
        # the emission gate can never be evaluated at frame 0. Extend the
        # scene range backward so frame-zero births are actually simulated.
        if birth_start < scene.frame_start:
            scene.frame_start = birth_start

        # Force the Geometry Nodes Simulation Zone to discard stale state.
        # Merely setting the current frame to frame_start is not sufficient
        # when the timeline is already parked there (for example, after a
        # previous live Speed edit). Scrub one frame before the simulation
        # start, update the depsgraph, then return to the start frame.
        simulation_start = int(scene.frame_start)
        try:
            scene.frame_set(simulation_start - 1)
            context.view_layer.update()
        except Exception as exc:
            print("[B-Flow] Could not perform simulation pre-roll reset:", exc)

        scene.frame_set(simulation_start)
        context.view_layer.update()

        for selected_obj in context.selected_objects:
            selected_obj.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj

        self.report(
            {'INFO'},
            f"Compiled {birth_count} particles | Collision: " +
            ("Object + Ground" if collision_enabled and collision_object else
             "Ground" if collision_enabled else "OFF")
        )
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI panels
# ---------------------------------------------------------------------------

class BFLOW_PT_stack_sidebar(Panel):
    bl_label = "B-Flow"
    bl_idname = "BFLOW_PT_stack_sidebar"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "B-Flow"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space and space.type == 'NODE_EDITOR' and getattr(space, "edit_tree", None) and space.edit_tree.bl_idname == "BFLOW_Tree"

    def draw(self, context):
        layout = self.layout
        tree, node = active_event(context)
        layout.label(text="EVENT STACK EDITOR", icon='PARTICLES')
        layout.operator("bflow.compile_simulation", text="Compile / Update Simulation", icon='PLAY')
        row = layout.row(align=True)
        row.operator("bflow.add_event", icon='NODE')
        if tree:
            row.operator("bflow.migrate_legacy", text="", icon='FILE_REFRESH')
        if not node:
            layout.separator()
            layout.label(text="Select an Event node", icon='INFO')
            return

        layout.separator()
        layout.label(text=node.name, icon='NODE')
        layout.prop(node, "event_name", text="Event")
        layout.label(text="Operator Stack")
        for index, item in enumerate(node.operator_items):
            box = layout.box()
            row = box.row(align=True)
            row.label(text="", icon=operator_icon_for_item(item))
            select = row.operator(
                "bflow.select_operator",
                text=operator_label(item.operator_type),
                icon='NONE',
                depress=(index == node.active_operator),
            )
            select.tree_name, select.node_name, select.index = tree.name, node.name, index
            row.prop(item, "enabled", text="")
            up = row.operator("bflow.move_operator", text="", icon='TRIA_UP')
            up.tree_name, up.node_name, up.index, up.direction = tree.name, node.name, index, "UP"
            down = row.operator("bflow.move_operator", text="", icon='TRIA_DOWN')
            down.tree_name, down.node_name, down.index, down.direction = tree.name, node.name, index, "DOWN"
            rem = row.operator("bflow.remove_operator", text="", icon='X')
            rem.tree_name, rem.node_name, rem.index = tree.name, node.name, index

        row = layout.row()
        add = row.operator("bflow.add_operator", text="Add Operator", icon='ADD')
        add.tree_name, add.node_name = tree.name, node.name

        if node.operator_items:
            index = min(node.active_operator, len(node.operator_items)-1)
            item = node.operator_items[index]
            layout.separator()
            layout.label(text="Selected Operator Parameters", icon='PREFERENCES')
            layout.prop(item, "enabled")
            t = item.operator_type
            if t == "BIRTH":
                layout.prop(item, "birth_start", text="Start")
                layout.prop(item, "birth_stop", text="Stop")
                layout.prop(item, "count", text="Total Count")
                if item.birth_stop < item.birth_start:
                    layout.label(text="Stop must be >= Start", icon='ERROR')
            elif t == "BIRTH_RATE":
                layout.prop(item, "rate")
                layout.prop(item, "count", text="Particles per emission")
            elif t in {"POSITION_OBJECT", "POSITION_VOLUME"}:
                layout.prop(item, "source_object")
            elif t == "POSITION_RANDOM":
                layout.prop(item, "random_amount")
            elif t in {"SPEED", "SPEED_RANDOM"}:
                layout.prop(item, "speed")
                layout.prop(item, "speed_mode")
                if item.speed_mode == "CUSTOM":
                    layout.prop(item, "direction")
                layout.prop(item, "speed_randomness")
                if t == "SPEED_RANDOM":
                    layout.prop(item, "random_amount")
            elif t == "GRAVITY":
                layout.prop(item, "gravity")
            elif t == "FORCE":
                layout.label(text="Blender Native Force Field Objects", icon='FORCE_FORCE')
                for field_index, force_entry in enumerate(item.force_fields):
                    force_row = layout.row(align=True)
                    force_row.prop(force_entry, "field_object", text="")
                    force_row.prop(force_entry, "influence", text="Influence")
                    remove_field = force_row.operator(
                        "bflow.remove_force_field", text="", icon='X'
                    )
                    remove_field.tree_name = tree.name
                    remove_field.node_name = node.name
                    remove_field.operator_index = index
                    remove_field.field_index = field_index
                add_field = layout.operator("bflow.add_force_field", text="Add Force Field", icon='ADD')
                add_field.tree_name = tree.name
                add_field.node_name = node.name
                add_field.operator_index = index
                layout.label(text="Supported: Force, Wind, Vortex, Turbulence, Drag", icon='INFO')
                layout.label(text="Recompile after changing field transforms/settings", icon='INFO')
            elif t == "KEEP_DIST":
                layout.label(text="Soft Particle Separation", icon='PARTICLES')
                layout.prop(item, "keep_distance", text="Keep Distance")
                layout.prop(item, "keep_dist_strength", text="Strength")
                layout.label(text="Pairwise calculation: N × N", icon='INFO')
                layout.label(text="High particle counts may slow simulation")
            elif t in {"WIND", "TURBULENCE", "VORTEX", "FOLLOW_PATH"}:
                if t == "FOLLOW_PATH":
                    layout.prop(item, "curve_object")
                else:
                    layout.prop(item, "direction")
                layout.prop(item, "strength")
            elif t == "DRAG":
                layout.prop(item, "damping")
            elif t == "ROTATION":
                layout.label(text="Initial Orientation", icon='ORIENTATION_GIMBAL')
                layout.prop(item, "rotation_offset", text="Rotation XYZ")
                layout.prop(item, "rotation_randomness")
            elif t == "SPIN":
                layout.label(text="Angular Motion", icon='DRIVER')
                layout.prop(item, "direction", text="Spin Axis")
                layout.prop(item, "strength", text="Spin Speed")
                layout.prop(item, "spin_randomness")
            elif t in {"SCALE", "SCALE_OVER_AGE"}:
                layout.prop(item, "size")
                if t == "SCALE_OVER_AGE":
                    layout.prop(item, "lifetime")
            elif t == "COLOR":
                layout.label(text="Color controls will be connected to particle attributes.")
            elif t == "MATERIAL":
                layout.prop(item, "material")
            elif t == "AGE":
                layout.prop(item, "lifetime")
            elif t == "COLLISION":
                layout.label(text="Ground Plane Collision", icon='MOD_PHYSICS')
                layout.prop(item, "collision_height", text="Ground Height")
                layout.prop(item, "bounce", text="Ground Bounce")
                layout.prop(item, "bounce_randomness", text="Ground Bounce Randomness")
                layout.prop(item, "friction", text="Ground Friction")
                layout.prop(item, "friction_randomness", text="Ground Friction Randomness")
                layout.prop(item, "particle_radius", text="Ground Particle Radius")
            elif t == "OBJECT_COLLISION":
                layout.label(text="Mesh Object Collision", icon='MOD_PHYSICS')
                layout.prop(item, "collider", text="Collider Object")
                layout.prop(item, "object_bounce", text="Object Restitution")
                layout.prop(item, "object_friction", text="Tangential Friction")
                layout.prop(item, "object_random_deflection", text="Random Deflection")
                layout.prop(item, "object_particle_radius")
            elif t == "SIZE":
                layout.label(text="Particle Display Size", icon='FULLSCREEN_ENTER')
                mode_row = layout.row(align=True)
                mode_row.prop(item, "size_mode", expand=True)

                fixed_column = layout.column()
                fixed_column.enabled = (item.size_mode == "FIXED")
                fixed_column.prop(item, "size_fixed", text="Fixed Size")

                animated_column = layout.column()
                animated_column.enabled = (item.size_mode != "FIXED")
                animated_column.separator(factor=0.35)
                unit_row = animated_column.row(align=True)
                unit_row.prop(item, "size_time_unit", expand=True)
                animated_column.prop(item, "size_start", text="Start Size")
                animated_column.prop(item, "size_end", text="End Size")
                time_row = animated_column.row(align=True)
                time_row.prop(item, "size_start_time", text="Start")
                time_row.prop(item, "size_end_time", text="End")

                if item.size_mode == "REAL_TIME":
                    animated_column.label(text="Uses scene time from the scene start frame")
                elif item.size_mode == "PARTICLE_TIME":
                    animated_column.label(text="Uses each particle's age since Birth")
            elif t == "OPACITY":
                layout.label(text="Particle Opacity / Fade", icon='IMAGE_ALPHA')
                mode_row = layout.row(align=True)
                # Use Blender's native expanded enum radio buttons. Do not
                # pass text="", because that suppresses the visible option
                # labels (Fixed / Real Time / Particle Time).
                mode_row.prop(item, "opacity_mode", expand=True)

                fixed_column = layout.column()
                fixed_column.enabled = (item.opacity_mode == "FIXED")
                fixed_column.prop(item, "opacity_fixed", text="Fixed Opacity")

                animated_column = layout.column()
                animated_column.enabled = (item.opacity_mode != "FIXED")
                animated_column.separator(factor=0.5)
                animated_column.prop(item, "opacity_start", text="Start Opacity")
                animated_column.prop(item, "opacity_end", text="End Opacity")
                frame_row = animated_column.row(align=True)
                frame_row.prop(item, "opacity_start_frame", text="Start Frame")
                frame_row.prop(item, "opacity_end_frame", text="End Frame")

                if item.opacity_mode == "REAL_TIME":
                    animated_column.label(text="Uses absolute scene frames")
                elif item.opacity_mode == "PARTICLE_TIME":
                    animated_column.label(text="Uses each particle's age in frames")

                layout.label(text="Preview in Material Preview or Rendered mode", icon='INFO')
            elif t == "RAND_COLOR":
                layout.label(text="Weighted Random Color Palette")
                palette_box = layout.box()
                palette_total = sum(float(entry.percentage) for entry in item.rand_color_entries)
                for entry_index, entry in enumerate(item.rand_color_entries):
                    color_row = palette_box.row(align=True)
                    color_row.prop(entry, "label", text="")
                    color_row.prop(entry, "color", text="")
                    color_row.prop(entry, "percentage", text="%")
                    remove_entry = color_row.operator(
                        "bflow.remove_rand_color_entry",
                        text="",
                        icon='X',
                    )
                    remove_entry.tree_name = tree.name
                    remove_entry.node_name = node.name
                    remove_entry.operator_index = index
                    remove_entry.entry_index = entry_index

                add_color_row = palette_box.row()
                add_color = add_color_row.operator(
                    "bflow.add_rand_color_entry",
                    text="Add Color",
                    icon='ADD',
                )
                add_color.tree_name = tree.name
                add_color.node_name = node.name
                add_color.operator_index = index

                palette_box.label(text="Total: 100.0%  |  Remaining percentages auto-adjust")
                if palette_total <= 0.0:
                    palette_box.label(text="Set at least one color above 0%", icon='ERROR')
                else:
                    palette_box.label(text="Each particle keeps its selected color after Birth", icon='INFO')
            elif t == "SHAPE_INSTANCE":
                # Auto-create the first object slot for existing/legacy operators
                # as well as newly added ones. The Add button is only for extras.
                _bflow_seed_shape_instance_list(item)
                layout.label(text="Weighted Random Shape Objects", icon='OUTLINER_OB_MESH')
                shape_box = layout.box()
                shape_total = sum(float(entry.percentage) for entry in item.shape_instance_entries)
                for entry_index, entry in enumerate(item.shape_instance_entries):
                    shape_row = shape_box.row(align=True)
                    shape_row.prop(entry, "object", text="")
                    shape_row.prop(entry, "percentage", text="%")
                    remove_shape = shape_row.operator(
                        "bflow.remove_shape_instance_entry",
                        text="",
                        icon='X',
                    )
                    remove_shape.tree_name = tree.name
                    remove_shape.node_name = node.name
                    remove_shape.operator_index = index
                    remove_shape.entry_index = entry_index

                add_shape = shape_box.operator(
                    "bflow.add_shape_instance_entry",
                    text="Add Shape Object",
                    icon='ADD',
                )
                add_shape.tree_name = tree.name
                add_shape.node_name = node.name
                add_shape.operator_index = index
                shape_box.label(text=f"Total: {shape_total:.1f}%  |  Relative weighted probability")
                if not any(entry.object is not None and entry.percentage > 0.0 for entry in item.shape_instance_entries):
                    shape_box.label(text="Choose at least one object with a percentage above 0%", icon='INFO')
                layout.prop(item, "shape_scale", text="Instance Scale")
                layout.label(text="Each particle keeps its selected object after Birth", icon='INFO')
            elif t in {"SPEED_TEST", "DISTANCE_TEST"}:
                layout.prop(item, "threshold")
                if t == "DISTANCE_TEST":
                    layout.prop(item, "source_object")
            elif t == "RANDOM_TEST":
                layout.prop(item, "random_amount")
            elif t == "SPAWN":
                layout.prop(item, "child_count")
                layout.prop(item, "speed")
            elif t == "RIGID_BODY":
                layout.label(text="Native Blender Rigid Body", icon='PHYSICS')
                layout.prop(item, "rb_mass", text="Mass")
                layout.prop(item, "rb_friction", text="Particle Friction")
                layout.prop(item, "rb_restitution", text="Restitution")
                layout.prop(item, "rb_collision_shape", text="Collision Shape")
                layout.prop(item, "rb_collision_margin", text="Collision Margin")
                layout.separator()
                layout.label(text="Damping")
                layout.prop(item, "rb_linear_damping", text="Linear Damping")
                layout.prop(item, "rb_angular_damping", text="Angular Damping")
                layout.separator()
                layout.prop(item, "rb_use_deactivation", text="Enable Deactivation")
                deactivation_column = layout.column()
                deactivation_column.enabled = item.rb_use_deactivation
                deactivation_column.prop(
                    item,
                    "rb_deactivate_linear_velocity",
                    text="Linear Sleep Threshold",
                )
                deactivation_column.prop(
                    item,
                    "rb_deactivate_angular_velocity",
                    text="Angular Sleep Threshold",
                )
                layout.separator()
                layout.prop(item, "rb_transfer_motion", text="Transfer Current Motion")
                motion_column = layout.column()
                motion_column.enabled = item.rb_transfer_motion
                motion_column.prop(
                    item,
                    "rb_transfer_acceleration",
                    text="Transfer Previous-Frame Acceleration",
                )
                layout.separator()
                build = layout.operator(
                    "bflow.build_rigid_bodies",
                    text="Build Native Rigid Bodies",
                    icon='PHYSICS',
                )
                build.tree_name = tree.name
                build.node_name = node.name
                build.index = index
                layout.operator(
                    "bflow.clear_rigid_bodies",
                    text="Clear / Restore B-Flow Preview",
                    icon='TRASH',
                )
                layout.label(text="Converts particles visible at the current frame", icon='INFO')
                layout.label(text="Enabled Object Collision targets become Passive bodies", icon='INFO')
                layout.label(text="Existing Rigid Bodies keep their original type", icon='INFO')
                layout.label(text="Future Birth schedule is not included yet", icon='INFO')
                layout.label(text="Bake in Scene > Rigid Body World > Cache", icon='INFO')
            elif t == "DELETE":
                layout.label(text="Particle Deletion", icon='TRASH')
                delete_mode_row = layout.row(align=True)
                delete_mode_row.prop(item, "delete_mode", expand=True)
                if item.delete_mode == "AFTER_COLLISION":
                    has_ground = any(
                        candidate.enabled and candidate.operator_type == "COLLISION"
                        for event in tree.nodes if event.bl_idname == "BFLOW_EventNode"
                        for candidate in event.operator_items
                    )
                    has_object = any(
                        candidate.enabled and candidate.operator_type == "OBJECT_COLLISION"
                        and candidate.collider is not None
                        for event in tree.nodes if event.bl_idname == "BFLOW_EventNode"
                        for candidate in event.operator_items
                    )
                    layout.label(text="Deletes on first Ground/Object Collision")
                    if not (has_ground or has_object):
                        layout.label(
                            text="Enable Ground or Object Collision first",
                            icon='ERROR'
                        )
                else:
                    delete_unit_row = layout.row(align=True)
                    delete_unit_row.prop(item, "delete_time_unit", expand=True)
                    layout.prop(item, "delete_after", text="Delete After")
                    if item.delete_mode == "REAL_TIME":
                        layout.label(text="Uses the global scene timeline clock")
                    else:
                        layout.label(text="Uses each particle's age since Birth")
            elif t == "DISPLAY":
                layout.label(text="Viewport display settings will be added in a later build.")

        layout.separator()
        expanded = context.scene.bflow_depot_commands_expanded
        row = layout.row(align=True)
        toggle = row.operator(
            "bflow.toggle_depot_commands",
            text="Show Bottom Operator Depot",
            icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT',
            emboss=False,
        )

        if expanded:
            box = layout.box()
            box.label(text="OPERATOR DEPOT  |  ADD", icon='PRESET')
            for identifier, label in _BFLOW_DEPOT_ITEMS:
                add_row = box.row(align=True)
                add_row.label(text=label, icon=OPERATOR_ICON_DEFAULTS.get(identifier, "DOT"))
                add = add_row.operator("bflow.add_depot_operator", text="", icon='ADD')
                add.operator_type = identifier


class BFLOW_PT_view3d(Panel):
    bl_label = "B-Flow Particle System"
    bl_idname = "BFLOW_PT_view3d"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "B-Flow"

    def draw(self, context):
        layout = self.layout
        tree = context.scene.bflow_graph
        if tree:
            layout.label(text=f"Graph: {tree.name}", icon='NODETREE')
            layout.operator("bflow.open_editor", icon='NODETREE')
            layout.operator("bflow.compile_simulation", text="Compile / Update Simulation", icon='PLAY')
            layout.operator("bflow.add_event", icon='NODE')
            layout.label(text="B-Flow V1.52")
        else:
            layout.operator("bflow.new_graph", icon='ADD')

class BFLOW_OT_open_editor(Operator):
    bl_idname = "bflow.open_editor"
    bl_label = "Open B-Flow Event Editor"

    def execute(self, context):
        tree = context.scene.bflow_graph
        if not tree:
            self.report({'WARNING'}, "Create a graph first")
            return {'CANCELLED'}
        # Use the same split-layout routine as New Graph. This preserves
        # the 3D Viewport on the left instead of converting the whole area.
        _bflow_open_split_layout(context, tree)
        return {'FINISHED'}

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

CLASSES = (
    BFLOW_RandColorEntry,
    BFLOW_ShapeInstanceEntry,
    BFLOW_ForceFieldRef,
    BFLOW_OperatorItem,
    BFLOW_Socket,
    BFLOW_Tree,
    BFLOW_EventNode,
    BFLOW_OT_new_graph,
    BFLOW_OT_add_event,
    BFLOW_OT_add_operator,
    BFLOW_OT_bottom_depot,
    BFLOW_OT_toggle_depot_commands,
    BFLOW_OT_add_depot_operator,
    BFLOW_OT_add_rand_color_entry,
    BFLOW_OT_add_shape_instance_entry,
    BFLOW_OT_remove_shape_instance_entry,
    BFLOW_OT_add_force_field,
    BFLOW_OT_remove_force_field,
    BFLOW_OT_remove_rand_color_entry,
    BFLOW_OT_select_operator,
    BFLOW_OT_remove_operator,
    BFLOW_OT_move_operator,
    BFLOW_OT_toggle_operator,
    BFLOW_OT_migrate_legacy,
    BFLOW_OT_build_rigid_bodies,
    BFLOW_OT_clear_rigid_bodies,
    BFLOW_OT_compile_simulation,
    BFLOW_OT_open_editor,
    BFLOW_PT_stack_sidebar,
    BFLOW_PT_view3d,
)


_BFLOW_DEPOT_ACTIVE_AREAS = set()
_BFLOW_DEPOT_INITIALIZED_AREAS = set()

def _bflow_auto_start_depots():
    try:
        wm = bpy.context.window_manager
        if wm is None:
            return 1.0
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type != 'NODE_EDITOR':
                    continue
                space = area.spaces.active
                tree = getattr(space, "edit_tree", None)
                if not tree or tree.bl_idname != "BFLOW_Tree":
                    continue
                area_key = (window.as_pointer(), area.as_pointer())
                if area_key in _BFLOW_DEPOT_INITIALIZED_AREAS:
                    continue
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                if region is None:
                    continue
                try:
                    with bpy.context.temp_override(window=window, area=area, region=region):
                        result = bpy.ops.bflow.bottom_depot('INVOKE_DEFAULT')
                    # Mark this editor as initialized even if the user later
                    # closes the Depot; reopening is then an explicit action.
                    _BFLOW_DEPOT_INITIALIZED_AREAS.add(area_key)
                    if 'RUNNING_MODAL' in result:
                        _BFLOW_DEPOT_ACTIVE_AREAS.add(area_key)
                except Exception as exc:
                    print("[B-Flow] Could not auto-start bottom depot:", exc)
        # Keep watching so the depot also appears when the user opens B-Flow
        # after the add-on has already been enabled.
        return 1.0
    except Exception as exc:
        print("[B-Flow] Depot startup check failed:", exc)
        return 2.0


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bflow_graph = PointerProperty(name="B-Flow Graph", type=BFLOW_Tree)
    bpy.types.Scene.bflow_default_emitter = PointerProperty(
        name="B-Flow Default Emitter",
        description="Selected mesh captured when the B-Flow graph was created; overridden only when Position Object has an assigned mesh",
        type=bpy.types.Object,
    )
    bpy.types.Scene.bflow_depot_commands_expanded = BoolProperty(
        name="Show Bottom Operator Depot",
        description="Expand the right-side operator list and reopen the bottom Depot",
        default=True,
    )
    try:
        bpy.app.timers.register(_bflow_auto_start_depots, first_interval=0.8)
    except Exception as exc:
        print("[B-Flow] Could not schedule Operator Depot:", exc)

def unregister():
    if hasattr(bpy.types.Scene, "bflow_graph"):
        del bpy.types.Scene.bflow_graph
    if hasattr(bpy.types.Scene, "bflow_default_emitter"):
        del bpy.types.Scene.bflow_default_emitter
    if hasattr(bpy.types.Scene, "bflow_depot_commands_expanded"):
        del bpy.types.Scene.bflow_depot_commands_expanded
    _BFLOW_DEPOT_ACTIVE_AREAS.clear()
    _BFLOW_DEPOT_INITIALIZED_AREAS.clear()
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

if __name__ == "__main__":
    register()
