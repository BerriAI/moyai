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
    dy, dx = np.gradient(height)
    normal = np.stack((-dx * 1.4, dy * 1.4, np.ones_like(dx)), axis=-1)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    normal_image = Image.fromarray(np.uint8(np.clip(normal * .5 + .5, 0, 1) * 255))
    mineral = np.clip(.78 + height * .075, .38, .98)
    color_image = Image.fromarray(np.uint8(np.stack((mineral*.94, mineral*.97, mineral), axis=-1)*255))
    return trimesh.visual.material.PBRMaterial(
        name="Cool gray cratered stone", baseColorFactor=[.64, .64, .64, 1],
        baseColorTexture=color_image, normalTexture=normal_image,
        metallicFactor=0., roughnessFactor=.88,
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

    # Irregular parent rock, with broad geological breaks in its silhouette.
    dx, dy, dz = x + .35 + y * .12, y, z + .22
    radii = (1.62, 2.03, 1.53)
    k0 = np.sqrt((dx / radii[0])**2 + (dy / radii[1])**2 + (dz / radii[2])**2)
    k1 = np.sqrt((dx / radii[0]**2)**2 + (dy / radii[1]**2)**2 + (dz / radii[2]**2)**2)
    field = k0 * (k0 - 1) / np.maximum(k1, 1e-6)
    field += .16 * np.sin(3.1*x + 1.7*z) * np.sin(2.4*y - .8*z)
    field += .065 * np.sin(7.3*x - 2.1*y + z) * np.cos(5.8*z + 1.2*y)
    for center, half, angle in [
        ((-1.15, 1.65, -.25), (.53, .63, .65), .36),
        ((-.25, 1.9, -.25), (.49, .39, .65), -.25),
        ((-1.65, .45, -.4), (.46, .68, .65), .24),
        ((-1.50, -.75, -.35), (.56, .61, .66), -.38),
        ((-.95, -1.65, -.3), (.55, .48, .48), .27),
        ((.45, -1.60, -.55), (.55, .45, .55), -.24),
        ((1.02, .95, -.45), (.42, .53, .68), -.32),
        ((.62, .05, -1.0), (.68, .72, .65), .17),
    ]:
        field = smooth_union(field, rounded_box(center, half, .025, angle), .045)

    # Angular carved planes rather than smooth ellipsoid facial features.
    face_x = .49
    field = smooth_union(field, rounded_box((face_x, -.10, .99), (.62, 1.50, .46), .06), .06)
    forehead = rounded_box((face_x, 1.00, 1.17), (.68, .43, .43), .045)
    # Slight arch under the wide, overhanging brow.
    forehead = np.maximum(forehead, .52 + .12 * (1 - ((x-face_x)/.68)**2) - y)
    field = smooth_union(field, forehead, .035)
    eye = rounded_box((face_x, .43, 1.48), (.57, .20, .40), .04)
    field = np.maximum(field, -eye)
    taper = np.clip((.61 - y) / 1.21, 0, 1)
    nose_width, nose_front = .125 + .14 * taper, 1.48 + .53 * taper
    nx = np.abs(x - face_x) - nose_width
    ny = np.abs(y - .02) - .65
    nz = np.maximum(1.09 - z, z - nose_front)
    nose = np.maximum(nx, np.maximum(ny, nz))
    field = smooth_union(field, nose, .025)
    field = smooth_union(field, rounded_box((face_x, -1.00, 1.38), (.48, .12, .23), .055), .035)
    field = smooth_union(field, rounded_box((face_x, -1.37, 1.13), (.58, .29, .43), .055), .045)
    mouth_y = -1.04 - .13 * ((x - face_x) / .52)**2
    mouth = np.maximum(np.abs(x - face_x) - .52,
                       np.maximum(np.abs(y - mouth_y) - .036, np.abs(z - 1.52) - .23))
    field = np.maximum(field, -mouth)

    # Small embedded rubble along the face's seam and the irregular left edge.
    for cx, cy, cz, size in [
        (-.42, 1.45, 1.05, .16), (-.75, .90, 1.12, .20),
        (-.42, -.50, 1.25, .14), (-.48, -1.16, 1.14, .19),
        (-1.60, .38, .90, .23), (-1.70, -.03, .83, .12),
        (-1.37, -1.35, .76, .18), (.80, 1.74, .61, .18),
        (1.25, -.9, .54, .16), (-.92, 1.88, .75, .15),
    ]:
        field = smooth_union(field, rounded_box((cx, cy, cz), (size, size*.82, size*.72),
                                                .018, float(rng.uniform(-.7, .7))), .025)

    # Broken planes and fine erosion are baked, never evaluated during rendering.
    for shape, amplitude in [((28, 32, 27), .018), ((100, 108, 100), .006)]:
        lattice = rng.uniform(-1, 1, shape).astype(np.float32)
        field += amplitude * zoom(lattice, counts / np.array(shape), order=1)
    for center, normal, extent in [
        ((-.7, .2, .9), (.8, .5, .2), 1.2),
        ((.0, -.7, 1.4), (.8, -.6, .25), .65),
        ((.60, .95, 1.6), (.9, .3, .1), .65),
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
    for cx, cy, radius in [(-.95, .65, .35), (-1.04, -.30, .43),
                           (-.80, -1.18, .35), (-.47, 1.42, .22),
                           (-.05, -1.95, .21), (1.15, .02, .23)]:
        hit = surface(np.array([cx, cy, 3.5]), np.array([0., 0., -1.]))
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
        if point[2] > 1.05 and -.35 < point[0] < 1.25 and -1.5 < point[1] < 1.5:
            radius = min(radius, .07)
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
    if len(mesh.faces) > target_faces:
        mesh = mesh.simplify_quadric_decimation(face_count=target_faces, aggression=5)

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
        uv[mask] = projected[mask][:, pair] * .70
    mesh = trimesh.Trimesh(vertices=projected, faces=inverse.reshape(-1, 3),
                           vertex_normals=normals[indices], process=False)
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv, material=stone_material(rng))
    mesh.visual.vertex_attributes["color"] = np.round(colors[indices] * 255).astype(np.uint8)
    mesh.metadata["name"] = "Moyai meteor | cratered basalt"
    # Center and fit the same camera envelope used by the original sculpture.
    bounds = mesh.bounds
    mesh.apply_translation(-(bounds[0] + bounds[1]) / 2)
    mesh.apply_scale(4.15 / np.ptp(mesh.vertices[:, 1]))
    mesh.apply_scale([1.10, 1., 1.])
    scene = trimesh.Scene(mesh)
    result = scene.export(file_type="glb")
    assert np.isfinite(mesh.vertices).all()
    assert len(mesh.faces) <= target_faces
    assert len(result) < 5_000_000
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result)
    print(json.dumps({"output": str(output), "vertices": len(mesh.vertices),
                      "triangles": len(mesh.faces), "craters": len(craters), "bytes": len(result)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolution", type=int, default=220)
    parser.add_argument("--faces", type=int, default=100_000)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "native/web/public/moyai.glb")
    args = parser.parse_args()
    generate(args.resolution, args.faces, args.output)
