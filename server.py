#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qwen 手机远程出图 · 中转服务

用 ComfyUI 便携版自带的 python_embeded 运行,不需要安装任何额外依赖。

职责:
  1. 给手机提供一个简单网页(文生图 / 多图修图 / 记录)
  2. 把手机发来的提示词和图片,填进已验证的 ComfyUI 工作流模板
  3. 单任务排队执行,实时汇报进度,完成后把图传回手机
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import secrets
import sys
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("QR_CONFIG") or (ROOT / "config.json"))
STATIC_DIR = ROOT / "static"
WORK_DIR = ROOT / "work"
JOBS_FILE = WORK_DIR / "jobs.json"
DEVICES_FILE = WORK_DIR / "devices.json"
DEVICES: dict[str, dict] = {}
WORK_DIR.mkdir(exist_ok=True)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qwen-remote")

SESSION_COOKIE = "qr_auth"
DEVICE_COOKIE = "qr_device"


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_FILE.exists() and CONFIG_FILE == ROOT / "config.json":
        example = ROOT / "config.example.json"
        if example.exists():
            CONFIG_FILE.write_text(example.read_text("utf-8"), "utf-8")
            log.warning("没有 config.json,已按 config.example.json 生成一份,记得填密钥")
    cfg = json.loads(CONFIG_FILE.read_text("utf-8"))
    if not cfg.get("access_token"):
        cfg["access_token"] = secrets.token_urlsafe(9)
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    return cfg


CFG = load_config()
COMFY = CFG["comfy_url"].rstrip("/")
INPUT_DIR = Path(CFG["comfy_input_dir"])
DEFAULTS = CFG["defaults"]


def expected_cookie() -> str:
    key = str(CFG["access_token"]).encode()
    return hmac.new(b"qwen-remote", key, hashlib.sha256).hexdigest()


def device_of(request: web.Request) -> str:
    """区分不同登录设备,历史记录是否互相可见由 history_scope 决定。"""
    return request.cookies.get(DEVICE_COOKIE) or ""


def history_scope() -> str:
    return str(CFG.get("history_scope", "shared")).lower()


def save_config() -> None:
    CONFIG_FILE.write_text(json.dumps(CFG, ensure_ascii=False, indent=2), "utf-8")


def job_visible(job: "Job", request: web.Request) -> bool:
    """device 模式下,别人设备的任务一律当作不存在。"""
    if history_scope() != "device":
        return True
    if not job.device:
        return True
    return job.device == device_of(request)


def is_authed(request: web.Request) -> bool:
    if request.cookies.get(SESSION_COOKIE) == expected_cookie():
        return True
    supplied = request.headers.get("X-Token") or request.query.get("token")
    return bool(supplied) and hmac.compare_digest(str(supplied), str(CFG["access_token"]))


def require_auth(handler):
    async def wrapper(request: web.Request):
        if not is_authed(request):
            raise web.HTTPUnauthorized(text="未登录或口令错误")
        return await handler(request)

    return wrapper


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------

JOBS: dict[str, "Job"] = {}
QUEUE: asyncio.Queue | None = None
PHONE_SOCKETS: dict[web.WebSocketResponse, str] = {}
STATE: dict = {"current": None, "progress": None, "comfy_online": False, "last_error": None}


class Job:
    def __init__(self, kind: str, params: dict):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.params = params
        self.status = "queued"
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.prompt_id: str | None = None
        self.images: list[dict] = []
        self.text: str | None = None
        self.device: str = ""
        self.error: str | None = None
        self.cancel = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "images": self.images,
            "text": self.text,
            "device": self.device,
            "params": {
                k: v for k, v in self.params.items() if k != "image_count"
            } | ({"image_count": self.params.get("image_count")} if self.kind == "edit" else {}),
            "progress": STATE["progress"] if STATE.get("current") == self.id else None,
        }


def recent_jobs(limit: int, device: str | None = None) -> list[dict]:
    """提示词增强属于临时工具,不进历史列表。

    device 不为空时只返回这台设备的任务(history_scope = device)。
    """
    items = [
        j
        for j in JOBS.values()
        if j.kind != "pe" and (device is None or not j.device or j.device == device)
    ]
    items.sort(key=lambda j: j.created, reverse=True)
    return [j.to_dict() for j in items[:limit]]


def save_jobs() -> None:
    keep = int(CFG.get("keep_jobs", 100))
    items = [
        j
        for j in sorted(JOBS.values(), key=lambda x: x.created, reverse=True)
        if j.kind != "pe"
    ][:keep]
    try:
        JOBS_FILE.write_text(
            json.dumps([j.to_dict() for j in items], ensure_ascii=False), "utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("保存历史失败: %s", exc)


def load_jobs() -> None:
    if not JOBS_FILE.exists():
        return
    try:
        for item in json.loads(JOBS_FILE.read_text("utf-8")):
            job = Job(item["kind"], item.get("params") or {})
            job.id = item["id"]
            job.status = item["status"] if item["status"] in ("done", "error") else "error"
            job.created = item.get("created") or time.time()
            job.started = item.get("started")
            job.finished = item.get("finished")
            job.images = item.get("images") or []
            job.device = item.get("device") or ""
            job.error = item.get("error")
            JOBS[job.id] = job
    except Exception as exc:  # noqa: BLE001
        log.warning("读取历史失败: %s", exc)


# --------------------------------------------------------------------------
# 工作流模板(真实节点与接线已在本机逐一验证)
# --------------------------------------------------------------------------

def _models() -> dict:
    """三个加载器 + 可选的推理加速节点。"""
    m = CFG["models"]
    graph = {
        "unet": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": m["unet"]}},
        "clip": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": m["clip"], "type": "qwen_image", "device": "default"},
        },
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
    }
    acc = CFG.get("acceleration") or {}
    if acc.get("enabled", False):
        graph["tspeed"] = {
            "class_type": "TESpeedQwenImage21",
            "inputs": {
                "model": ["unet", 0],
                "attention": acc.get("attention", "kitchen_int8"),
                "step_cache": acc.get("step_cache", "te_predictor"),
                "reuse_threshold": acc.get("reuse_threshold", 0.06),
                "start_percent": acc.get("start_percent", 0.0),
                "end_percent": acc.get("end_percent", 0.0),
                "predictor_error_limit": acc.get("predictor_error_limit", 0.08),
                "verbose": bool(acc.get("verbose", True)),
            },
        }
        graph["cache"] = {
            "class_type": "QwenImage21Cache",
            "inputs": {"model": ["tspeed", 0], "device": "auto", "dtype": "default"},
        }
    return graph


