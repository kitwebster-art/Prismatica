#!/usr/bin/env python3
"""Build the bundled Prismatica scale visitor from Microsoft Rocketbox.

Run with Blender in background mode, for example:

  Blender --background --python scripts/build-human-scale-model.py -- \
    --source-dir /path/to/rocketbox-source \
    --output assets/models/standing-gallery-visitor.glb

The source directory must contain the selected avatar FBX, its three referenced
colour/opacity textures, and m_idle_neutral_01.max.fbx. The final GLB freezes
an authored gallery-viewing pose and contains no animation or rig.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import bpy


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--avatar-name", default="Male_Adult_04")
    parser.add_argument("--texture-size", type=int, default=1024)
    return parser.parse_args(argv)


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablocks in (
        bpy.data.armatures,
        bpy.data.meshes,
        bpy.data.materials,
        bpy.data.images,
        bpy.data.actions,
    ):
        for datablock in list(datablocks):
            datablocks.remove(datablock)


def existing_image_nodes(material: bpy.types.Material):
    if not material.use_nodes:
        return []
    return [
        node
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE" and node.image is not None
    ]


def optimise_textures(source_dir: Path, texture_size: int) -> None:
    derivatives = source_dir / "derived"
    derivatives.mkdir(exist_ok=True)
    source_textures = {
        "body": next(source_dir.glob("*_body_color.tga")),
        "head": next(source_dir.glob("*_head_color.tga")),
        "opacity": next(source_dir.glob("*_opacity_color.tga")),
    }
    formats = {
        source_textures["body"].name: (f"{source_textures['body'].stem}.jpg", "JPEG"),
        source_textures["head"].name: (f"{source_textures['head'].stem}.jpg", "JPEG"),
        source_textures["opacity"].name: (f"{source_textures['opacity'].stem}.png", "PNG"),
    }
    converted = {}
    for source_name, (output_name, output_format) in formats.items():
        source_path = source_dir / source_name
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        image = bpy.data.images.load(str(source_path), check_existing=False)
        image.scale(texture_size, texture_size)
        image.filepath_raw = str(derivatives / output_name)
        image.file_format = output_format
        image.save()
        converted[source_name] = image

    for material in bpy.data.materials:
        for node in existing_image_nodes(material):
            source_name = Path(bpy.path.abspath(node.image.filepath)).name
            if source_name in converted:
                node.image = converted[source_name]
            elif not Path(bpy.path.abspath(node.image.filepath)).exists():
                material.node_tree.nodes.remove(node)


def import_avatar(source_dir: Path, avatar_name: str):
    before = set(bpy.context.scene.objects)
    bpy.ops.import_scene.fbx(filepath=str(source_dir / f"{avatar_name}.fbx"))
    imported = set(bpy.context.scene.objects) - before
    armature = next(obj for obj in imported if obj.type == "ARMATURE")
    mesh = next(obj for obj in imported if obj.type == "MESH")
    return armature, mesh, imported


def add_ik_target(armature, bone_name: str, target_location, pole_location) -> set:
    target = bpy.data.objects.new(f"{bone_name}-target", None)
    pole = bpy.data.objects.new(f"{bone_name}-pole", None)
    bpy.context.scene.collection.objects.link(target)
    bpy.context.scene.collection.objects.link(pole)
    target.location = target_location
    pole.location = pole_location
    constraint = armature.pose.bones[bone_name].constraints.new("IK")
    constraint.target = target
    constraint.pole_target = pole
    constraint.chain_count = 3
    constraint.use_tail = True
    constraint.iterations = 80
    return {target, pole}


def apply_considering_pose(source_dir: Path, avatar_armature, avatar_mesh, avatar_objects) -> None:
    before = set(bpy.context.scene.objects)
    bpy.ops.import_scene.fbx(filepath=str(source_dir / "m_idle_neutral_01.max.fbx"))
    animation_objects = set(bpy.context.scene.objects) - before
    animation_armature = next(obj for obj in animation_objects if obj.type == "ARMATURE")

    action = animation_armature.animation_data.action if animation_armature.animation_data else None
    if action is None:
        raise RuntimeError("The Rocketbox idle FBX did not contain an animation action")
    avatar_armature.animation_data_create()
    avatar_armature.animation_data.action = action
    start, end = action.frame_range
    bpy.context.scene.frame_set(round(start + (end - start) * 0.37))
    bpy.context.view_layer.update()

    # An authored, readable gallery pose: the right hand rests at the chin with
    # the elbow kept close to the torso, while the left arm retains the relaxed
    # idle pose. The avatar faces local -Y before glTF axis conversion.
    pose_helpers = set()
    pose_helpers |= add_ik_target(
        avatar_armature,
        "Bip01 R Hand",
        (-0.065, -0.205, 1.545),
        (-0.38, -0.03, 1.31),
    )
    bpy.context.view_layer.update()

    bpy.ops.object.select_all(action="DESELECT")
    avatar_mesh.select_set(True)
    bpy.context.view_layer.objects.active = avatar_mesh
    for modifier in list(avatar_mesh.modifiers):
        if modifier.type == "ARMATURE":
            bpy.ops.object.modifier_apply(modifier=modifier.name)

    for obj in animation_objects | pose_helpers | (avatar_objects - {avatar_mesh}):
        bpy.data.objects.remove(obj, do_unlink=True)


def prepare_mesh(mesh) -> None:
    mesh.name = "standing-gallery-visitor"
    mesh.data.name = "standing-gallery-visitor-mesh"
    for polygon in mesh.data.polygons:
        polygon.use_smooth = True

    # Place the feet on Y=0 after Three.js converts glTF's Y-up convention.
    corners = [mesh.matrix_world @ __import__("mathutils").Vector(corner) for corner in mesh.bound_box]
    min_z = min(corner.z for corner in corners)
    mesh.location.z -= min_z
    bpy.context.view_layer.update()


def export_glb(mesh, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    bpy.context.view_layer.objects.active = mesh
    bpy.ops.export_scene.gltf(
        filepath=str(output),
        export_format="GLB",
        use_selection=True,
        export_apply=True,
        export_animations=False,
        export_skins=False,
        export_morph=False,
        export_image_format="AUTO",
        export_jpeg_quality=78,
        export_copyright="Microsoft Rocketbox, MIT License, 2020 Microsoft",
    )


def main() -> None:
    args = parse_args()
    source_dir = args.source_dir.resolve()
    output = args.output.resolve()
    clear_scene()
    armature, mesh, avatar_objects = import_avatar(source_dir, args.avatar_name)
    optimise_textures(source_dir, args.texture_size)
    apply_considering_pose(source_dir, armature, mesh, avatar_objects)
    prepare_mesh(mesh)
    export_glb(mesh, output)
    print(f"Built {output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
