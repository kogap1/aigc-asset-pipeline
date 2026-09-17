"""基准实验共用的工具函数（bridge_96 / production 复用）。"""
import json
import math
from pathlib import Path


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_grid(files, out_path, cols=6):
    from PIL import Image, ImageOps

    if not files:
        return
    thumb_size = (256, 256)
    rows = math.ceil(len(files) / cols)
    canvas = Image.new("RGB", (cols * thumb_size[0], rows * thumb_size[1]), "white")
    for index, path in enumerate(files):
        with Image.open(path).convert("RGB") as image:
            thumb = ImageOps.fit(image, thumb_size)
            canvas.paste(thumb, ((index % cols) * thumb_size[0], (index // cols) * thumb_size[1]))
    canvas.save(out_path, optimize=True)


def compute_fid(generated_dir, real_dir):
    from pytorch_fid import fid_score

    return float(fid_score.calculate_fid_given_paths(
        [str(generated_dir), str(real_dir)], batch_size=32, device="cuda", dims=2048
    ))


def load_records(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {record["item_id"]: record for record in data}