def model_source(graph: dict) -> list:
    """采样器该接哪个模型来源(开了加速就接加速链的末端)。"""
    return ["cache", 0] if "cache" in graph else ["unet", 0]


def build_t2i(p: dict) -> dict:
    g = _models()
    g["enc"] = {
        "class_type": "TextEncodeQwenImage21",
        "inputs": {
            "clip": ["clip", 0],
            "vae": ["vae", 0],
            "prompt": p["prompt"],
            "negative_prompt": p["negative"],
            "resolution": 1024,
        },
    }
    g["lat"] = {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": p["width"], "height": p["height"], "batch_size": 1},
    }
    g["ks"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": model_source(g),
            "positive": ["enc", 0],
            "negative": ["enc", 1],
            "latent_image": ["lat", 0],
            "seed": p["seed"],
            "steps": p["steps"],
            "cfg": p["cfg"],
            "sampler_name": p["sampler"],
            "scheduler": p["scheduler"],
            "denoise": 1.0,
        },
    }
    g["dec"] = {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}}
    g["save"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["dec", 0], "filename_prefix": "phone_t2i"},
    }
    return g


def build_edit(p: dict, filenames: list[str]) -> dict:
    g = _models()
    enc_inputs = {
        "clip": ["clip", 0],
        "vae": ["vae", 0],
        "prompt": p["prompt"],
        "negative_prompt": p["negative"],
        "resolution": 0,
    }
    for index, name in enumerate(filenames, start=1):
        g[f"img_{index}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g[f"scale_{index}"] = {
            "class_type": "ImageScaleToTotalPixels",
            "inputs": {
                "image": [f"img_{index}", 0],
                "upscale_method": "lanczos",
                "megapixels": p["megapixels"],
                "resolution_steps": 32,
            },
        }
        enc_inputs[f"images.image_{index}"] = [f"scale_{index}", 0]
    g["enc"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc_inputs}
    # 编辑模式用文本编码器吐出的参考潜变量作为起点,而不是空白画布
    g["ks"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": model_source(g),
            "positive": ["enc", 0],
            "negative": ["enc", 1],
            "latent_image": ["enc", 2],
            "seed": p["seed"],
            "steps": p["steps"],
            "cfg": p["cfg"],
            "sampler_name": p["sampler"],
            "scheduler": p["scheduler"],
            "denoise": 1.0,
        },
    }
    g["dec"] = {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}}
    g["save"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["dec", 0], "filename_prefix": "phone_edit"},
    }
    return g

