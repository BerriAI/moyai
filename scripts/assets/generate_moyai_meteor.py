# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "numpy==2.5.4",
#   "scipy==1.18.1",
#   "trimesh==5.1.1",
#   "fast-simplification==0.2.0",
#   "pillow==13.0.0",
# ]
# ///
"""Build a closed, reference-textured meteor with inferred 3D relief.

  uv run --script scripts/assets/generate_moyai_meteor.py

The supplied photograph provides the silhouette and surface colors. Facial relief,
crater depths and the unseen back are authored estimates, not a recovered 3D scan.
The front UVs account for perspective so the initial view preserves the reference.
This runs offline; the app only loads the bundled GLB.
"""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw
from scipy.ndimage import (
    binary_fill_holes,
    distance_transform_edt,
    gaussian_filter,
    label,
)

CAMERA_DISTANCE = 8.6
REFERENCE_SIZE = 1254


def generate(resolution: int, target_faces: int, output: Path) -> None:
    rng = np.random.default_rng(17012026)
    original = Image.open(Path(__file__).with_name("moyai-meteor-reference.webp")).convert("RGBA")
    bounds = original.getchannel("A").getbbox()
    assert bounds is not None
    left, top, right, bottom = bounds
    crop = (max(left-8, 0), max(top-8, 0), min(right+8, original.width), min(bottom+8, original.height))
    photograph = original.crop(crop)
    width = round(resolution * photograph.width / photograph.height)
    height = resolution
    small = np.asarray(photograph.resize((width, height), Image.Resampling.LANCZOS))
    components, _ = label(small[:, :, 3] > 48)
    areas = np.bincount(components.ravel())
    areas[0] = 0
    mask = binary_fill_holes(components == areas.argmax())
    world_height = 4.15 * photograph.height / (bottom-top)
    world_width = world_height * photograph.width / photograph.height
    step_x, step_y = world_width/(width-1), world_height/(height-1)
    distance = distance_transform_edt(mask, sampling=(step_y, step_x))
    yy, xx = np.mgrid[:height, :width]
    sx = (xx/(width-1)-.5)*world_width
    sy = (.5-yy/(height-1))*world_height
    front = .035 + 1.18*np.sqrt(np.clip(distance/1.15, 0, 1))
    back = -.035 - 1.30*np.sqrt(np.clip(distance/1.30, 0, 1))

    def image_point(x, y):
        return ((x*original.width/REFERENCE_SIZE-crop[0])/photograph.width*(width-1),
                (y*original.height/REFERENCE_SIZE-crop[1])/photograph.height*(height-1))

    def relief(points, depth, blur=1.4):
        image = Image.new("L", (width, height))
        ImageDraw.Draw(image).polygon([image_point(*point) for point in points], fill=255)
        return gaussian_filter(np.asarray(image, dtype=float)/255, max(blur, 3.5))*depth

    # Large planes follow the photographed forehead, cheeks, nose and chin.
    front += relief([(727,410),(963,423),(1070,887),(996,945),(784,861),(714,610)], .22, 3)
    front += relief([(654,299),(889,245),(977,341),(1007,415),(942,452),(712,439)], .30, 2)
    # A continuous tapered bridge with sloping cheeks and a beveled solid tip.
    # Raising a whole polygon uniformly produces a thin paddle in profile.
    bridge = np.array(image_point(863,420))
    tip = np.array(image_point(947,614))
    axis = tip-bridge
    along = ((xx-bridge[0])*axis[0]+(yy-bridge[1])*axis[1])/np.dot(axis,axis)
    across = ((xx-bridge[0])*axis[1]-(yy-bridge[1])*axis[0])/np.linalg.norm(axis)
    progress = np.clip(along,0,1)
    pixels = original.width/REFERENCE_SIZE/photograph.width*(width-1)
    half_width = np.where(across < 0, 18+82*progress, 14+55*progress)*pixels
    cross_section = np.clip((1-abs(across)/half_width)*1.4,0,1)
    start = np.clip((along+.03)/.12,0,1)
    end = np.clip((1.11-along)/.11,0,1)
    cap = start*start*(3-2*start)*end*end*(3-2*end)
    front += gaussian_filter((.07+.49*progress)*cross_section*cap,.8)
    front += relief([(808,710),(897,691),(1016,722),(1037,748),(834,752)], .17, 1.5)
    front += relief([(799,750),(1037,747),(1065,885),(995,934),(792,851)], .22, 2)
    front -= relief([(708,411),(817,388),(848,413),(806,451),(710,449)], .15, 1.2)
    front -= relief([(908,397),(980,410),(1001,444),(985,475),(930,445)], .14, 1.2)
    front -= relief([(816,706),(899,695),(1016,722),(1029,737),(901,714),(819,725)], .10, .8)

    # Measured impact bowls in the reference, not unrelated random circles.
    craters = [
        (457,635,84,79), (515,846,70,67), (403,504,54,53),
        (488,302,54,53), (802,1013,51,49), (1071,592,48,57),
        (279,680,35,47), (416,840,35,40), (762,298,17,14),
        (580,277,11,8), (657,226,13,11), (713,217,11,9),
        (704,504,11,12), (628,681,14,17), (668,628,12,15),
        (440,980,20,24), (875,1111,13,16), (1094,804,22,29),
    ]
    bowls = np.zeros_like(front)
    for px, py, rx, ry in craters:
        cx, cy = image_point(px, py)
        rx = rx*original.width/REFERENCE_SIZE/photograph.width*(width-1)
        ry = ry*original.height/REFERENCE_SIZE/photograph.height*(height-1)
        radius2 = ((xx-cx)/rx)**2 + ((yy-cy)/ry)**2
        bowl = np.sqrt(np.clip(1-radius2, 0, 1))
        front -= bowl*min(rx*step_x, ry*step_y)*.62
        bowls = np.maximum(bowls, bowl)

    # Only fine image detail contributes relief; baked shadows are not deep holes.
    gray = np.mean(small[:, :, :3]/255, axis=2)
    front += gaussian_filter(gray-gaussian_filter(gray, 2.2), .7)*.015
    back += gaussian_filter(rng.normal(size=(height, width)), 3)*.12
    for _ in range(95):
        cx, cy = rng.uniform(0, width), rng.uniform(0, height)
        if distance[min(int(cy), height-1), min(int(cx), width-1)] < .22:
            continue
        radius = rng.uniform(.06, .25)
        radial2 = ((xx-cx)*step_x/radius)**2 + ((yy-cy)*step_y/radius)**2
        back += np.sqrt(np.clip(1-radial2, 0, 1))*radius*.55
    edge = np.clip(distance/.08, 0, 1)
    front = front*edge
    back = back*edge

    def points(z):
        perspective = (CAMERA_DISTANCE-z)/CAMERA_DISTANCE
        return np.stack((sx*perspective, sy*perspective, z), axis=-1).reshape(-1, 3)

    vertices = np.concatenate((points(front), points(back)))
    indices = np.arange(width*height).reshape(height, width)
    a, b, c, d = indices[:-1, :-1], indices[:-1, 1:], indices[1:, :-1], indices[1:, 1:]
    valid = mask[:-1, :-1] & mask[:-1, 1:] & mask[1:, :-1] & mask[1:, 1:]
    face_front = np.concatenate((np.stack((a,c,b), axis=-1)[valid], np.stack((b,c,d), axis=-1)[valid]))
    face_back = face_front[:, ::-1]+width*height
    edges = np.concatenate((face_front[:, [0,1]], face_front[:, [1,2]], face_front[:, [2,0]]))
    _, unique, count = np.unique(np.sort(edges, axis=1), axis=0, return_index=True, return_counts=True)
    boundary = edges[unique[count == 1]]
    p, q = boundary[:, 0], boundary[:, 1]
    side = np.concatenate((np.stack((q,p,p+width*height), axis=-1),
                           np.stack((q,p+width*height,q+width*height), axis=-1)))
    mesh = trimesh.Trimesh(vertices=vertices, faces=np.concatenate((face_front, face_back, side)), process=False)
    mesh.remove_unreferenced_vertices()
    if mesh.volume < 0:
        mesh.invert()
    assert mesh.is_watertight, "The reference relief must be a closed 3D object."
    mineral_count = 96
    if len(mesh.faces) > target_faces-mineral_count:
        mesh = mesh.simplify_quadric_decimation(face_count=target_faces-mineral_count, aggression=4)

    # Make a face-free stone crop periodic without mirrored crater patterns.
    # Subtract the smooth boundary discrepancy while retaining mineral detail.
    stone_bounds = tuple(round(v*original.width/REFERENCE_SIZE) for v in (267,375,661,1040))
    tile = np.asarray(original.crop(stone_bounds).convert("RGB").resize((512,768), Image.Resampling.LANCZOS), dtype=float)
    boundary = np.zeros_like(tile)
    boundary[0] += tile[-1]-tile[0]
    boundary[-1] -= tile[-1]-tile[0]
    boundary[:,0] += tile[:,-1]-tile[:,0]
    boundary[:,-1] -= tile[:,-1]-tile[:,0]
    eigenvalues = (2*np.cos(2*np.pi*np.arange(tile.shape[0])/tile.shape[0])[:,None]
                   + 2*np.cos(2*np.pi*np.arange(tile.shape[1])/tile.shape[1])[None,:]-4)
    eigenvalues[0,0] = 1
    smooth = np.fft.ifft2(np.fft.fft2(boundary, axes=(0,1))/eigenvalues[...,None], axes=(0,1)).real
    stone = Image.fromarray(np.uint8(np.clip(tile-smooth,0,255)))
    front_material = trimesh.visual.material.PBRMaterial(
        name="Reference surface", baseColorTexture=photograph, baseColorFactor=[1.,1.,1.,1.],
        metallicFactor=0., roughnessFactor=1.,
    )
    back_material = trimesh.visual.material.PBRMaterial(
        name="Inferred back", baseColorTexture=stone, baseColorFactor=[1.,1.,1.,1.],
        metallicFactor=0., roughnessFactor=1.,
    )
    positions, normals = np.asarray(mesh.vertices), np.asarray(mesh.vertex_normals)
    perspective = (CAMERA_DISTANCE-positions[:, 2])/CAMERA_DISTANCE
    uv = np.stack((positions[:, 0]/perspective/world_width+.5,
                   positions[:, 1]/perspective/world_height+.5), axis=-1)
    front_faces = mesh.face_normals[:, 2] > 0
    parts = []
    for selected, material in ((front_faces, front_material), (~front_faces, back_material)):
        unique, inverse = np.unique(mesh.faces[selected], return_inverse=True)
        part = trimesh.Trimesh(vertices=positions[unique], faces=inverse.reshape(-1, 3),
                               vertex_normals=normals[unique], process=False)
        part.visual = trimesh.visual.TextureVisuals(uv=uv[unique], material=material)
        parts.append(part)

    # Mineral facets add changing glints without replacing the photo's fine detail.
    candidates = np.argwhere((bowls > .20) & mask & (gray > .12))
    chosen = candidates[rng.choice(len(candidates), mineral_count, replace=False)]
    gy, gx = np.gradient(front, step_y, step_x)
    crystals = []
    for iy, ix in chosen:
        normal = np.array([-gx[iy,ix], gy[iy,ix], 1.])
        normal /= np.linalg.norm(normal)
        tangent = np.cross(normal, (0,1,0))
        tangent /= np.linalg.norm(tangent)
        bitangent = np.cross(normal, tangent)
        z = front[iy,ix]
        center = np.array([sx[iy,ix]*(CAMERA_DISTANCE-z)/CAMERA_DISTANCE,
                           sy[iy,ix]*(CAMERA_DISTANCE-z)/CAMERA_DISTANCE, z])+normal*.003
        size = float(rng.uniform(.004,.012))
        crystals.extend((center+tangent*size, center-tangent*size*.6+bitangent*size*.5,
                         center-tangent*size*.6-bitangent*size*.5))
    crystal_mesh = trimesh.Trimesh(vertices=crystals, faces=np.arange(mineral_count*3).reshape(-1,3), process=False)
    crystal_mesh.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(
        name="Mineral highlights", baseColorFactor=[.60,.72,.95,1.], metallicFactor=.65, roughnessFactor=.18,
    ))
    parts.append(crystal_mesh)
    glb = trimesh.Scene(parts).export(file_type="glb", include_normals=True)
    # Reference light and shade are already in the photo. The viewer adds only a
    # subtle physical reflection on top of these color-preserving base materials.
    json_size, _ = struct.unpack_from("<II", glb, 12)
    document = json.loads(glb[20:20+json_size])
    for material in document["materials"]:
        if material["name"] in ("Reference surface", "Inferred back"):
            material["extensions"] = {"KHR_materials_unlit": {}}
    document["extensionsUsed"] = list(dict.fromkeys(document.get("extensionsUsed", [])+["KHR_materials_unlit"]))
    packed = json.dumps(document, separators=(",", ":")).encode()
    packed += b" "*((-len(packed)) % 4)
    binary = glb[20+json_size:]
    result = struct.pack("<III", 0x46546C67, 2, 20+len(packed)+len(binary))
    result += struct.pack("<II", len(packed), 0x4E4F534A)+packed+binary
    assert np.isfinite(mesh.vertices).all() and mesh.is_watertight
    assert sum(len(part.faces) for part in parts) <= target_faces
    assert len(result) < 6_000_000
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result)
    print(json.dumps({"output": str(output), "triangles": sum(len(part.faces) for part in parts),
                      "watertight_body": bool(mesh.is_watertight), "bytes": len(result),
                      "reference_craters": len(craters), "mineral_facets": mineral_count}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolution", type=int, default=384)
    parser.add_argument("--faces", type=int, default=100_000)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2]/"native/web/public/moyai.glb")
    args = parser.parse_args()
    generate(args.resolution, args.faces, args.output)
