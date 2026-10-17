# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "numpy==2.5.4",
#   "scipy==1.18.1",
#   "scikit-image==0.26.0",
#   "trimesh==5.1.1",
#   "fast-simplification==0.2.0",
#   "pillow==13.0.0",
# ]
# ///
"""Bake an original, fully volumetric Moai meteor for the home sculpture.

Run with uv (offline asset creation, not an application dependency):
  uv run --script scripts/assets/generate_moyai_meteor.py

The face, rock, craters and vertex shading are generated geometry. No photograph
is projected onto the mesh. The supplied meteor image is a visual reference.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image
from scipy.ndimage import gaussian_filter, map_coordinates, zoom
from skimage.measure import marching_cubes


def stone_material(rng):
    """Small, embedded surface maps: fractured mineral grain, not a photo decal."""
    size = 512
    height = np.zeros((size, size))
    for sigma, strength in ((18, .42), (7, .25), (2.5, .18), (.7, .07)):
        grain = gaussian_filter(rng.normal(size=(size, size)), sigma, mode="wrap")
        height += grain / grain.std() * strength
    yy, xx = np.mgrid[:size, :size]
    for _ in range(350):
        cx, cy = rng.uniform(0, size, 2)
        radius = rng.uniform(1.5, 6)
        distance = np.hypot(np.minimum(abs(xx-cx), size-abs(xx-cx)),
                            np.minimum(abs(yy-cy), size-abs(yy-cy))) / radius
        height -= np.maximum(1-distance**2, 0)**.65 * rng.uniform(.4, 1.1)
    # Sparse crystalline inclusions catch the lighting as the rock turns.
    flakes = np.zeros_like(height)
    for _ in range(230):
        cx, cy = rng.uniform(0, size, 2)
        rx, ry = rng.uniform(1.1, 3.2, 2)
        flake = ((xx-cx)/rx)**2 + ((yy-cy)/ry)**2 < 1
        flakes[flake] = rng.uniform(.65, 1.)
    dy, dx = np.gradient(height)
    normal = np.stack((-dx * 1.4, dy * 1.4, np.ones_like(dx)), axis=-1)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    normal_image = Image.fromarray(np.uint8(np.clip(normal * .5 + .5, 0, 1) * 255))
    mineral = np.clip(.74 + height * .13, .28, .98)
    colors = np.stack((mineral*.94, mineral*.97, mineral), axis=-1)
    colors = colors * (1-flakes[..., None]*.65) + np.array([.80, .86, 1.])*flakes[..., None]*.65
    color_image = Image.fromarray(np.uint8(colors*255))
    roughness = np.clip(.86 + height*.035 - flakes*.74, .10, 1.)
    metalness = .025 + flakes*.82
    surface_image = Image.fromarray(np.uint8(np.stack((np.ones_like(height), roughness, metalness), axis=-1)*255))
    return trimesh.visual.material.PBRMaterial(
        name="Silver gray stone with crystalline inclusions", baseColorFactor=[.70, .70, .70, 1],
        baseColorTexture=color_image, normalTexture=normal_image,
        metallicRoughnessTexture=surface_image, metallicFactor=1., roughnessFactor=1.,
    )


def generate(resolution: int, target_faces: int, output: Path) -> None:
    rng = np.random.default_rng(17012026)
    low = np.array([-2.7, -2.7, -2.5], dtype=np.float32)
    high = np.array([2.4, 2.7, 2.5], dtype=np.float32)
    counts = np.array([resolution, round(resolution * 1.08), resolution])
    spacing = (high - low) / (counts - 1)
    x = np.linspace(low[0], high[0], counts[0], dtype=np.float32)[:, None, None]
    y = np.linspace(low[1], high[1], counts[1], dtype=np.float32)[None, :, None]
    z = np.linspace(low[2], high[2], counts[2], dtype=np.float32)[None, None, :]

    def smooth_union(a, b, radius=.12):
        h = np.clip(.5 + .5 * (b - a) / radius, 0, 1)
        return b * (1 - h) + a * h - radius * h * (1 - h)

    def rounded_box(center, half, radius=.08, angle=0):
        dx, dy, dz = x - center[0], y - center[1], z - center[2]
        if angle:
            dx, dy = dx * np.cos(angle) - dy * np.sin(angle), dx * np.sin(angle) + dy * np.cos(angle)
        qx = np.abs(dx) - half[0] + radius
        qy = np.abs(dy) - half[1] + radius
        qz = np.abs(dz) - half[2] + radius
        outer = np.sqrt(np.maximum(qx, 0)**2 + np.maximum(qy, 0)**2 + np.maximum(qz, 0)**2)
        return outer + np.minimum(np.maximum(qx, np.maximum(qy, qz)), 0) - radius

    def fragment(center, half, angle):
        dx, dy, dz = x-center[0], y-center[1], z-center[2]
        rx = dx*np.cos(angle)-dy*np.sin(angle)
        ry = dx*np.sin(angle)+dy*np.cos(angle)
        cut = (np.abs(rx)/half[0] + np.abs(ry)/half[1] + np.abs(dz)/half[2] - 2.0) * min(half)
        return np.maximum(rounded_box(center, half, .025, angle), cut)

    # Irregular parent rock, with broad geological breaks in its silhouette.
    dx, dy, dz = x + .40 + y * .14, y, z + .28
    radii = (1.80, 2.04, 1.58)
    k0 = np.sqrt((dx / radii[0])**2 + (dy / radii[1])**2 + (dz / radii[2])**2)
    k1 = np.sqrt((dx / radii[0]**2)**2 + (dy / radii[1]**2)**2 + (dz / radii[2]**2)**2)
    field = k0 * (k0 - 1) / np.maximum(k1, 1e-6)
    field += .16 * np.sin(3.1*x + 1.7*z) * np.sin(2.4*y - .8*z)
    field += .065 * np.sin(7.3*x - 2.1*y + z) * np.cos(5.8*z + 1.2*y)
    # The cratered flank falls away from the face as a broad geological plane.
    field = np.maximum(field, z - (.90 + .40*(x+.4)))
    for center, half, angle in [
        ((-1.10, 1.65, -.35), (.60, .60, .72), .36),
        ((-.15, 1.8, -.45), (.55, .39, .68), -.25),
        ((-1.65, .55, -.3), (.62, .68, .74), .24),
        ((-1.45, -.65, -.45), (.65, .63, .70), -.38),
        ((-1.00, -1.55, -.4), (.65, .48, .60), .27),
        ((.65, -1.45, -.45), (.55, .55, .60), -.24),
        ((1.35, .85, -.5), (.42, .53, .65), -.32),
        ((.62, .05, -1.0), (.68, .72, .65), .17),
    ]:
        field = smooth_union(field, fragment(center, half, angle), .055)

    # The whole front plane is a single Moai, carved from the parent rock.
    # Broad cheeks and brow merge into its sides instead of forming a narrow mask.
    face_x = .40
    front = rounded_box((face_x, -.05, .82), (1.12, 1.63, .58), .13)
    cheek_taper = np.maximum(abs(x-face_x) - (1.08 + .05*y), z - (1.43 - .12*abs(x-face_x)))
    field = smooth_union(field, np.maximum(front, cheek_taper), .14)
    forehead = rounded_box((face_x, 1.03, 1.09), (1.15, .56, .48), .23)
    forehead = np.maximum(forehead, z - (1.63 - .24*((x-face_x)/1.15)**2))
    # Slight arch under the wide, overhanging brow.
    forehead = np.maximum(forehead, .49 + .18 * (1 - ((x-face_x)/1.15)**2) - y)
    field = smooth_union(field, forehead, .075)
    eye = rounded_box((face_x, .42, 1.43), (.99, .23, .38), .075)
    field = np.maximum(field, -eye)
    taper = np.clip((.65 - y) / 1.30, 0, 1)
    nose_width = .19 + .17 * taper
    nose_front = 1.44 + .61 * taper - .10*abs(x-face_x)/nose_width
    nx = np.abs(x - face_x) - nose_width
    ny = np.abs(y - .02) - .68
    nz = np.maximum(1.09 - z, z - nose_front)
    nose = np.maximum(nx, np.maximum(ny, nz))
    field = smooth_union(field, nose, .055)
    # A shallow undercut lets the broad nose read as a carved ledge.
    field = np.maximum(field, -rounded_box((face_x, -.68, 1.81), (.25, .065, .22), .035))
    field = smooth_union(field, rounded_box((face_x, -1.04, 1.30), (.79, .12, .23), .10), .15)
    chin = rounded_box((face_x, -1.39, 1.09), (1.00, .29, .45), .22)
    chin = np.maximum(chin, z - (1.55 + (y+1.22)*.75))
    field = smooth_union(field, chin, .18)
    mouth_y = -1.07 - .14 * ((x-face_x) / .79)**2
    mouth = np.maximum(np.abs(x-face_x) - .80,
                       np.maximum(np.abs(y - mouth_y) - .042, np.abs(z - 1.47) - .26))
    field = np.maximum(field, -mouth)

    # Small embedded rubble along the face's seam and the irregular left edge.
    for cx, cy, cz, size in [
        (-.80, 1.45, .80, .16), (-.85, .90, .85, .20),
        (-.72, -.50, .90, .14), (-.75, -1.16, .80, .19),
        (-1.80, .38, .40, .23), (-1.90, -.03, .35, .12),
        (-1.55, -1.35, .35, .18), (.80, 1.74, .55, .18),
        (1.55, -.9, .54, .16), (-1.32, 1.88, .35, .15),
    ]:
        field = smooth_union(field, fragment((cx, cy, cz), (size, size*.82, size*.72),
                                             float(rng.uniform(-.7, .7))), .035)

    # Broken planes and fine erosion are baked, never evaluated during rendering.
    for shape, amplitude in [((28, 32, 27), .018), ((100, 108, 100), .006)]:
        lattice = rng.uniform(-1, 1, shape).astype(np.float32)
        field += amplitude * zoom(lattice, counts / np.array(shape), order=1)
    for center, normal, extent in [
        ((-.7, .2, .9), (.8, .5, .2), 1.2),
        ((.0, -.7, 1.4), (.8, -.6, .25), .65),
        ((.60, .95, 1.6), (.9, .3, .1), .75),
        ((-.8, -1.3, .9), (.6, -.8, .2), .8),
        ((-1.3, 1.3, .6), (.6, .7, -.2), .9),
    ]:
        plane = ((x-center[0])*normal[0] + (y-center[1])*normal[1] + (z-center[2])*normal[2]
                 + .018*np.sin(33*y+17*z) + .012*np.sin(49*x-21*y))
        limit = extent - np.sqrt((x-center[0])**2 + (y-center[1])**2 + (z-center[2])**2)
        field = np.maximum(field, np.minimum(np.minimum(.014 - abs(plane), limit), field + .065))
    field = field.astype(np.float32)

    def sample(points, volume=None):
        coordinates = ((points - low) / spacing).T
        return map_coordinates(field if volume is None else volume, coordinates, order=1, mode="constant", cval=2.)

    def surface(origin, direction):
        distances = np.linspace(0, 7, 280)
        positions = origin + distances[:, None] * direction
        values = sample(positions)
        hits = np.flatnonzero(values < 0)
        if not len(hits) or hits[0] == 0:
            return None
        index = hits[0]
        fraction = values[index - 1] / (values[index - 1] - values[index])
        point = positions[index - 1] + fraction * (positions[index] - positions[index - 1])
        normal = np.array([sample((point + np.eye(3)[axis]*.14)[None, :])[0]
                           - sample((point - np.eye(3)[axis]*.14)[None, :])[0] for axis in range(3)])
        return point, normal / max(np.linalg.norm(normal), 1e-8)

    craters = []
    for cx, cy, radius in [(-1.20, .90, .34), (-1.40, -.10, .49),
                           (-1.10, -1.15, .38), (-.90, 1.55, .24),
                           (-.05, -1.95, .21), (1.60, .02, .23)]:
        hit = surface(np.array([cx, cy, 3.5]), np.array([0., 0., -1.]))
        if hit:
            point, normal = hit
            if cx < -.7:
                normal = np.array([-.4, 0., 1.])
                normal /= np.linalg.norm(normal)
            craters.append((point, normal, radius))
    # Large impact bowls continue around the left flank, visible in the default
    # three-quarter view without punching through the face's key features.
    for cy, cz, radius in [(.80, -.05, .33), (-.20, -.12, .42), (-1.08, -.24, .29),
                           (.30, -.85, .26)]:
        hit = surface(np.array([-3.5, cy, cz]), np.array([1., 0., 0.]))
        if hit:
            craters.append((*hit, radius))
    for _ in range(5000):
        direction = rng.normal(size=3)
        direction /= np.linalg.norm(direction)
        hit = surface(direction * 4.5, -direction)
        if hit is None:
            continue
        point, normal = hit
        radius = float(rng.uniform(.045, .10) if rng.random() < .85 else rng.uniform(.12, .27))
        # Keep the face recognizable; only small pits on its important features.
        if point[2] > 1.05 and abs(point[0]-face_x) < 1.12 and -1.7 < point[1] < 1.6:
            radius = float(rng.uniform(.025, .055))
        if any(np.linalg.norm(point - prior) < (radius + other) * .9 for prior, _, other in craters):
            continue
        craters.append((point, normal, radius))
        if len(craters) >= 520:
            break

    for point, normal, radius in craters:
        center = point + normal * radius * .32
        lo = np.maximum(0, np.floor((center - radius * 1.08 - low) / spacing).astype(int))
        hi = np.minimum(counts, np.ceil((center + radius * 1.08 - low) / spacing).astype(int) + 1)
        sl = tuple(slice(a, b) for a, b in zip(lo, hi))
        gx = x[lo[0]:hi[0]] - center[0]
        gy = y[:, lo[1]:hi[1]] - center[1]
        gz = z[:, :, lo[2]:hi[2]] - center[2]
        sphere = np.sqrt(gx*gx + gy*gy + gz*gz) - radius
        field[sl] = np.maximum(field[sl], -sphere)

    vertices, faces, _, _ = marching_cubes(field, level=0, spacing=tuple(spacing), allow_degenerate=False)
    vertices += low
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    if mesh.volume < 0:
        mesh.invert()
    mineral_count = 300
    if len(mesh.faces) > target_faces - mineral_count:
        mesh = mesh.simplify_quadric_decimation(face_count=target_faces - mineral_count, aggression=5)

    # Baked ambient occlusion and mottled mineral color; no texture fetches.
    vertices, normals = np.asarray(mesh.vertices), np.asarray(mesh.vertex_normals)
    occlusion = np.zeros(len(vertices))
    for distance, weight in ((.04, .12), (.10, .22), (.22, .34), (.42, .32)):
        outside = sample(vertices + normals * distance)
        occlusion += np.clip(1 - outside / distance, 0, 1) * weight
    grain = (np.sin(vertices[:, 0]*47 + vertices[:, 1]*19) *
             np.cos(vertices[:, 2]*39 - vertices[:, 1]*23))
    strata = np.sin(vertices[:, 0]*9 + vertices[:, 1]*12 + vertices[:, 2]*7)
    value = np.clip((.60 + grain*.07 + strata*.035) * (1 - occlusion*.80), .10, .78)
    colors = np.column_stack((value*.93, value*.96, value, np.ones(len(value))))
    # A restrained mineral tint follows real crater walls and broken rims.
    # It is surface color, not a halo or an emissive outline around the object.
    rim = np.zeros(len(vertices))
    deposits = np.zeros(len(vertices))
    for point, normal, radius in craters:
        if radius < .12:
            continue
        delta = vertices - point
        depth = delta @ normal
        radial = np.sqrt(np.maximum(np.sum(delta*delta, axis=1) - depth*depth, 0))
        band = np.exp(-((radial/radius-.87)/.18)**2)
        band *= np.clip(1-abs(depth/radius+.12)/.48, 0, 1)
        facing = np.clip(normals @ np.array([-.72, -.35, .60]), 0, 1)
        rim = np.maximum(rim, band * facing)
        deposits = np.maximum(deposits, np.clip(1-radial/radius, 0, 1) * np.clip(-depth/radius, 0, 1))
    colors[:, :3] *= 1-rim[:, None]*np.array([.28, .13, -.12])
    colors[:, :3] *= 1-deposits[:, None]*.60
    colors[:, :3] = np.clip(colors[:, :3], 0, 1)

    # Tiny mineral facets sit on the actual crater walls. Their polished material
    # reflects the environment and lights, so glints move with the user's rotation.
    chosen = rng.choice(len(vertices), size=mineral_count, replace=False, p=deposits/deposits.sum())
    crystal_vertices = []
    for index in chosen:
        normal = normals[index]
        facet_normal = normal + rng.normal(size=3)*.28
        facet_normal /= np.linalg.norm(facet_normal)
        tangent = np.cross(facet_normal, (0, 1, 0) if abs(facet_normal[1]) < .9 else (1, 0, 0))
        tangent /= np.linalg.norm(tangent)
        bitangent = np.cross(facet_normal, tangent)
        center = vertices[index] + normal*.006
        size = float(rng.uniform(.008, .025))
        crystal_vertices.extend([
            center + tangent*size,
            center - tangent*size*.65 + bitangent*size*.6,
            center - tangent*size*.65 - bitangent*size*.6,
        ])
    crystals = trimesh.Trimesh(vertices=crystal_vertices, faces=np.arange(mineral_count*3).reshape(-1, 3), process=False)
    crystals.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(
        name="Reflective crater minerals", baseColorFactor=[.58, .70, .95, 1],
        metallicFactor=.65, roughnessFactor=.14,
    ))

    # Box projection with split UV seams, retaining the original smooth normals.
    # The embedded 512px maps add mineral grain without millions of triangles.
    axes = np.argmax(np.abs(mesh.face_normals), axis=1)
    keys = mesh.faces * 3 + axes[:, None]
    unique, inverse = np.unique(keys.ravel(), return_inverse=True)
    indices, axes = unique // 3, unique % 3
    projected = vertices[indices]
    uv = np.zeros((len(unique), 2))
    for axis, pair in enumerate(((2, 1), (0, 2), (0, 1))):
        mask = axes == axis
        uv[mask] = projected[mask][:, pair] * .40
    mesh = trimesh.Trimesh(vertices=projected, faces=inverse.reshape(-1, 3),
                           vertex_normals=normals[indices], process=False)
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv, material=stone_material(rng))
    mesh.visual.vertex_attributes["color"] = np.round(colors[indices] * 255).astype(np.uint8)
    mesh.metadata["name"] = "Moyai meteor | cratered basalt"
    # Center and fit the same camera envelope used by the original sculpture.
    bounds = mesh.bounds
    center = (bounds[0] + bounds[1]) / 2
    scale = 4.15 / np.ptp(mesh.vertices[:, 1])
    for part in (mesh, crystals):
        part.apply_translation(-center)
        part.apply_scale(scale)
        # Match the reference's roughly .95 width/height silhouette in the
        # viewer's initial three-quarter pose, keeping the broad cratered flank.
        part.apply_scale([.945, 1., 1.])
    scene = trimesh.Scene([mesh, crystals])
    result = scene.export(file_type="glb")
    assert np.isfinite(mesh.vertices).all()
    assert len(mesh.faces) + len(crystals.faces) <= target_faces
    assert len(result) < 5_500_000
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result)
    print(json.dumps({"output": str(output), "vertices": len(mesh.vertices)+len(crystals.vertices),
                      "triangles": len(mesh.faces)+len(crystals.faces), "craters": len(craters),
                      "mineral_facets": mineral_count, "bytes": len(result)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolution", type=int, default=220)
    parser.add_argument("--faces", type=int, default=100_000)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "native/web/public/moyai.glb")
    args = parser.parse_args()
    generate(args.resolution, args.faces, args.output)
