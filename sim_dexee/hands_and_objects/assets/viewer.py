"""Browse OBJ assets using the project's existing Open3D dependency.

Close each window for the next page; press Esc in a window to stop browsing.
"""

import argparse
import math
from pathlib import Path

import numpy as np
import open3d as o3d


def build_scene(paths, root):
    """Arrange models in a grid, independently centered and uniformly scaled."""
    scene = []
    columns = math.ceil(math.sqrt(len(paths)))
    for index, path in enumerate(paths):
        label = path.relative_to(root).as_posix()
        try:
            mesh = o3d.io.read_triangle_mesh(str(path), enable_post_processing=True)
            if mesh.is_empty() or not mesh.has_triangles():
                print(f"Skipping empty model: {label}")
                continue
            bounds = np.array([mesh.get_min_bound(), mesh.get_max_bound()])
            extent = float(np.max(bounds[1] - bounds[0]))
            if not np.isfinite(bounds).all() or extent <= 0:
                print(f"Skipping invalid bounds: {label}")
                continue
            center = bounds.mean(axis=0)
            offset = np.array([
                (index % columns) * 1.5,
                -(index // columns) * 1.5,
                0.0,
            ])
            mesh.translate(-center)
            mesh.scale(1.0 / extent, center=np.zeros(3))
            mesh.translate(offset)
            mesh.compute_vertex_normals()
            if not mesh.has_vertex_colors() and not mesh.has_textures():
                mesh.paint_uniform_color([0.65, 0.75, 0.85])
            scene.append(mesh)
            print(f"  [{index + 1}] {label}")
        except Exception as exc:
            print(f"Failed to load {label}: {exc}")
    return scene


def show_page(scene, title):
    """Return False when Esc is pressed, True when the window is closed."""
    viewer = o3d.visualization.VisualizerWithKeyCallback()
    if not viewer.create_window(window_name=title, width=1280, height=900):
        raise RuntimeError("Cannot create Open3D window; check your desktop/display connection.")
    keep_browsing = True

    def stop(vis):
        nonlocal keep_browsing
        keep_browsing = False
        vis.close()
        return False

    try:
        viewer.register_key_callback(256, stop)  # GLFW_KEY_ESCAPE
        for mesh in scene:
            viewer.add_geometry(mesh, reset_bounding_box=False)
        options = viewer.get_render_option()
        options.mesh_show_back_face = True
        options.background_color = np.array([0.12, 0.12, 0.15])
        viewer.reset_view_point(True)
        viewer.get_view_control().set_front([0.0, 0.0, 1.0])
        viewer.get_view_control().set_up([0.0, 1.0, 0.0])
        viewer.run()
    finally:
        viewer.destroy_window()
    return keep_browsing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-size", type=int, default=36,
                        help="Models per window (default: 36)")
    parser.add_argument("--page", type=int, default=1,
                        help="First page to display, starting at 1")
    args = parser.parse_args()
    if args.page_size < 1 or args.page < 1:
        parser.error("--page-size and --page must be positive integers")

    root = Path(__file__).resolve().parent
    paths = sorted(
        (path for path in root.rglob("*")
         if path.is_file() and path.suffix.lower() == ".obj"),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not paths:
        print(f"No OBJ files found under {root}")
        return
    page_count = math.ceil(len(paths) / args.page_size)
    if args.page > page_count:
        parser.error(f"--page exceeds total page count ({page_count})")
    print(f"Found {len(paths)} OBJ files; {page_count} pages.")
    print("Grid order: left to right, top to bottom; filenames are listed below.")
    print("Drag to rotate; scroll to zoom. Close window for next page; Esc to exit.")
    try:
        for page_index in range(args.page - 1, page_count):
            print(f"\nPage {page_index + 1}/{page_count}")
            start = page_index * args.page_size
            scene = build_scene(paths[start:start + args.page_size], root)
            if scene and not show_page(scene, f"OBJ assets - page {page_index + 1}/{page_count}"):
                break
    except KeyboardInterrupt:
        print("\nViewer stopped.")


if __name__ == "__main__":
    main()
