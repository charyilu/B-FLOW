bl_info = {
    "name": "B-Flow Particle System",
    "author": "OpenAI",
    "version": (0, 8, 46),
    "blender": (5, 2, 0),
    "location": "Node Editor > B-Flow",
    "description": "Gate particle movement by its stored birth frame",
    "category": "Node",
}

import bpy
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
    ("KEEP_DIST", "Keep Dist", "Softly repel particles that are closer than a set distance"),
    ("ROTATION", "Rotation", "Set random initial particle orientation"),
    ("SPIN", "Spin", "Apply per-particle angular motion"),
    ("COLLISION", "Ground Collision", "Collide with a horizontal ground plane"),
    ("OBJECT_COLLISION", "Object Collision", "Collide with a selected mesh object"),
    ("SHAPE_INSTANCE", "Shape Instance", "Replace particle display with a custom object"),
    ("SIZE", "Size", "Set fixed size or animate size over real/particle time"),
    ("OPACITY", "Opacity", "Fade particle instances using fixed, real, or particle time"),
    ("DELETE", "DEL", "Delete particles by scene time, particle age, or collision"),
]

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
    if identifier in {"SCALE", "SCALE_OVER_AGE", "SIZE", "OPACITY", "COLOR", "ROTATION", "SPIN"}:
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


def _bflow_enable_opacity_on_material(material, owner_name):
    """Wrap each active material surface in a Transparent/Surface mix.

    The Instancer Attribute node reads the per-particle 'bflow_opacity'
    attribute stored on the Geometry Nodes points. Original material datablocks
    are not changed; this function is only used on dedicated material copies.
    """
    material.use_nodes = True
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


def _bflow_remove_generated_opacity_assets(owner_name):
    """Remove only B-Flow generated hidden source objects/materials for a host."""
    for generated in list(bpy.data.objects):
        if generated.get("bflow_opacity_owner") != owner_name:
            continue
        data = generated.data
        bpy.data.objects.remove(generated, do_unlink=True)
        if data is not None and getattr(data, "users", 1) == 0:
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)

    for material in list(bpy.data.materials):
        if (
            material.get("bflow_opacity_owner") == owner_name
            and material.users == 0
        ):
            bpy.data.materials.remove(material)


def _bflow_make_opacity_shape_source(source_object, host_object, scene):
    """Create a hidden duplicate mesh with copied, opacity-enabled materials."""
    if source_object is None or source_object.data is None:
        return None

    try:
        duplicate = source_object.copy()
        duplicate.data = source_object.data.copy()
        duplicate.name = f"B-Flow Opacity Source - {source_object.name}"
        duplicate["bflow_opacity_owner"] = host_object.name
        duplicate["bflow_opacity_source"] = True

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

        # Copy and wrap every material slot. If the source has no material,
        # create a basic Principled material for the generated display copy.
        original_materials = list(data.materials)
        if not original_materials:
            generated_material = bpy.data.materials.new(
                f"B-Flow Opacity Material - {source_object.name}"
            )
            generated_material.use_nodes = True
            generated_material["bflow_opacity_owner"] = host_object.name
            _bflow_enable_opacity_on_material(generated_material, host_object.name)
            data.materials.append(generated_material)
        else:
            for slot_index, original_material in enumerate(original_materials):
                if original_material is None:
                    copied_material = bpy.data.materials.new(
                        f"B-Flow Opacity Material - {source_object.name} - {slot_index + 1}"
                    )
                    copied_material.use_nodes = True
                else:
                    copied_material = original_material.copy()
                    copied_material.name = f"B-Flow Opacity - {original_material.name}"
                copied_material["bflow_opacity_owner"] = host_object.name
                _bflow_enable_opacity_on_material(copied_material, host_object.name)
                data.materials[slot_index] = copied_material

        duplicate.hide_render = True
        try:
            duplicate.hide_set(True)
        except Exception:
            pass
        return duplicate
    except Exception as exc:
        print("[B-Flow] Could not create opacity Shape Instance source:", exc)
        try:
            if 'duplicate' in locals() and duplicate and duplicate.name in bpy.data.objects:
                bpy.data.objects.remove(duplicate, do_unlink=True)
        except Exception:
            pass
        return None


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