def build_pe_graph(p: dict) -> dict:
    """提示词增强:调官方的 PE 模型,把一句话扩写成完整画面描述。"""
    cfg = CFG.get("prompt_enhancer") or {}
    inputs = {
        "输入提示词": p["prompt"],
        "任务模式": "图生图" if p.get("mode") == "edit" else "文生图",
        "输出语言": p.get("lang") or cfg.get("language", "中文"),
        "增强方式": cfg.get("engine", "本地官方PE"),
        "api_key": "",
        "api_base_url": cfg.get("api_base_url", "https://teynex.com"),
        "model": cfg.get("api_model", "deepseek-v4.1-flash"),
        "文生图PE模型": cfg["t2i_model"],
        "图生图PE模型": cfg["i2i_model"],
        "主模型": cfg["main_model"],
        "mmproj": cfg.get("mmproj", "无"),
        "最大生成token": int(cfg.get("max_tokens", 2048)),
        "上下文长度": int(cfg.get("context", 8192)),
        "seed": p.get("seed", 0),
        "生成后自动卸载模型": bool(cfg.get("unload_after", True)),
        "启用思考": bool(cfg.get("think", False)),
    }
    graph: dict = {
        "pe": {"class_type": "TE_Qwen_Image_2_1_Prompt_Enhancer", "inputs": inputs},
        "disp": {"class_type": "TE_text_display", "inputs": {"text": ["pe", 0]}},
    }
    # 注意:第一个参考图的输入名是「图片」,从第二个起才带序号
    for index, name in enumerate(p.get("filenames") or [], start=1):
        graph[f"pe_img_{index}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        key = "图片" if index == 1 else f"图片{index}"
        inputs[key] = [f"pe_img_{index}", 0]
    return graph


def extract_text(entry: dict) -> str | None:
    """从执行结果里取出节点输出的文本。"""
    for node_output in (entry.get("outputs") or {}).values():
        texts = node_output.get("text")
        if isinstance(texts, list) and texts:
            joined = "\n".join(str(item) for item in texts).strip()
            if joined:
                return joined
    return None


# --------------------------------------------------------------------------
# ComfyUI 客户端
# --------------------------------------------------------------------------

class ComfyClient:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.client_id = uuid.uuid4().hex

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=120)
        )

    async def stop(self) -> None:
        if self.session:
            await self.session.close()

    async def submit(self, graph: dict) -> str:
        assert self.session
        payload = {"prompt": graph, "client_id": self.client_id}
        try:
            async with self.session.post(f"{COMFY}/prompt", json=payload) as resp:
                data = await resp.json()
        except aiohttp.ClientConnectorError as exc:
            raise RuntimeError("连不上 ComfyUI,请确认 run_nvidia_gpu.bat 还开着") from exc
        if resp.status != 200:
            raise RuntimeError(f"ComfyUI 拒绝了任务: {json.dumps(data, ensure_ascii=False)[:400]}")
        return data["prompt_id"]

    async def history(self, prompt_id: str) -> dict | None:
        assert self.session
        async with self.session.get(f"{COMFY}/history/{prompt_id}") as resp:
            data = await resp.json()
        return data.get(prompt_id)

    async def fetch_image(self, filename: str, subfolder: str, kind: str) -> bytes:
        assert self.session
        params = {"filename": filename, "subfolder": subfolder, "type": kind}
        async with self.session.get(f"{COMFY}/view", params=params) as resp:
            if resp.status != 200:
                raise web.HTTPNotFound(text="图片已不在磁盘上")
            return await resp.read()

    async def interrupt(self) -> None:
        assert self.session
        try:
            async with self.session.post(f"{COMFY}/interrupt") as resp:
                await resp.read()
        except Exception:  # noqa: BLE001
            pass

    async def stats(self) -> dict | None:
        assert self.session
        try:
            async with self.session.get(f"{COMFY}/system_stats", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return await resp.json()
        except Exception:  # noqa: BLE001
            return None


comfy = ComfyClient()


# --------------------------------------------------------------------------
# 手机端实时推送
# --------------------------------------------------------------------------

async def broadcast() -> None:
    if not PHONE_SOCKETS:
        return
    scoped = history_scope() == "device"
    cache: dict[str, str] = {}
    for ws, device in list(PHONE_SOCKETS.items()):
        if ws.closed:
            PHONE_SOCKETS.pop(ws, None)
            continue
        key = device if scoped else ""
        if key not in cache:
            cache[key] = json.dumps(
                {
                    "type": "state",
                    "current": STATE["current"],
                    "progress": STATE["progress"],
                    "comfy_online": STATE["comfy_online"],
                    "queued": QUEUE.qsize() if QUEUE else 0,
                    "jobs": recent_jobs(10, device if scoped else None),
                },
                ensure_ascii=False,
            )
        try:
            await ws.send_str(cache[key])
        except Exception:  # noqa: BLE001
            PHONE_SOCKETS.pop(ws, None)


async def comfy_listener() -> None:
    """订阅 ComfyUI 的进度广播。"""
    while True:
        if not comfy.session:
            await asyncio.sleep(1)
            continue
        try:
            url = f"{COMFY}/ws?clientId={comfy.client_id}"
            async with comfy.session.ws_connect(url, heartbeat=20) as ws:
                if not STATE["comfy_online"]:
                    STATE["comfy_online"] = True
                    log.info("已连上 ComfyUI")
                    await broadcast()
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    kind = data.get("type")
                    body = data.get("data") or {}
                    if kind == "progress":
                        if STATE["current"]:
                            STATE["progress"] = {
                                "value": body.get("value"),
                                "max": body.get("max"),
                            }
                            await broadcast()
                    elif kind == "executing":
                        if body.get("node") is None and STATE["current"]:
                            STATE["progress"] = None
                            await broadcast()
                    elif kind == "execution_error":
                        STATE["last_error"] = body.get("exception_message")
        except Exception as exc:  # noqa: BLE001
            if STATE["comfy_online"]:
                STATE["comfy_online"] = False
                log.info("ComfyUI 连接断开(%s),稍后重试", type(exc).__name__)
                await broadcast()
            await asyncio.sleep(3)


# --------------------------------------------------------------------------
# 完成通知(服务端推送,手机锁屏也能收到)
# --------------------------------------------------------------------------

NOTIFY_CHANNELS = (
    "wecom",      # 企业微信群机器人
    "feishu",     # 飞书自定义机器人
    "dingtalk",   # 钉钉机器人
    "serverchan", # Server酱(推送到微信)
    "pushplus",   # PushPlus(推送到微信)
    "bark",       # Bark(iOS)
    "ntfy",       # ntfy(安卓)
    "custom",     # 自定义 webhook
)


def notify_config() -> dict:
    return CFG.get("notify") or {}


def load_devices() -> None:
    global DEVICES
    if not DEVICES_FILE.exists():
        return
    try:
        data = json.loads(DEVICES_FILE.read_text("utf-8"))
        if isinstance(data, dict):
            DEVICES = {str(k): v for k, v in data.items() if isinstance(v, dict)}
            log.info("已载入 %d 台设备的设置", len(DEVICES))
    except Exception as exc:  # noqa: BLE001
        log.warning("读取设备设置失败: %s", exc)


def save_devices() -> None:
    try:
        DEVICES_FILE.write_text(
            json.dumps(DEVICES, ensure_ascii=False, indent=2), "utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("保存设备设置失败: %s", exc)


def mask_secret(text: str) -> str:
    text = str(text or "")
    if not text:
        return ""
    if len(text) <= 10:
        return "*" * len(text)
    return f"{text[:6]}…{text[-4:]}"


def device_notify_conf(device: str) -> dict:
    """设备自己配了通知就用设备自己的,否则回落全局配置。"""
    own = (DEVICES.get(device) or {}).get("notify") or {}
    if own.get("channel") and (own.get("token") or own.get("webhook")):
        merged = dict(own)
        merged["enabled"] = True
        return merged
    fallback = str(CFG.get("notify", {}).get("fallback_to_global", True)).lower()
    if fallback in ("false", "0", "no"):
        return {}
    return notify_config()


def device_notify_summary(device: str) -> dict:
    own = (DEVICES.get(device) or {}).get("notify") or {}
    active = bool(own.get("channel") and (own.get("token") or own.get("webhook")))
    return {
        "channel": own.get("channel") or "",
        "token_set": bool(own.get("token")),
        "token_hint": mask_secret(own.get("token") or ""),
        "webhook": own.get("webhook") or "",
        "on_success": bool(own.get("on_success", True)),
        "on_failure": bool(own.get("on_failure", True)),
        "using_global": not active,
        "global_channel": notify_config().get("channel") or "none",
    }


def notify_ready(conf: dict | None = None) -> bool:
    conf = notify_config() if conf is None else conf
    if not conf.get("enabled"):
        return False
    channel = str(conf.get("channel") or "none").lower()
    if channel not in NOTIFY_CHANNELS:
        return False
    if channel in ("wecom", "feishu", "dingtalk", "custom"):
        # 这几个渠道必须填机器人地址
        return bool(conf.get("webhook"))
    if channel == "serverchan":
        # 新老版本的推送地址不一样,填了完整地址就优先用它
        return bool(conf.get("token") or conf.get("webhook"))
    return bool(conf.get("token"))


def build_notify_request(
    channel: str, title: str, body: str, job: "Job | None", conf: dict | None = None
) -> tuple | None:
    """按渠道拼出请求,统一用 (method, url, kwargs) 表示。"""
    conf = notify_config() if conf is None else conf
    text = f"{title}\n{body}"
    if channel in ("wecom", "dingtalk"):
        url = str(conf.get("webhook") or "").strip()
        if not url:
            return None
        return "POST", url, {"json": {"msgtype": "text", "text": {"content": text}}}
    if channel == "feishu":
        url = str(conf.get("webhook") or "").strip()
        if not url:
            return None
        return "POST", url, {"json": {"msg_type": "text", "content": {"text": text}}}
    if channel == "serverchan":
        key = str(conf.get("token") or "").strip()
        url = str(conf.get("webhook") or "").strip()
        if not url:
            if not key:
                return None
            # 新版 Server酱³ 的 key 形如 sctp1234tXXXX,地址里要带上编号
            matched = re.match(r"^sctp(\d+)t", key)
            if matched:
                url = f"https://{matched.group(1)}.push.ft07.com/send/{key}.send"
            else:
                # 老版 Turbo 的固定格式
                url = f"https://sctapi.ftqq.com/{key}.send"
        return "POST", url, {"data": {"title": title, "desp": body}}
    if channel == "pushplus":
        key = str(conf.get("token") or "").strip()
        if not key:
            return None
        url = str(conf.get("webhook") or "https://www.pushplus.plus/send").strip()
        return "POST", url, {
            "json": {"token": key, "title": title, "content": body}
        }
    if channel == "bark":
        key = str(conf.get("token") or "").strip()
        server = str(conf.get("webhook") or "https://api.day.app").strip().rstrip("/")
        if not key:
            return None
        return "POST", f"{server}/{key}", {"json": {"title": title, "body": body}}
    if channel == "ntfy":
        topic = str(conf.get("token") or "").strip()
        server = str(conf.get("webhook") or "https://ntfy.sh").strip().rstrip("/")
        if not topic:
            return None
        # 标题并进正文,避免 header 里出现中文
        return "POST", f"{server}/{topic}", {"data": text.encode("utf-8")}
    if channel == "custom":
        url = str(conf.get("webhook") or "").strip()
        if not url:
            return None
        payload: dict = {"title": title, "body": body}
        if job is not None:
            payload["job"] = {
                "id": job.id,
                "kind": job.kind,
                "status": job.status,
                "prompt": job.params.get("prompt") or "",
                "steps": job.params.get("steps"),
                "images": [img.get("filename") for img in job.images],
            }
        return "POST", url, {"json": payload}
    return None


async def send_notification(
    title: str, body: str, job: "Job | None" = None, conf: dict | None = None
) -> bool:
    if not notify_ready(conf):
        return False
    active = notify_config() if conf is None else conf
    channel = str(active.get("channel") or "").lower()
    spec = build_notify_request(channel, title, body, job, active)
    if spec is None:
        log.warning("通知渠道 %s 配置不完整,已跳过", channel)
        return False
    method, url, kwargs = spec
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, **kwargs) as resp:
                detail = await resp.text()
        if resp.status >= 300:
            log.warning("通知发送失败(%s %s):%s", resp.status, channel, detail[:200])
            return False
        log.info("已发送通知(%s)", channel)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("通知发送出错:%s", exc)
        return False


async def notify_job_finished(job: Job) -> None:
    """任务结束后推一条消息。取消和提示词增强不打扰用户。"""
    conf = device_notify_conf(job.device)
    if job.kind == "pe" or job.cancel or not notify_ready(conf):
        return
    if job.status == "done":
        if not conf.get("on_success", True):
            return
        kind = "改图" if job.kind == "edit" else "文生图"
        seconds = (job.finished - job.started) if (job.finished and job.started) else 0
        lines = [f"{kind} · {job.params.get('steps')} 步 · 用时 {seconds:.0f} 秒"]
        prompt = (job.params.get("prompt") or "").strip().replace("\n", " ")
        if conf.get("include_prompt", True) and prompt:
            if len(prompt) > 60:
                prompt = prompt[:60] + "…"
            lines.append(prompt)
        lines.append("打开出图助手查看")
        await send_notification("✅ 出图完成", "\n".join(lines), job, conf)
    elif job.status == "error":
        if not conf.get("on_failure", True):
            return
        await send_notification("⚠️ 生成失败", str(job.error or "未知原因"), job, conf)


# --------------------------------------------------------------------------
# 执行队列(同一时刻只跑一个,避免显存爆掉)
# --------------------------------------------------------------------------

async def run_job(job: Job) -> None:
    if job.kind == "t2i":
        graph = build_t2i(job.params)
    elif job.kind == "edit":
        graph = build_edit(job.params, job.params["filenames"])
    elif job.kind == "pe":
        graph = build_pe_graph(job.params)
    else:
        raise RuntimeError(f"未知任务类型:{job.kind}")

    job.prompt_id = await comfy.submit(graph)
    STATE["progress"] = {"value": 0, "max": job.params.get("steps", 1)}
    await broadcast()

    deadline = time.time() + float(CFG.get("job_timeout_sec", 1800))
    while True:
        if job.cancel:
            await comfy.interrupt()
            raise RuntimeError("已取消")
        if time.time() > deadline:
            await comfy.interrupt()
            raise RuntimeError("超时,已中断")
        entry = await comfy.history(job.prompt_id)
        if entry is not None:
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                messages = status.get("messages") or []
                detail = ""
                for item in messages:
                    if isinstance(item, list) and len(item) > 1 and item[0] == "execution_error":
                        detail = (item[1] or {}).get("exception_message") or ""
                raise RuntimeError(detail or "ComfyUI 执行出错")
            if job.kind == "pe":
                text = extract_text(entry)
                if not text:
                    raise RuntimeError("提示词增强没有返回内容")
                job.text = text
                return
            images: list[dict] = []
            for node_output in (entry.get("outputs") or {}).values():
                for image in node_output.get("images") or []:
                    images.append(
                        {
                            "filename": image.get("filename"),
                            "subfolder": image.get("subfolder") or "",
                            "type": image.get("type") or "output",
                        }
                    )
            if images:
                job.images = images
                return
            raise RuntimeError("执行结束但没有产出图片")
        await asyncio.sleep(1.5)


async def worker() -> None:
    assert QUEUE
    while True:
        job_id = await QUEUE.get()
        job = JOBS.get(job_id)
        if job is None or job.status != "queued":
            continue
        STATE["current"] = job.id
        job.status = "running"
        job.started = time.time()
        await broadcast()
        try:
            await run_job(job)
            job.status = "done"
            log.info("任务 %s 完成,用时 %.1f 秒", job.id, time.time() - job.started)
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = str(exc)
            log.warning("任务 %s 失败: %s", job.id, exc)
        finally:
            job.finished = time.time()
            STATE["current"] = None
            STATE["progress"] = None
            save_jobs()
            try:
                cleanup_outputs()
            except Exception as exc:  # noqa: BLE001
                log.warning("清理输出目录出错: %s", exc)
            await broadcast()
            try:
                await notify_job_finished(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("发送通知出错: %s", exc)


# --------------------------------------------------------------------------
# 参数处理
# --------------------------------------------------------------------------

def _clamp_int(value, low: int, high: int, fallback: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _snap32(value: int) -> int:
    return max(256, min(4096, int(round(value / 32) * 32)))


def parse_params(src: dict, kind: str) -> dict:
    prompt = str(src.get("prompt") or "").strip()
    if not prompt:
        raise web.HTTPBadRequest(text="提示词不能为空")
    if len(prompt) > 4000:
        raise web.HTTPBadRequest(text="提示词太长了")

    seed = src.get("seed")
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1
    if seed < 0:
        seed = random.randint(1, 2**31 - 1)

    params = {
        "prompt": prompt,
        "negative": str(src.get("negative") or "").strip(),
        "steps": _clamp_int(src.get("steps"), 1, 60, DEFAULTS["steps"]),
        "cfg": float(src.get("cfg") or DEFAULTS["cfg"]),
        "sampler": str(src.get("sampler") or DEFAULTS["sampler_name"]),
        "scheduler": str(src.get("scheduler") or DEFAULTS["scheduler"]),
        "seed": seed,
    }
    if kind == "t2i":
        params["width"] = _snap32(_clamp_int(src.get("width"), 256, 4096, DEFAULTS["width"]))
        params["height"] = _snap32(_clamp_int(src.get("height"), 256, 4096, DEFAULTS["height"]))
    else:
        params["megapixels"] = max(0.25, min(4.0, float(src.get("megapixels") or DEFAULTS["megapixels"])))
    return params


async def save_upload(part, index: int) -> str:
    raw = await part.read(decode=False)
    limit = int(CFG.get("max_upload_mb", 20)) * 1024 * 1024
    if len(raw) > limit:
        raise web.HTTPBadRequest(text=f"第 {index} 张图超过 {CFG.get('max_upload_mb')}MB")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
        image = ImageOps.exif_transpose(image)
    except Exception as exc:  # noqa: BLE001
        raise web.HTTPBadRequest(
            text=f"第 {index} 张图无法识别。请用 JPG / PNG / WEBP;iPhone 的 HEIC 格式请先转成 JPG"
        ) from exc

    edge = int(CFG.get("max_input_edge", 2048))
    if max(image.size) > edge:
        image.thumbnail((edge, edge), Image.LANCZOS)

    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    stamp = f"phone_{int(time.time())}_{index}_{secrets.token_hex(3)}"
    if has_alpha:
        name = f"{stamp}.png"
        image.convert("RGBA").save(INPUT_DIR / name, "PNG")
    else:
        name = f"{stamp}.jpg"
        image.convert("RGB").save(INPUT_DIR / name, "JPEG", quality=95)
    return name


# --------------------------------------------------------------------------
# 网页接口
# --------------------------------------------------------------------------

async def handle_index(request: web.Request) -> web.Response:
    path = STATIC_DIR / "index.html"
    return web.Response(
        body=path.read_bytes(), content_type="text/html", charset="utf-8"
    )


async def handle_login(request: web.Request) -> web.Response:
    body = await request.json()
    token = str(body.get("token") or "")
    if not hmac.compare_digest(token, str(CFG["access_token"])):
        await asyncio.sleep(0.5)
        raise web.HTTPUnauthorized(text="口令不对")
    response = web.json_response({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        expected_cookie(),
        max_age=60 * 60 * 24 * 180,
        httponly=True,
        samesite="Lax",
    )
    device = request.cookies.get(DEVICE_COOKIE) or secrets.token_hex(8)
    response.set_cookie(
        DEVICE_COOKIE,
        device,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="Lax",
    )
    return response


async def handle_logout(request: web.Request) -> web.Response:
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE)
    return response


@require_auth
async def handle_state(request: web.Request) -> web.Response:
    stats = await comfy.stats()
    vram = None
    if stats:
        device = (stats.get("devices") or [{}])[0]
        total = device.get("vram_total") or 0
        free = device.get("vram_free") or 0
        if total:
            vram = round(100 * (1 - free / total))
    return web.json_response(
        {
            "authenticated": True,
            "comfy_online": STATE["comfy_online"],
            "vram_used_percent": vram,
            "queued": QUEUE.qsize() if QUEUE else 0,
            "current": STATE["current"],
            "progress": STATE["progress"],
            "defaults": DEFAULTS,
            "max_images": int(CFG.get("max_images_per_job", 6)),
            "jobs": recent_jobs(30, device_of(request) if history_scope() == "device" else None),
        }
    )


async def handle_ping(request: web.Request) -> web.Response:
    return web.json_response({"authenticated": is_authed(request)})


@require_auth
async def handle_t2i(request: web.Request) -> web.Response:
    body = await request.json()
    params = parse_params(body, "t2i")
    job = Job("t2i", params)
    job.device = device_of(request)
    JOBS[job.id] = job
    await QUEUE.put(job.id)
    save_jobs()
    await broadcast()
    log.info("新任务 %s(文生图):%s", job.id, params["prompt"][:40])
    return web.json_response(job.to_dict())


@require_auth
async def handle_edit(request: web.Request) -> web.Response:
    if not (request.content_type or "").startswith("multipart/"):
        raise web.HTTPBadRequest(text="请至少上传一张参考图")
    reader = await request.multipart()
    fields: dict[str, str] = {}
    filenames: list[str] = []
    max_images = int(CFG.get("max_images_per_job", 6))
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "images":
            if len(filenames) >= max_images:
                raise web.HTTPBadRequest(text=f"最多同时 {max_images} 张图")
            filenames.append(await save_upload(part, len(filenames) + 1))
        else:
            fields[part.name] = (await part.text()).strip()

    if not filenames:
        raise web.HTTPBadRequest(text="请至少上传一张图")

    params = parse_params(fields, "edit")
    params["filenames"] = filenames
    params["image_count"] = len(filenames)
    job = Job("edit", params)
    job.device = device_of(request)
    JOBS[job.id] = job
    await QUEUE.put(job.id)
    save_jobs()
    await broadcast()
    log.info("新任务 %s(修图 %d 张):%s", job.id, len(filenames), params["prompt"][:40])
    return web.json_response(job.to_dict())

@require_auth
async def handle_enhance(request: web.Request) -> web.Response:
    """提示词增强:接受纯文本,也可以带上参考图(改图模式下更准)。"""
    if not (CFG.get("prompt_enhancer") or {}).get("enabled", True):
        raise web.HTTPBadRequest(text="提示词增强没启用,可以在 config.json 里打开")

    filenames: list[str] = []
    if (request.content_type or "").startswith("multipart/"):
        reader = await request.multipart()
        source: dict = {}
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "images":
                if len(filenames) < 3:
                    filenames.append(await save_upload(part, len(filenames) + 1))
            else:
                source[part.name] = (await part.text()).strip()
    else:
        source = await request.json()

    prompt = str(source.get("prompt") or "").strip()
    if not prompt:
        raise web.HTTPBadRequest(text="先写一句话,AI 再帮你扩写")
    if len(prompt) > 2000:
        raise web.HTTPBadRequest(text="提示词太长了,先精简一下再增强")
    if str(source.get("mode")) == "edit" and not filenames:
        raise web.HTTPBadRequest(text="改图的扩写要先选一张参考图,AI 才能看懂你想改哪里")

    params = {
        "prompt": prompt,
        "mode": "edit" if str(source.get("mode")) == "edit" else "t2i",
        "lang": str(source.get("lang") or "中文"),
        "seed": random.randint(1, 2**31 - 1),
        "filenames": filenames,
        "steps": 1,
    }
    job = Job("pe", params)
    job.device = device_of(request)
    JOBS[job.id] = job
    await QUEUE.put(job.id)
    await broadcast()
    log.info("新任务 %s(提示词增强):%s", job.id, prompt[:30])
    return web.json_response(job.to_dict())


@require_auth
async def handle_job(request: web.Request) -> web.Response:
    job = JOBS.get(request.match_info["job_id"])
    if job is None or not job_visible(job, request):
        raise web.HTTPNotFound(text="没有这个任务")
    return web.json_response(job.to_dict())


@require_auth
async def handle_cancel(request: web.Request) -> web.Response:
    job = JOBS.get(request.match_info["job_id"])
    if job is None or not job_visible(job, request):
        raise web.HTTPNotFound(text="没有这个任务")
    job.cancel = True
    if job.status == "queued":
        job.status = "error"
        job.error = "已取消"
        save_jobs()
    await broadcast()
    return web.json_response(job.to_dict())


@require_auth
async def handle_clear(request: web.Request) -> web.Response:
    """清空记录。device 模式下只清本设备的,不碰别人设备。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    delete_files = bool(body.get("delete_files"))
    device = device_of(request)
    scoped = history_scope() == "device"

    targets = [
        job
        for job in JOBS.values()
        if job.kind != "pe" and (not scoped or not job.device or job.device == device)
    ]

    removed_files = 0
    out_dir = Path(CFG["comfy_output_dir"]) if CFG.get("comfy_output_dir") else None
    if delete_files and out_dir and out_dir.is_dir():
        for job in targets:
            for img in job.images:
                name = img.get("filename") or ""
                # 只删工具自己生成的文件
                if not name.startswith(("phone_t2i_", "phone_edit_")):
                    continue
                path = out_dir / name
                try:
                    if path.is_file():
                        path.unlink()
                        removed_files += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("删除图片 %s 失败: %s", name, exc)

    for job in targets:
        JOBS.pop(job.id, None)

    save_jobs()
    await broadcast()
    log.info(
        "清空记录:%d 条%s",
        len(targets),
        f",并删除 {removed_files} 张图" if delete_files else "",
    )
    return web.json_response({"ok": True, "cleared": len(targets), "files": removed_files})


@require_auth
async def handle_file(request: web.Request) -> web.Response:
    job = JOBS.get(request.match_info["job_id"])
    if job is None or not job_visible(job, request):
        raise web.HTTPNotFound(text="没有这个任务")
    try:
        index = int(request.match_info["index"])
        meta = job.images[index]
    except (KeyError, ValueError, IndexError):
        raise web.HTTPNotFound(text="没有这张图")
    data = await comfy.fetch_image(meta["filename"], meta["subfolder"], meta["type"])

    thumb = request.query.get("max")
    if thumb:
        try:
            limit = max(64, min(2048, int(thumb)))
            image = Image.open(io.BytesIO(data))
            image.thumbnail((limit, limit), Image.LANCZOS)
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, "JPEG", quality=88)
            data = buffer.getvalue()
            return web.Response(body=data, content_type="image/jpeg")
        except Exception:  # noqa: BLE001
            pass
    return web.Response(body=data, content_type="image/png")


@require_auth
async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    PHONE_SOCKETS[ws] = device_of(request)
    try:
        await ws.send_str(json.dumps({"type": "hello", "ok": True}))
        await broadcast()
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        PHONE_SOCKETS.pop(ws, None)
    return ws


def cleanup_old_uploads() -> None:
    """清掉手机上传的临时图,避免 input 目录无限膨胀。"""
    days = float(CFG.get("clean_uploads_after_days", 7))
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        for item in INPUT_DIR.glob("phone_*"):
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("清理旧上传失败: %s", exc)
    if removed:
        log.info("已清理 %d 个过期上传文件", removed)


def cleanup_outputs() -> None:
    """清理工具自己生成的旧图,避免 output 目录无限膨胀。

    只动 phone_t2i_ / phone_edit_ 前缀的文件,不碰你自己手动出的图。
    """
    conf = CFG.get("output_cleanup") or {}
    if not conf.get("enabled", True):
        return
    raw = CFG.get("comfy_output_dir")
    if not raw:
        return
    out_dir = Path(raw)
    if not out_dir.is_dir():
        return

    keep = int(conf.get("keep", 300))
    max_age_days = float(conf.get("max_age_days", 30) or 0)
    cutoff = time.time() - max_age_days * 86400 if max_age_days > 0 else 0.0

    files: list[Path] = []
    for pattern in ("phone_t2i_*", "phone_edit_*"):
        files.extend(item for item in out_dir.glob(pattern) if item.is_file())
    if not files:
        return
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    removed: set[str] = set()
    for index, path in enumerate(files):
        expired = bool(cutoff) and path.stat().st_mtime < cutoff
        if index < keep and not expired:
            continue
        try:
            path.unlink()
            removed.add(path.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("清理旧图 %s 失败: %s", path.name, exc)

    if not removed:
        return
    log.info("已清理 %d 张旧图(保留最近 %d 张 / %g 天)", len(removed), keep, max_age_days)
    for job in JOBS.values():
        if job.images and any(img.get("filename") in removed for img in job.images):
            job.images = [img for img in job.images if img.get("filename") not in removed]
    save_jobs()


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPRequestEntityTooLarge:
        limit = body_limit_bytes() // (1024 * 1024)
        raise web.HTTPRequestEntityTooLarge(
            max_size=body_limit_bytes(),
            text=f"上传内容太大了(上限 {limit}MB)。少选几张图,或者换小一点的图再试",
        )
    except web.HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("处理 %s 出错", request.path)
        return web.Response(text=f"服务器出错:{exc}", status=500)


def body_limit_bytes() -> int:
    """一次请求最多允许多大的体积(手机可能一次传好几张图)。"""
    explicit = int(CFG.get("max_body_mb", 0) or 0)
    if explicit > 0:
        return explicit * 1024 * 1024
    per_image = int(CFG.get("max_upload_mb", 10))
    count = int(CFG.get("max_images_per_job", 6)) + 1
    return (per_image * count + 8) * 1024 * 1024


ADMIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>出图助手 · 主机设置</title>
<style>
 body{margin:0;padding:28px;background:#0f1115;color:#e8eaf0;
      font:15px/1.6 -apple-system,"Microsoft YaHei",system-ui,sans-serif}
 .card{max-width:520px;margin:0 auto;background:#181b22;border:1px solid #2b3040;
       border-radius:16px;padding:22px}
 h1{font-size:19px;margin:0 0 4px}
 p.sub{color:#98a0b3;font-size:13px;margin:0 0 6px}
 label{display:block;font-size:13px;color:#98a0b3;margin:16px 0 6px}
 input{width:100%;box-sizing:border-box;padding:11px 13px;border-radius:10px;
       border:1px solid #2b3040;background:#202430;color:#e8eaf0;font:inherit}
 button{margin-top:18px;width:100%;padding:13px;border:none;border-radius:10px;
        background:linear-gradient(135deg,#6c8cff,#9b6cff);color:#fff;
        font:600 15px -apple-system,"Microsoft YaHei",sans-serif;cursor:pointer}
 #msg{font-size:13px;margin-top:12px;min-height:18px}
 .tip{color:#e0a03c;font-size:12px;margin-top:18px;line-height:1.6}
 code{background:#202430;padding:2px 6px;border-radius:6px}
</style></head><body>
<div class="card">
  <h1>主机设置</h1>
  <p class="sub">这个页面只能在运行服务的这台电脑上打开。</p>
  <label>当前访问口令</label>
  <input value="__TOKEN__" readonly onclick="this.select()">
  <label>新口令(至少 6 位)</label>
  <input id="new" placeholder="输入新口令,然后点保存">
  <button onclick="saveToken()">保存并立即生效</button>
  <div id="msg"></div>
  <div class="tip">
    保存后手机上需要重新登录。也可以直接改 <code>config.json</code> 里的
    <code>access_token</code>,或者在命令行执行
    <code>python server.py --set-token 新口令</code>。
  </div>
</div>
<script>
async function saveToken(){
  const input = document.getElementById('new');
  const msg = document.getElementById('msg');
  const token = input.value.trim();
  msg.textContent = '';
  if (token.length < 6) { msg.style.color = '#e05c5c'; msg.textContent = '口令至少 6 位'; return; }
  const res = await fetch('/admin/token', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token: token})
  });
  const body = await res.text();
  if (res.ok) {
    msg.style.color = '#3fbf7f';
    msg.textContent = '已保存,新口令是:' + token;
    document.querySelector('input[readonly]').value = token;
    input.value = '';
  } else {
    msg.style.color = '#e05c5c';
    msg.textContent = body;
  }
}
</script></body></html>"""


def escape_attr(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def is_local(request: web.Request) -> bool:
    return (request.remote or "") in ("127.0.0.1", "::1", "localhost")


async def handle_admin(request: web.Request) -> web.Response:
    if not is_local(request):
        raise web.HTTPForbidden(text="这个页面只能在运行服务的电脑上打开")
    page = ADMIN_PAGE.replace("__TOKEN__", escape_attr(CFG["access_token"]))
    return web.Response(text=page, content_type="text/html", charset="utf-8")


async def handle_admin_token(request: web.Request) -> web.Response:
    if not is_local(request):
        raise web.HTTPForbidden(text="只能在这台电脑上修改口令")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(text="请求格式不对")
    token = str(body.get("token") or "").strip()
    if len(token) < 6:
        raise web.HTTPBadRequest(text="口令至少 6 位")
    if len(token) > 64:
        raise web.HTTPBadRequest(text="口令最长 64 位")
    if token == CFG["access_token"]:
        return web.json_response({"ok": True, "token": token, "unchanged": True})
    CFG["access_token"] = token
    save_config()
    log.info("访问口令已更新,所有设备需要重新登录")
    return web.json_response({"ok": True, "token": token})


async def handle_notify_test(request: web.Request) -> web.Response:
    conf = device_notify_conf(device_of(request))
    if not notify_ready(conf):
        raise web.HTTPBadRequest(
            text="这台设备还没配置通知。点右上角设置,选一个渠道并填好密钥"
        )
    ok = await send_notification(
        "🔔 出图助手 · 测试通知",
        "看到这条消息就说明这台设备的通知配置成功了。",
        None,
        conf,
    )
    if not ok:
        raise web.HTTPInternalServerError(text="发送失败,请检查密钥或地址是否正确")
    return web.json_response({"ok": True, "channel": conf.get("channel")})


@require_auth
async def handle_settings_get(request: web.Request) -> web.Response:
    return web.json_response(device_notify_summary(device_of(request)))


@require_auth
async def handle_settings_post(request: web.Request) -> web.Response:
    device = device_of(request)
    if not device:
        raise web.HTTPBadRequest(text="这台设备没有标识,请退出后重新登录一次")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(text="请求格式不对")

    channel = str(body.get("channel") or "").strip().lower()
    if channel and channel not in NOTIFY_CHANNELS:
        raise web.HTTPBadRequest(text="不认识的渠道")

    entry = DEVICES.setdefault(device, {})
    conf = dict(entry.get("notify") or {})
    conf["channel"] = channel
    if "token" in body:
        conf["token"] = str(body.get("token") or "").strip()
    if "webhook" in body:
        conf["webhook"] = str(body.get("webhook") or "").strip()
    conf["on_success"] = bool(body.get("on_success", True))
    conf["on_failure"] = bool(body.get("on_failure", True))

    if channel and not (conf.get("token") or conf.get("webhook")):
        raise web.HTTPBadRequest(text="请填写密钥或推送地址")

    entry["notify"] = conf
    entry["updated"] = time.time()
    save_devices()
    log.info(
        "设备 %s 的通知设置已更新:channel=%s", device[:8] or "(无标识)", channel or "关闭"
    )
    return web.json_response(device_notify_summary(device))


@require_auth
async def handle_notify_test_auth(request: web.Request) -> web.Response:
    return await handle_notify_test(request)


def build_app() -> web.Application:
    app = web.Application(
        middlewares=[error_middleware], client_max_size=body_limit_bytes()
    )
    app.router.add_get("/", handle_index)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/api/ping", handle_ping)
    app.router.add_get("/admin", handle_admin)
    app.router.add_post("/admin/token", handle_admin_token)
    app.router.add_get("/api/state", handle_state)
    app.router.add_post("/api/t2i", handle_t2i)
    app.router.add_post("/api/edit", handle_edit)
    app.router.add_post("/api/enhance", handle_enhance)
    app.router.add_get("/api/job/{job_id}", handle_job)
    app.router.add_post("/api/job/{job_id}/cancel", handle_cancel)
    app.router.add_post("/api/clear", handle_clear)
    app.router.add_post("/api/notify/test", handle_notify_test_auth)
    app.router.add_get("/api/settings", handle_settings_get)
    app.router.add_post("/api/settings", handle_settings_post)
    app.router.add_get("/api/file/{job_id}/{index}", handle_file)
    app.router.add_get("/api/ws", handle_ws)
    return app


def write_connect_info(urls: list[str]) -> None:
    """同时写一份连接信息到文件,万一控制台中文显示乱码也能查。"""
    lines = ["出图助手 · 连接信息", ""]
    lines.append("手机访问地址(需与电脑同一 WiFi):")
    lines.extend(f"    {u}" for u in urls)
    lines.extend(["", f"访问口令: {CFG['access_token']}", ""])
    lines.append("Phone URL / Access token:")
    lines.extend(f"    {u}" for u in urls)
    lines.append(f"    token: {CFG['access_token']}")
    try:
        (WORK_DIR / "connect.txt").write_text("\n".join(lines) + "\n", "utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("写入连接信息失败: %s", exc)


def local_ips() -> list[str]:
    import socket

    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except Exception:  # noqa: BLE001
        pass
    return sorted(ip for ip in found if not ip.startswith("127."))


async def main() -> None:
    global QUEUE
    QUEUE = asyncio.Queue()
    load_jobs()
    load_devices()
    cleanup_old_uploads()
    cleanup_outputs()
    await comfy.start()

    app = build_app()
    app["worker"] = asyncio.create_task(worker())
    app["listener"] = asyncio.create_task(comfy_listener())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(CFG["port"]))
    await site.start()

    urls = [f"http://{ip}:{CFG['port']}" for ip in local_ips()] or [
        f"http://127.0.0.1:{CFG['port']}"
    ]
    log.info("=" * 60)
    log.info("  手机访问地址 / Phone URL :")
    for url in urls:
        log.info("      %s", url)
    log.info("  访问口令 / Access token : %s", CFG["access_token"])
    log.info("  主机设置页 / Admin      : http://127.0.0.1:%s/admin", CFG["port"])
    log.info("=" * 60)
    write_connect_info(urls)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await comfy.stop()


def run_cli() -> bool:
    """几个简单的命令行操作,方便直接在主机上改配置。"""
    args = sys.argv[1:]
    if not args:
        return False
    if args[0] in ("--set-token", "-t") and len(args) > 1:
        token = args[1].strip()
        if len(token) < 6:
            print("口令至少 6 位")
            return True
        CFG["access_token"] = token
        save_config()
        print("访问口令已更新:", token)
        return True
    if args[0] in ("--show-token", "-s"):
        print("当前访问口令:", CFG["access_token"])
        return True
    if args[0] in ("--help", "-h"):
        print("用法:")
        print("  python server.py                     启动服务")
        print("  python server.py --show-token        查看当前口令")
        print("  python server.py --set-token 新口令   修改访问口令")
        return True
    return False


if __name__ == "__main__":
    if run_cli():
        sys.exit(0)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
