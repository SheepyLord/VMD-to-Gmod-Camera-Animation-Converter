#!/usr/bin/env python3
"""Convert an MMD camera VMD file into JSON text for the Garry's Mod Camera Path Tool.

The addon's importer expects the resulting JSON text to be pasted into the in-game
"Import MMD Camera Motion" window.

This converter now rebuilds VMD camera motion using the same camera rig semantics
used by Blender MMD Tools:
- the VMD camera `location` becomes the rig root / target point
- the VMD `distance` stays a local camera offset on the child camera
- the VMD `rotation` is applied using Blender's YXZ MMD camera rig order

Example:
    python tools/vmd_to_campath_mmd_json.py camera.vmd -o camera_import.json
"""
from __future__ import annotations

import argparse
import json
import math
import struct
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import BinaryIO, Dict, List, Sequence

VMD_SIGNATURE_PREFIX = b"Vocaloid Motion Data"
BONE_FRAME_SIZE = 111
MORPH_FRAME_SIZE = 23
CAMERA_FRAME_STRUCT = struct.Struct("<I7f24BIb")
DEFAULT_SENSOR_WIDTH = 36.0
DEFAULT_FPS = 30.0
PAYLOAD_FORMAT = "campath_mmd_camera_import_v2"
SOURCE_SPACE = "mmd_vmd_camera_rig_blender_compatible"


class VMDParseError(Exception):
    pass


@dataclass
class Vec3:
    x: float
    y: float
    z: float


@dataclass
class CameraFrame:
    frame: int
    seconds: float
    name: str
    angle: float
    fov: float
    focalLength: float
    perspective: bool
    distance: float
    location: Vec3
    rotation: Vec3
    target: Vec3
    camera: Vec3
    interpolation: List[int]
    sourceSpace: str


@dataclass
class VMDMotion:
    format: str
    sourceSpace: str
    name: str
    source_file: str
    model_name: str
    fps: float
    sensorWidth: float
    defaultTransition: str
    cameraFrames: List[CameraFrame]