class BFLOW_OperatorItem(PropertyGroup):
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
    size_duration: FloatProperty(
        name="Duration", default=30.0, min=0.001, max=1000000.0,
        update=_bflow_schedule_live_update,
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
        update=_bflow_schedule_live_update,
    )
    opacity_end_frame: IntProperty(
        name="End Frame",
        description="Scene frame for Real Time, or age in frames for Particle Time",
        default=30,
        min=1,
        max=1000000,
        update=_bflow_schedule_live_update,
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
    shape_object: PointerProperty(
        name="Instance Object", type=bpy.types.Object,
        description="Object used as the visible particle shape",
        update=_bflow_schedule_live_update
    )
    shape_scale: FloatProperty(
        name="Instance Scale", default=1.0, min=0.0001, max=10000.0,
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
    ("KEEP_DIST", "Keep Dist"),
    ("ROTATION", "Rotation"),
    ("SPIN", "Spin"),
    ("COLLISION", "Ground Collision"),
    ("OBJECT_COLLISION", "Object Collision"),
    ("SHAPE_INSTANCE", "Shape Instance"),
    ("SIZE", "Size"),
    ("OPACITY", "Opacity"),
    ("DELETE", "DEL"),
]

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
    import gpu
    area = operator._area

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
        _bflow_depot_text(x+8, y+7, label, 11)
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
        shape_instance_scale = 1.0
        size_enabled = False
        size_mode = "FIXED"
        size_fixed = 1.0
        size_start = 1.0
        size_end = 2.0
        size_duration = 30.0
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
        rotation_randomness = 1.0
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
                    if item.shape_object is not None:
                        shape_instance_object = item.shape_object
                        shape_instance_scale = item.shape_scale
                elif item.operator_type == "SIZE" and not size_enabled:
                    size_enabled = True
                    size_mode = item.size_mode
                    size_fixed = item.size_fixed
                    size_start = item.size_start
                    size_end = item.size_end
                    size_duration = item.size_duration
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
                    rotation_randomness = item.rotation_randomness
                elif item.operator_type in {"POSITION_OBJECT", "POSITION_VOLUME"} and emitter_object is None:
                    emitter_object = item.source_object

        # Use the active mesh as a convenience if Position Object has no object.
        if emitter_object is None:
            candidate = context.view_layer.objects.active
            if candidate and candidate.name != "B-Flow Preview" and candidate.type == 'MESH':
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

        # Rebuild only B-Flow-managed opacity assets. Original Shape Instance
        # objects and their original materials are never modified.
        _bflow_remove_generated_opacity_assets(obj.name)
        opacity_shape_object = shape_instance_object
        opacity_default_material = None
        if opacity_enabled:
            if shape_instance_object is not None:
                opacity_shape_object = _bflow_make_opacity_shape_source(
                    shape_instance_object, obj, scene
                )
                if opacity_shape_object is None:
                    self.report(
                        {'WARNING'},
                        "Could not make an opacity-enabled Shape Instance copy; using original object."
                    )
                    opacity_shape_object = shape_instance_object
            else:
                opacity_default_material = _bflow_make_default_opacity_material(obj)

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

            store_initial_rotation = nodes.new("GeometryNodeStoreNamedAttribute")
            store_initial_rotation.label = "STORE INITIAL ROTATION"
            store_initial_rotation.location = (-420, 500)
            store_initial_rotation.domain = 'POINT'
            store_initial_rotation.data_type = 'FLOAT_VECTOR'
            store_initial_rotation.inputs["Name"].default_value = "bflow_rotation"
            links.new(birth_geometry, store_initial_rotation.inputs["Geometry"])
            links.new(random_rotation.outputs["Value"], store_initial_rotation.inputs["Value"])
            birth_geometry = store_initial_rotation.outputs["Geometry"]

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
        links.new(gravity_dt.outputs["Vector"], add_acceleration.inputs[1])
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

            progress = nodes.new("ShaderNodeMath")
            progress.operation = 'DIVIDE'
            progress.label = "SIZE ANIMATION PROGRESS"
            progress.location = (1740, -980)
            progress.inputs[1].default_value = max(size_duration, 0.001)
            links.new(elapsed_socket, progress.inputs[0])

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
            shape_info = nodes.new("GeometryNodeObjectInfo")
            shape_info.label = "PARTICLE SHAPE OBJECT"
            shape_info.location = (940, -260)
            # Use the shape object's own local geometry, not its scene-space
            # transform relative to the B-Flow preview host. The latter can
            # carry the source object's location into every particle instance.
            shape_info.transform_space = 'ORIGINAL'
            shape_info.inputs["Object"].default_value = opacity_shape_object

            instance = nodes.new("GeometryNodeInstanceOnPoints")
            instance.label = "CUSTOM SHAPE INSTANCES"
            instance.location = (1160, 100)
            links.new(display_geometry, instance.inputs["Points"])
            links.new(shape_info.outputs["Geometry"], instance.inputs["Instance"])
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
            if opacity_default_material is not None:
                set_ico_material = nodes.new("GeometryNodeSetMaterial")
                set_ico_material.label = "B-FLOW / PARTICLE OPACITY MATERIAL"
                set_ico_material.location = (1080, -280)
                set_ico_material.inputs["Material"].default_value = opacity_default_material
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

        group["bflow_version"] = "0.8.46"
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
        group["bflow_size_duration"] = size_duration
        group["bflow_size_time_unit"] = size_time_unit
        group["bflow_opacity_enabled"] = opacity_enabled
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

        # Reset timeline to the (possibly extended) start for a fresh simulation.
        scene.frame_set(scene.frame_start)
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
                layout.prop(item, "size_mode", text="Mode")
                if item.size_mode == "FIXED":
                    layout.prop(item, "size_fixed", text="Fixed Size")
                else:
                    layout.prop(item, "size_time_unit", text="Time Unit")
                    layout.prop(item, "size_start", text="Start Size")
                    layout.prop(item, "size_end", text="End Size")
                    layout.prop(item, "size_duration", text="Duration")
                    if item.size_mode == "REAL_TIME":
                        layout.label(text="All particles follow the scene timeline")
                    else:
                        layout.label(text="Each particle scales from its own Birth")
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
            elif t == "SHAPE_INSTANCE":
                layout.label(text="Replace particle display geometry", icon='OUTLINER_OB_MESH')
                layout.prop(item, "shape_object", text="Instance Object")
                layout.prop(item, "shape_scale", text="Instance Scale")
                layout.label(text="Physics and collision still use particle points", icon='INFO')
            elif t in {"SPEED_TEST", "DISTANCE_TEST"}:
                layout.prop(item, "threshold")
                if t == "DISTANCE_TEST":
                    layout.prop(item, "source_object")
            elif t == "RANDOM_TEST":
                layout.prop(item, "random_amount")
            elif t == "SPAWN":
                layout.prop(item, "child_count")
                layout.prop(item, "speed")
            elif t == "DELETE":
                layout.label(text="Particle Deletion", icon='TRASH')
                layout.prop(item, "delete_mode", text="Delete Mode")
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
                    layout.prop(item, "delete_time_unit", text="Time Unit")
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
                add_row.label(text=label)
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
            layout.label(text="B-Flow V0.8.46")
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
    BFLOW_OT_select_operator,
    BFLOW_OT_remove_operator,
    BFLOW_OT_move_operator,
    BFLOW_OT_toggle_operator,
    BFLOW_OT_migrate_legacy,
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
    bpy.types.Scene.bflow_depot_commands_expanded = BoolProperty(
        name="Show Bottom Operator Depot",
        description="Expand the right-side operator list and reopen the bottom Depot",
        default=False,
    )
    try:
        bpy.app.timers.register(_bflow_auto_start_depots, first_interval=0.8)
    except Exception as exc:
        print("[B-Flow] Could not schedule Operator Depot:", exc)

def unregister():
    if hasattr(bpy.types.Scene, "bflow_graph"):
        del bpy.types.Scene.bflow_graph
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
