# VMD to Camera Path JSON Converter

Convert an **MMD camera `.vmd` motion file** into **JSON text** that can be imported into the **Garry's Mod Camera Path Tool**.

This script is designed for the Camera Path addon workflow where MMD camera motion is first converted on desktop, then pasted into the addon's **Import MMD Camera Motion** window in-game.

---

## Features

- Reads **MMD camera VMD** files directly
- Exports JSON in the format expected by the Camera Path addon
- Reconstructs camera motion using **MMD / Blender camera rig semantics**
- Preserves:
  - frame timing
  - distance
  - target/location
  - rotation
  - FOV
  - interpolation bytes
- Generates editable lens metadata:
  - FOV
  - focal length
  - sensor width
- Supports custom:
  - FPS
  - sensor width
  - default transition
  - JSON indentation

---

## Why this converter exists

MMD camera motion is not stored as a simple world-space camera path.

A VMD camera frame contains:
- a **target/root location**
- a **camera distance**
- a **rotation**
- a **field of view**
- interpolation data

To match how MMD camera motion behaves in tools like Blender, this converter rebuilds the camera using the same general rig logic:

- the VMD `location` is treated as the **camera rig root / target**
- the VMD `distance` is treated as a **local camera offset**
- the VMD `rotation` is applied with the expected rig rotation behavior

This avoids common issues such as:
- incorrect origin offset
- incorrect distance-to-target behavior
- drift after import
- roll / orientation mismatches

---

## Requirements

- Python **3.9+** recommended
- No third-party Python dependencies required

---

## Usage

### Basic usage

```bash
python vmd_to_campath_mmd_json.py camera.vmd