def decode_text(raw: bytes) -> str:
    raw = raw.split(b"\x00", 1)[0]
    if not raw:
        return ""
    for encoding in ("shift_jis", "cp932", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace").strip()


def read_u32(fh: BinaryIO) -> int:
    data = fh.read(4)
    if len(data) != 4:
        raise VMDParseError("Unexpected end of file while reading a 32-bit count.")
    return struct.unpack("<I", data)[0]


def skip_exact(fh: BinaryIO, byte_count: int, label: str) -> None:
    skipped = fh.read(byte_count)
    if len(skipped) != byte_count:
        raise VMDParseError(f"Unexpected end of file while skipping {label}.")


def focal_from_fov(fov_degrees: float, sensor_width: float = DEFAULT_SENSOR_WIDTH) -> float:
    """Return a focal-length metadata value from FOV.

    The addon renders from FOV directly, so this is editable metadata for the lens UI.
    """
    safe_fov = min(max(float(fov_degrees), 1.0), 179.0)
    tangent = math.tan(math.radians(safe_fov) * 0.5)
    if tangent <= 1e-6:
        return sensor_width
    return sensor_width / (2.0 * tangent)


def add_vec(a: Vec3, b: Vec3) -> Vec3:
    return Vec3(a.x + b.x, a.y + b.y, a.z + b.z)


def mmd_to_blender(vec: Sequence[float] | Vec3) -> Vec3:
    if isinstance(vec, Vec3):
        return Vec3(vec.x, vec.z, vec.y)
    x, y, z = map(float, vec)
    return Vec3(x, z, y)


def blender_to_mmd(vec: Sequence[float] | Vec3) -> Vec3:
    if isinstance(vec, Vec3):
        return Vec3(vec.x, vec.z, vec.y)
    x, y, z = map(float, vec)
    return Vec3(x, z, y)


def rotate_blender_vector_by_vmd_camera_rig(vec: Sequence[float] | Vec3, raw_rotation: Vec3) -> Vec3:
    """Rotate a Blender-space vector using Blender MMD Tools' camera rig semantics.

    Blender MMD Tools writes VMD camera rotations onto the rig root as:
    - x <- raw_rotation.x
    - z <- raw_rotation.y
    - y <- raw_rotation.z
    with rotation mode YXZ.

    Applying that YXZ Euler to a vector is equivalent to rotating in this order:
    1) Z by raw_rotation.y
    2) X by raw_rotation.x
    3) Y by raw_rotation.z
    """
    if isinstance(vec, Vec3):
        x, y, z = vec.x, vec.y, vec.z
    else:
        x, y, z = map(float, vec)

    cz, sz = math.cos(raw_rotation.y), math.sin(raw_rotation.y)
    x, y = (x * cz - y * sz), (x * sz + y * cz)

    cx, sx = math.cos(raw_rotation.x), math.sin(raw_rotation.x)
    y, z = (y * cx - z * sx), (y * sx + z * cx)

    cy, sy = math.cos(raw_rotation.z), math.sin(raw_rotation.z)
    x, z = (x * cy + z * sy), (-x * sy + z * cy)

    return Vec3(x, y, z)


def reconstruct_camera_from_blender_mmd_tools(location: Vec3, distance: float, rotation: Vec3) -> tuple[Vec3, Vec3]:
    """Rebuild target and camera world-space positions from raw VMD camera values.

    This follows the same model used by Blender MMD Tools:
    - the rig root is placed at the VMD camera location (after the MMD->Blender axis swap)
    - the child camera keeps a local Y offset equal to `distance`
    - the child camera's constant 90° X rotation makes it look back toward the rig root
    """
    target_blender = mmd_to_blender(location)
    offset_blender = rotate_blender_vector_by_vmd_camera_rig((0.0, float(distance), 0.0), rotation)
    camera_blender = add_vec(target_blender, offset_blender)
    return location, blender_to_mmd(camera_blender)


def parse_vmd_camera(
    vmd_path: Path,
    *,
    sensor_width: float = DEFAULT_SENSOR_WIDTH,
    fps: float = DEFAULT_FPS,
    default_transition: str = "linear",
) -> Dict[str, object]:
    with vmd_path.open("rb") as fh:
        signature = fh.read(30)
        if len(signature) != 30:
            raise VMDParseError("File is too short to contain a VMD header.")
        if not signature.startswith(VMD_SIGNATURE_PREFIX):
            raise VMDParseError("File does not look like a VMD motion file.")

        # VMD 0002 uses a 20-byte model/camera name, older VMD 0001 uses 10 bytes.
        model_name_size = 20 if b"0002" in signature else 10
        model_name = decode_text(fh.read(model_name_size))

        bone_count = read_u32(fh)
        skip_exact(fh, bone_count * BONE_FRAME_SIZE, "bone frames")

        morph_count = read_u32(fh)
        skip_exact(fh, morph_count * MORPH_FRAME_SIZE, "morph frames")

        camera_count = read_u32(fh)
        camera_frames: List[CameraFrame] = []

        for index in range(camera_count):
            raw = fh.read(CAMERA_FRAME_STRUCT.size)
            if len(raw) != CAMERA_FRAME_STRUCT.size:
                raise VMDParseError(
                    f"Unexpected end of file while reading camera frame {index + 1}/{camera_count}."
                )

            (
                frame_number,
                distance,
                tx,
                ty,
                tz,
                rx,
                ry,
                rz,
                *tail,
            ) = CAMERA_FRAME_STRUCT.unpack(raw)
            interpolation = list(map(int, tail[:24]))
            angle = float(tail[24])
            perspective_flag = int(tail[25])

            location = Vec3(float(tx), float(ty), float(tz))
            rotation = Vec3(float(rx), float(ry), float(rz))
            target, camera = reconstruct_camera_from_blender_mmd_tools(location, float(distance), rotation)

            camera_frames.append(
                CameraFrame(
                    frame=int(frame_number),
                    seconds=float(frame_number) / float(fps),
                    name=f"MMD {int(frame_number):05d}",
                    angle=angle,
                    fov=angle,
                    focalLength=focal_from_fov(angle, sensor_width),
                    perspective=(perspective_flag == 0),
                    distance=float(distance),
                    location=location,
                    rotation=rotation,
                    target=target,
                    camera=camera,
                    interpolation=interpolation,
                    sourceSpace=SOURCE_SPACE,
                )
            )

    camera_frames.sort(key=lambda item: (item.frame, item.name))

    payload = VMDMotion(
        format=PAYLOAD_FORMAT,
        sourceSpace=SOURCE_SPACE,
        name=vmd_path.stem,
        source_file=vmd_path.name,
        model_name=model_name,
        fps=float(fps),
        sensorWidth=float(sensor_width),
        defaultTransition=default_transition,
        cameraFrames=camera_frames,
    )

    def to_jsonable(obj):
        if isinstance(obj, list):
            return [to_jsonable(item) for item in obj]
        if hasattr(obj, "__dataclass_fields__"):
            return {key: to_jsonable(value) for key, value in asdict(obj).items()}
        return obj

    return to_jsonable(payload)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert an MMD .vmd camera motion file into JSON text that the Camera Path Tool can import."
    )
    parser.add_argument("input", type=Path, help="Path to the source .vmd file.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Where to write the JSON text. Defaults to <input>.campath_mmd.json next to the VMD file.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=DEFAULT_FPS,
        help=f"Frames per second used to convert VMD frame numbers into seconds. Default: {DEFAULT_FPS}",
    )
    parser.add_argument(
        "--sensor-width",
        type=float,
        default=DEFAULT_SENSOR_WIDTH,
        help=f"Sensor width metadata stored in the JSON. Default: {DEFAULT_SENSOR_WIDTH}",
    )
    parser.add_argument(
        "--default-transition",
        default="linear",
        help="Transition name stored in the JSON for imported keyframes. The addon falls back to linear if it does not recognize the value.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="Pretty-print indentation for the generated JSON. Use 0 for the most compact output.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    input_path: Path = args.input.expanduser().resolve()
    if not input_path.is_file():
        parser.error(f"Input file not found: {input_path}")

    output_path = args.output
    if output_path is None:
        output_path = input_path.with_suffix(".campath_mmd.json")
    else:
        output_path = output_path.expanduser().resolve()

    try:
        payload = parse_vmd_camera(
            input_path,
            sensor_width=float(args.sensor_width),
            fps=float(args.fps),
            default_transition=str(args.default_transition),
        )
    except VMDParseError as exc:
        parser.exit(2, f"Error: {exc}\n")

    indent = None if int(args.indent) <= 0 else int(args.indent)
    json_text = json.dumps(payload, ensure_ascii=False, indent=indent)
    if indent is not None:
        json_text += "\n"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json_text, encoding="utf-8")

    frame_count = len(payload.get("cameraFrames", []))
    print(f"Wrote {frame_count} camera frames to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
