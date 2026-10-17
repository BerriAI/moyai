"""Convert the supplied Meshy FBX and texture maps to the home-screen GLB.

Run with Blender --background --factory-startup --python this-file -- source-dir output.glb.
"""
from pathlib import Path
import json
import struct
import sys

import bpy
from mathutils import Matrix, Vector

source, output = map(Path, sys.argv[sys.argv.index("--") + 1:])
stem = "Meshy_AI_Silent_Stone_Orbit_1017192604_texture"
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.fbx(filepath=str(source / f"{stem}.fbx"))
meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
for obj in meshes:
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    modifier = obj.modifiers.new("Web geometry", "DECIMATE")
    modifier.ratio = min(1, 50000 / len(obj.data.polygons))
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    for face in obj.data.polygons:
        face.use_smooth = True
    obj.select_set(False)

corners = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
low = Vector(tuple(min(v[i] for v in corners) for i in range(3)))
high = Vector(tuple(max(v[i] for v in corners) for i in range(3)))
center, scale = (high + low) / 2, 4.15 / max(high - low)
material = bpy.data.materials.new("Silent Stone Orbit")
material.use_nodes = True
nodes, links = material.node_tree.nodes, material.node_tree.links
surface = nodes.get("Principled BSDF")
for suffix, socket in [("", "Base Color"), ("_roughness", "Roughness"), ("_metallic", "Metallic"), ("_normal", "Normal")]:
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = bpy.data.images.load(str(source / f"{stem}{suffix}.png"))
    if suffix:
        texture.image.colorspace_settings.name = "Non-Color"
    texture.image.scale(2048, 2048)
    if suffix == "_normal":
        normal = nodes.new("ShaderNodeNormalMap")
        links.new(texture.outputs["Color"], normal.inputs["Color"])
        links.new(normal.outputs["Normal"], surface.inputs[socket])
    else:
        links.new(texture.outputs["Color"], surface.inputs[socket])
for obj in meshes:
    obj.location = (obj.location - center) * scale
    obj.scale *= scale
    obj.data.materials.clear()
    obj.data.materials.append(material)
    obj.select_set(True)
output.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.export_scene.gltf(filepath=str(output), export_format="GLB", use_selection=True,
                          export_image_format="JPEG", export_jpeg_quality=92, export_tangents=True)

# Collapsed UVs can produce zero tangents after decimation. Give those vertices
# a valid perpendicular tangent so the normal map remains portable across viewers.
glb = bytearray(output.read_bytes())
json_length = struct.unpack_from("<I", glb, 12)[0]
document = json.loads(glb[20:20 + json_length])
binary_start = 28 + json_length
def element_offset(accessor, index, size):
    view = document["bufferViews"][accessor["bufferView"]]
    return binary_start + view.get("byteOffset", 0) + accessor.get("byteOffset", 0) + index * view.get("byteStride", size)

for mesh in document["meshes"]:
    for primitive in mesh["primitives"]:
        attributes = primitive["attributes"]
        if "TANGENT" not in attributes:
            continue
        tangents = document["accessors"][attributes["TANGENT"]]
        normals = document["accessors"][attributes["NORMAL"]]
        for index in range(tangents["count"]):
            offset = element_offset(tangents, index, 16)
            x, y, z, handedness = struct.unpack_from("<4f", glb, offset)
            if x * x + y * y + z * z < 1e-12:
                normal = Vector(struct.unpack_from("<3f", glb, element_offset(normals, index, 12)))
                axis = Vector((0, 0, 1)) if abs(normal.z) < .9 else Vector((0, 1, 0))
                tangent = normal.cross(axis).normalized()
                struct.pack_into("<4f", glb, offset, *tangent, handedness)
output.write_bytes(glb)

# Match the viewer's starting pose without changing the exported geometry.
pose = Matrix.Rotation(-.1, 4, "X") @ Matrix.Rotation(.6, 4, "Z")
for obj in meshes:
    obj.matrix_world = pose @ obj.matrix_world

# A matching fallback uses the same geometry, textures and camera.
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.samples = 32
scene.cycles.use_denoising = True
scene.render.resolution_x = scene.render.resolution_y = 640
scene.render.resolution_percentage = 100
scene.render.film_transparent = True
scene.world = bpy.data.worlds.new("Studio")
scene.world.use_nodes = True
scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (.14, .16, .21, 1)
scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = .3
bpy.ops.object.camera_add(location=(0, -8.6, 0))
camera = bpy.context.object
camera.rotation_euler = (-camera.location).to_track_quat("-Z", "Y").to_euler()
camera.data.type = "PERSP"
camera.data.angle = .558505
scene.camera = camera
for color, energy, xyz in [((.94,.95,.98),3.8,(3,-5,5)), ((.48,.61,.9),2.4,(-4,2,0)), ((.61,.53,.85),.95,(3,3,1)), ((.51,.62,.91),.9,(-4,-3,-2))]:
    bpy.ops.object.light_add(type="SUN", location=xyz)
    light = bpy.context.object
    light.data.energy, light.data.color, light.data.angle = energy, color, .12
    light.rotation_euler = (-light.location).to_track_quat("-Z", "Y").to_euler()
scene.render.filepath = str(output.with_suffix(".png"))
bpy.ops.render.render(write_still=True)
print(f"Exported {sum(len(obj.data.polygons) for obj in meshes):,} faces to {output}")
