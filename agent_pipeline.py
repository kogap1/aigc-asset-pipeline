"""游戏素材自动化生成管线编排器。

流程: 一句话需求 → DeepSeek Function Calling 拆任务 → 调度器调 ComfyUI API 出图
     → CLIP-Score 质量门（不达标换 seed 重试） → 后处理(超分) → 结构化交付。

依赖只在对应功能内延迟导入，保证本机无服务器依赖时也能 import 与单测。
"""
import argparse
import concurrent.futures
import copy
import json
import os
import random
import re
import queue
import shutil
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PLACEHOLDER_RE = re.compile(r"^\{\{(\w+)\}\}$")


# ---------------------------------------------------------------------------
# DeepSeek Agent（Function Calling 解析需求）
# ---------------------------------------------------------------------------
class DeepSeekAgent:
    TOOLS = [{
        "type": "function",
        "function": {
            "name": "generate_assets",
            "description": "把一条自然语言素材需求拆解为一个或多个生成任务",
            "parameters": {
                "type": "object",
                "properties": {
                    "requests": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string",
                                           "description": "英文正向提示词，具体、可直接用于 Stable Diffusion，含主体/风格/构图/光影"},
                                "count": {"type": "integer", "description": "该任务生成的张数", "minimum": 1},
                                "mode": {"type": "string", "enum": ["txt2img", "img2img", "ipadapter"],
                                         "description": "txt2img 全新创作；img2img 在已有图上重绘翻新；ipadapter 用参考图做风格迁移"},
                                "ref_image": {"type": "string",
                                              "description": "img2img/ipadapter 模式时参考图在 ComfyUI input 目录的文件名(可选)"},
                            },
                            "required": ["prompt", "count", "mode"],
                        },
                    }
                },
                "required": ["requests"],
            },
        },
    }]

    SYSTEM = (
        "你是游戏美术素材自动化生成管线的任务解析器。"
        "把用户的一句话需求拆成结构化生成任务，全部通过调用 generate_assets 工具返回。"
        "prompt 必须是英文、具体、可直接用于 Stable Diffusion 的提示词，包含主体、风格、构图、光影。"
        "mode 选择规则：全新创作用 txt2img；在已有图上重绘/翻新用 img2img；"
        "给定风格参考图做风格迁移用 ipadapter。"
        "count 是每个任务需要生成的张数。若用户提到参考图，填 ref_image。"
        "不要输出任何额外解释，只调用工具。"
    )

    def __init__(self, cfg=None, api_key=None, client=None):
        self.cfg = cfg or {}
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        a = self.cfg.get("agent", {})
        self.model = a.get("model", "deepseek-chat")
        self.base_url = a.get("base_url", "https://api.deepseek.com")
        self.temperature = a.get("temperature", 0.2)
        self.max_retries = a.get("max_retries", 3)
        self._client = client

    def _get_client(self):
        if self._client is not None:
            return self._client
        from openai import OpenAI
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def _call_llm(self, req_text):
        client = self._get_client()
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": self.SYSTEM},
                      {"role": "user", "content": req_text}],
            tools=self.TOOLS,
            tool_choice="auto",
            temperature=self.temperature,
        )
        return resp.choices[0].message

    def parse_requirement(self, req_text, max_retries=None):
        max_retries = max_retries if max_retries is not None else self.max_retries
        for attempt in range(max_retries + 1):
            try:
                msg = self._call_llm(req_text)
                tasks = self._extract_tasks(msg)
                if tasks is None:
                    raise ValueError("模型未返回合法任务 JSON")
                return tasks
            except Exception:
                if attempt >= max_retries:
                    raise
                time.sleep(2 ** attempt)  # 指数退避
        return []

    def _extract_tasks(self, msg):
        args = None
        for tc in getattr(msg, "tool_calls", None) or []:
            if getattr(tc.function, "name", "") == "generate_assets":
                args = tc.function.arguments
                break
        if args is None:
            args = getattr(msg, "content", None) or ""
        data = self._parse_json(args)
        if data is None:
            return None
        reqs = data.get("requests") if isinstance(data, dict) else data
        if not isinstance(reqs, list) or not reqs:
            return None
        out = []
        for r in reqs:
            if not isinstance(r, dict):
                return None
            prompt = (r.get("prompt") or "").strip()
            if not prompt:
                return None
            try:
                count = int(r.get("count", 1))
            except (TypeError, ValueError):
                return None
            mode = r.get("mode", "txt2img")
            ref_image = r.get("ref_image")
            if count < 1 or mode not in {"txt2img", "img2img", "ipadapter"}:
                return None
            if mode in {"img2img", "ipadapter"} and not ref_image:
                return None
            out.append({
                "prompt": prompt,
                "count": count,
                "mode": mode,
                "ref_image": ref_image,
            })
        return out if out else None

    @staticmethod
    def _parse_json(s):
        s = (s or "").strip()
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
        try:
            return json.loads(s)
        except Exception:
            m = re.search(r"\{.*\}", s, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    return None
            return None


# ---------------------------------------------------------------------------
# Scheduler（ComfyUI API：提交 / 轮询 / 下载）
# ---------------------------------------------------------------------------
class Scheduler:
    def __init__(self, cfg):
        import requests
        self._requests = requests
        c = cfg["comfyui"]
        self.base_url = c["base_url"].rstrip("/")
        self.workflows_dir = Path(c["workflows_dir"])
        self.input_dir = Path(c.get("input_dir") or "") if c.get("input_dir") else None
        self.max_poll_sec = c.get("max_poll_sec", 180)
        self.poll_interval = c.get("poll_interval", 2)

    def load_template(self, workflow_name):
        return json.loads((self.workflows_dir / f"{workflow_name}.json").read_text(encoding="utf-8"))

    @staticmethod
    def fill_template(template, params):
        t = copy.deepcopy(template)
        for node in t["prompt"].values():
            for k, v in node["inputs"].items():
                if isinstance(v, str):
                    m = PLACEHOLDER_RE.match(v)
                    if m and m.group(1) in params:
                        node["inputs"][k] = params[m.group(1)]
        return t

    def submit(self, workflow_name, params):
        template = self.load_template(workflow_name)
        prompt = self.fill_template(template, params)["prompt"]
        payload = {"prompt": prompt}
        r = self._requests.post(f"{self.base_url}/prompt", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()["prompt_id"]

    def poll(self, prompt_id, timeout=None, interval=None):
        timeout = timeout or self.max_poll_sec
        interval = interval or self.poll_interval
        start = time.time()
        while time.time() - start < timeout:
            r = self._requests.get(f"{self.base_url}/history/{prompt_id}", timeout=10)
            hist = r.json()
            if prompt_id in hist:
                entry = hist[prompt_id]
                if entry.get("outputs"):
                    return entry["outputs"]
                if entry.get("status", {}).get("status_str") == "error":
                    messages = entry.get("status", {}).get("messages", [])
                    execution_errors = [
                        payload for kind, payload in messages
                        if kind == "execution_error" and isinstance(payload, dict)
                    ]
                    detail = execution_errors[-1] if execution_errors else entry.get("status", {})
                    raise RuntimeError(
                        "ComfyUI 执行出错: "
                        + json.dumps(detail, ensure_ascii=False, indent=2)
                    )
            time.sleep(interval)
        raise TimeoutError(f"prompt {prompt_id} 轮询超时({timeout}s)")

    def download_images(self, outputs, save_dir, prefix):
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for out in outputs.values():
            for img in out.get("images", []):
                params = {"filename": img["filename"],
                          "subfolder": img.get("subfolder", ""),
                          "type": img.get("type", "output")}
                r = self._requests.get(f"{self.base_url}/view", params=params, timeout=30)
                r.raise_for_status()
                p = save_dir / f"{prefix}_{img['filename']}"
                p.write_bytes(r.content)
                paths.append(p)
        return paths

    def prepare_ref(self, ref_name):
        """把参考图放进 ComfyUI input 目录，返回文件名。ref_name 是服务器上的路径或 input 目录内文件名。"""
        if not self.input_dir:
            raise RuntimeError("config.comfyui.input_dir 未配置，无法使用参考图")
        self.input_dir.mkdir(parents=True, exist_ok=True)
        src = Path(ref_name)
        if src.is_file() and src.resolve().parent != self.input_dir.resolve():
            dst = self.input_dir / src.name
            shutil.copy2(src, dst)
            return dst.name
        candidate = self.input_dir / src.name
        if candidate.is_file():
            return candidate.name
        raise FileNotFoundError(f"参考图不存在: {ref_name}（也未在 ComfyUI input 目录找到）")


# ---------------------------------------------------------------------------
# QualityGate（CLIP-Score 文图对齐质量门）
# ---------------------------------------------------------------------------
class QualityGate:
    def __init__(self, cfg=None, threshold=None, device=None):
        self.threshold = threshold if threshold is not None else (cfg or {}).get("quality", {}).get("threshold", 0.30)
        self._clip = None
        self._preprocess = None
        self._tokenizer = None
        self._device = device

    def _load(self):
        if self._clip is not None:
            return
        import torch
        import open_clip
        if self._device is None:
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        model, _, preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k", device=self._device)
        tokenizer = open_clip.get_tokenizer("ViT-B-32")
        model.eval()
        self._clip, self._preprocess, self._tokenizer = model, preprocess, tokenizer

    def score(self, image_path, prompt):
        import torch
        from PIL import Image
        self._load()
        image = self._preprocess(Image.open(image_path).convert("RGB")).unsqueeze(0).to(self._device)
        text = self._tokenizer([prompt]).to(self._device)
        with torch.no_grad():
            img_feat = self._clip.encode_image(image)
            txt_feat = self._clip.encode_text(text)
            img_feat = img_feat / img_feat.norm(dim=-1, keepdim=True)
            txt_feat = txt_feat / txt_feat.norm(dim=-1, keepdim=True)
            return float((img_feat @ txt_feat.T).item())

    def retry(self, score):
        """分数低于阈值 → 需要重试。"""
        return score < self.threshold


# ---------------------------------------------------------------------------
# PostProcess（超分，走 wf_post 工作流；当前工作流不包含去背景节点）
# ---------------------------------------------------------------------------
class PostProcess:
    def __init__(self, sched, cfg):
        self.sched = sched
        self.cfg = cfg

    def run(self, image_path, batch_dir, prefix):
        pp = self.cfg.get("postprocess", {})
        if not self.sched.input_dir:
            return [image_path]
        self.sched.input_dir.mkdir(parents=True, exist_ok=True)
        name = f"{prefix}_src.png"
        shutil.copy2(image_path, self.sched.input_dir / name)
        wf_params = {
            "IMAGE": name,
            "REMBG_MODEL": pp.get("rembg_model", "u2net"),
            "UPSCALE_MODEL": pp.get("upscale_model", "RealESRGAN_x4plus.pth"),
            "PREFIX": prefix,
        }
        pid = self.sched.submit("wf_post", wf_params)
        outputs = self.sched.poll(pid)
        out_dir = Path(batch_dir) / "_post"
        return self.sched.download_images(outputs, out_dir, prefix)


# ---------------------------------------------------------------------------
# Deliverer（结构化交付）
# ---------------------------------------------------------------------------
class Deliverer:
    def __init__(self, cfg):
        self.out_root = Path(cfg["deliver"]["out_root"])

    def new_batch(self, batch_id=None):
        batch_id = batch_id or time.strftime("batch_%Y%m%d_%H%M%S")
        d = self.out_root / batch_id
        d.mkdir(parents=True, exist_ok=True)
        return d, batch_id

    def write_manifest(self, batch_dir, records):
        (Path(batch_dir) / "manifest.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_state(self, batch_dir, state):
        (Path(batch_dir) / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def publish_asset(batch_dir, image_path, prefix):
        """把最终图片发布到批次根目录，返回 manifest 使用的相对文件名。"""
        src = Path(image_path)
        suffix = src.suffix or ".png"
        dst = Path(batch_dir) / f"{prefix}{suffix}"
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        return dst.name

    @staticmethod
    def load_state(batch_dir):
        p = Path(batch_dir) / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _wf_params(mode, gen, prompt, seed, ref_name, cfg):
    base = {
        "CHECKPOINT": cfg["comfyui"].get("checkpoint", "v1-5-pruned-emaonly.safetensors"),
        "LORA_NAME": gen.get("lora_name", "mtg_lora.safetensors"),
        "LORA_STRENGTH": gen.get("lora_strength", 0.8),
        "POSITIVE": prompt,
        "NEGATIVE": gen.get("negative_prompt",
                            "lowres, bad anatomy, bad hands, text, watermark, worst quality, low quality, blurry"),
        "WIDTH": gen.get("width", 512),
        "HEIGHT": gen.get("height", 768),
        "BATCH": 1,
        "SEED": seed,
        "STEPS": gen.get("steps", 28),
        "CFG": gen.get("cfg", 7.0),
        "SAMPLER": gen.get("sampler", "euler"),
        "SCHEDULER": gen.get("scheduler", "normal"),
    }
    if mode == "txt2img":
        return "wf_txt2img", base
    if mode == "img2img":
        return "wf_img2img", {**base, "IMAGE": ref_name, "DENOISE": gen.get("img2img_denoise", 0.6)}
    if mode == "ipadapter":
        return "wf_ipadapter", {
            **base, "IMAGE": ref_name,
            "CLIP_VISION": cfg.get("comfyui", {}).get("clip_vision", "CLIP-ViT-H.safetensors"),
            "IPADAPTER": cfg.get("comfyui", {}).get("ipadapter", "ip-adapter_sd15.safetensors"),
            "IPADAPTER_WEIGHT": cfg.get("comfyui", {}).get("ipadapter_weight", 0.8),
        }
    raise ValueError(f"未知 mode: {mode}")


def _gen_one(sched, gate, post, batch_dir, mode, prompt, ref_name, cfg, prefix):
    total_started = time.perf_counter()
    gen = cfg["generation"]
    q = cfg["quality"]
    rec = {"mode": mode, "prompt": prompt, "attempts": [], "gate_score": None,
           "final_score": None, "seed": None, "status": "ok", "image": None}
    seed = random.randint(0, 2 ** 31 - 1)
    tmp_dir = Path(batch_dir) / "_tmp"
    for attempt in range(q["max_retries"] + 1):
        wf, params = _wf_params(mode, gen, prompt, seed, ref_name, cfg)
        params["PREFIX"] = f"{prefix}_{seed}"
        generation_started = time.perf_counter()
        try:
            pid = sched.submit(wf, params)
            outputs = sched.poll(pid)
            imgs = sched.download_images(outputs, tmp_dir, f"{prefix}_{seed}")
            if not imgs:
                raise RuntimeError("ComfyUI 已完成任务，但没有返回图片")
        except Exception as e:
            rec["attempts"].append({
                "seed": seed,
                "error": str(e),
                "generation_sec": round(time.perf_counter() - generation_started, 4),
            })
            seed += 1
            if attempt < q["max_retries"]:
                continue
            rec["status"] = "failed"
            rec["total_sec"] = round(time.perf_counter() - total_started, 4)
            return rec
        img = imgs[0]
        generation_sec = time.perf_counter() - generation_started
        score_started = time.perf_counter()
        try:
            score = gate.score(img, prompt)
        except Exception as e:
            rec["attempts"].append({
                "seed": seed,
                "generation_sec": round(generation_sec, 4),
                "quality_error": str(e),
            })
            rec["status"] = "failed"
            rec["total_sec"] = round(time.perf_counter() - total_started, 4)
            return rec
        rec["attempts"].append({
            "seed": seed,
            "score": round(score, 4),
            "generation_sec": round(generation_sec, 4),
            "score_sec": round(time.perf_counter() - score_started, 4),
        })
        if gate.retry(score) and attempt < q["max_retries"]:
            seed += 1
            continue
        rec["gate_score"] = round(score, 4)
        rec["seed"] = seed
        try:
            rec["raw_image"] = str(Path(img).relative_to(batch_dir))
        except ValueError:
            rec["raw_image"] = str(img)
        if rec["gate_score"] < q["threshold"]:
            rec["status"] = "low_quality"  # 留档不删
        post_started = time.perf_counter()
        try:
            post_imgs = post.run(img, batch_dir, f"{prefix}_{seed}")
            if not post_imgs:
                raise RuntimeError("后处理工作流没有返回图片")
            final_img = post_imgs[0]
        except Exception as e:
            rec["postprocess_error"] = str(e)
            final_img = img
        rec["postprocess_sec"] = round(time.perf_counter() - post_started, 4)
        rec["image"] = Deliverer.publish_asset(batch_dir, final_img, prefix)
        published = Path(batch_dir) / rec["image"]
        final_score_started = time.perf_counter()
        try:
            rec["final_score"] = round(gate.score(published, prompt), 4)
            rec["final_score_sec"] = round(time.perf_counter() - final_score_started, 4)
            rec["final_quality_pass"] = rec["final_score"] >= q["threshold"]
        except Exception as e:
            rec["final_score_error"] = str(e)
            rec["final_quality_pass"] = None
        rec["total_sec"] = round(time.perf_counter() - total_started, 4)
        return rec
    return rec


def run_tasks(tasks, cfg, batch_id=None):
    """执行已经结构化的任务；供 Agent 主流程和可复现实验共用。"""
    deli = Deliverer(cfg)
    worker_urls = cfg["comfyui"].get("workers") or [cfg["comfyui"]["base_url"]]
    contexts = []
    for worker_index, worker_url in enumerate(worker_urls):
        worker_cfg = copy.deepcopy(cfg)
        worker_cfg["comfyui"]["base_url"] = worker_url
        scheduler = Scheduler(worker_cfg)
        quality_device = f"cuda:{worker_index}" if len(worker_urls) > 1 else None
        gate = QualityGate(worker_cfg, device=quality_device)
        contexts.append((scheduler, gate, PostProcess(scheduler, worker_cfg), worker_url))

    batch_dir, batch_id = deli.new_batch(batch_id)
    state = deli.load_state(batch_dir)
    records_by_id = {
        record["task_id"]: record
        for record in state.get("records", [])
        if record.get("task_id")
    }
    done = set(state.get("done", []))
    done.difference_update(
        item_id for item_id, record in records_by_id.items()
        if record.get("status") == "failed"
    )

    jobs = []
    reference_scheduler = contexts[0][0]
    for ti, task in enumerate(tasks):
        mode = task.get("mode", "txt2img")
        prompt = task["prompt"]
        count = int(task.get("count", 1))
        ref_name = None
        if task.get("ref_image"):
            ref_name = reference_scheduler.prepare_ref(task["ref_image"])
        for i in range(count):
            item_id = f"{ti}_{i}"
            if item_id in done:
                continue
            prefix = f"{batch_id}_{mode}_{ti}_{i}"
            jobs.append((item_id, prefix, mode, prompt, ref_name))

    available_workers = queue.Queue()
    for worker_index in range(len(contexts)):
        available_workers.put(worker_index)

    def execute_job(job):
        item_id, prefix, mode, prompt, ref_name = job
        worker_index = available_workers.get()
        scheduler, gate, post, worker_url = contexts[worker_index]
        try:
            record = _gen_one(
                scheduler, gate, post, batch_dir, mode, prompt, ref_name, cfg, prefix
            )
            record["task_id"] = item_id
            record["worker_url"] = worker_url
            return item_id, record
        finally:
            available_workers.put(worker_index)

    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(contexts)) as executor:
            futures = [executor.submit(execute_job, job) for job in jobs]
            for future in concurrent.futures.as_completed(futures):
                item_id, rec = future.result()
                records_by_id[item_id] = rec
                if rec.get("status") == "failed":
                    done.discard(item_id)  # 下次用同 batch_id 时重试失败项
                else:
                    done.add(item_id)
                records = list(records_by_id.values())
                deli.save_state(batch_dir, {
                    "records": records,
                    "done": sorted(done),
                    "task_count": len(tasks),
                    "tasks": tasks,
                    "workers": worker_urls,
                })

    records = list(records_by_id.values())
    deli.write_manifest(batch_dir, records)
    return batch_dir, records


def run_batch(req_text, cfg, batch_id=None):
    agent = DeepSeekAgent(cfg)
    tasks = agent.parse_requirement(req_text)
    return run_tasks(tasks, cfg, batch_id=batch_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def load_config(path=None):
    import yaml
    p = Path(path) if path else BASE_DIR / "config.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg.setdefault("comfyui", {})
    cfg.setdefault("agent", {})
    cfg.setdefault("quality", {})
    cfg.setdefault("generation", {})
    cfg.setdefault("postprocess", {})
    cfg.setdefault("deliver", {})
    cfg["comfyui"]["workflows_dir"] = str((BASE_DIR / cfg["comfyui"]["workflows_dir"]).resolve())
    cfg["deliver"]["out_root"] = str((BASE_DIR / cfg["deliver"]["out_root"]).resolve())
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env")
    except Exception:
        pass
    workers_env = os.environ.get("COMFYUI_WORKERS", "").strip()
    if workers_env:
        cfg["comfyui"]["workers"] = [
            url.strip().rstrip("/") for url in workers_env.split(",") if url.strip()
        ]
    return cfg


def main():
    ap = argparse.ArgumentParser(description="游戏素材自动化生成管线编排器")
    ap.add_argument("cmd", choices=["parse", "run"])
    ap.add_argument("--req", help="一句话需求")
    ap.add_argument("--config", default=None)
    ap.add_argument("--input-dir", help="ComfyUI input 目录（覆盖 config）")
    ap.add_argument("--batch-id", help="指定批次 ID；与已有批次同名时按 state.json 断点续跑")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.input_dir:
        cfg["comfyui"]["input_dir"] = args.input_dir
    req = args.req or input("请输入素材需求: ").strip()

    if args.cmd == "parse":
        tasks = DeepSeekAgent(cfg).parse_requirement(req)
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
    else:
        batch_dir, records = run_batch(req, cfg, batch_id=args.batch_id)
        ok = sum(1 for r in records if r["status"] == "ok")
        print(f"完成批次: {batch_dir}")
        print(f"任务: {len(records)} 条，状态 ok: {ok}")
        print(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
