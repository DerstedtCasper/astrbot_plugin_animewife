from astrbot.api.all import *
from astrbot.api.star import StarTools
from datetime import datetime, timedelta
import random
import os
import json
import aiohttp
import asyncio
import tempfile
import time

# ==================== 常量定义 ====================

PLUGIN_DIR = StarTools.get_data_dir("astrbot_plugin_animewife")
CONFIG_DIR = os.path.join(PLUGIN_DIR, "config")
IMG_DIR = os.path.join(PLUGIN_DIR, "img", "wife")

# 确保目录存在
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)

# 数据文件路径
RECORDS_FILE = os.path.join(CONFIG_DIR, "records.json")
SWAP_REQUESTS_FILE = os.path.join(CONFIG_DIR, "swap_requests.json")
NTR_STATUS_FILE = os.path.join(CONFIG_DIR, "ntr_status.json")
BACKPACKS_KEY = "__wife_backpacks__"
# 记录“今日老婆”在背包中的绑定槽位（用于换老婆/发老婆时同步更新同一槽位）
BACKPACK_TODAY_SLOT_KEY = "__wife_backpack_today_slot__"

# 仅允许这些后缀用于从本地文件系统读取，避免路径穿越/任意文件读取
ALLOWED_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# 网络请求超时（避免外部 HTTP 卡住协程）
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
WIFE_LIST_CACHE_TTL_SECONDS = 30 * 60
WIFE_LIST_RETRY_COOLDOWN_SECONDS = 60

# ==================== 全局数据存储 ====================

records = {  # 统一的记录数据结构
    "ntr": {},        # 牛老婆记录
    "change": {},     # 换老婆记录
    "reset": {},      # 重置使用次数
    "swap": {}        # 交换老婆请求次数
}
swap_requests = {}  # 交换请求数据
ntr_statuses = {}  # NTR 开关状态

# ==================== 并发锁 ====================

config_locks = {}      # 群组配置锁
records_lock = asyncio.Lock()  # 记录数据锁
swap_lock = asyncio.Lock()     # 交换请求锁
ntr_lock = asyncio.Lock()      # NTR 状态锁


def get_config_lock(group_id: str) -> asyncio.Lock:
    """获取或创建群组配置锁"""
    if group_id not in config_locks:
        config_locks[group_id] = asyncio.Lock()
    return config_locks[group_id]

def get_today():
    """获取当前上海时区日期字符串"""
    utc_now = datetime.utcnow()
    return (utc_now + timedelta(hours=8)).date().isoformat()


def load_json(path: str) -> dict:
    """安全加载 JSON 文件"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        # 兼容带 BOM 的旧文件
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            return {}
    except Exception:
        return {}


def save_json(path: str, data: dict) -> None:
    """保存数据到 JSON 文件（原子写入，避免半写入导致配置损坏）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = None
    try:
        # 使用同目录临时文件，确保 os.replace 原子替换
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(path),
            delete=False,
        ) as f:
            tmp_path = f.name
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                # 某些环境下 fsync 可能不可用，忽略但仍保持原子替换
                pass
        os.replace(tmp_path, path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def normalize_img_id(img: str) -> str | None:
    """规范化图片标识，拒绝绝对路径/路径穿越/非图片后缀。"""
    if not isinstance(img, str):
        return None
    s = img.strip()
    if not s:
        return None
    # 防止把 URL 当作文件名
    if "://" in s:
        return None
    s = s.replace("\\", "/").lstrip("/").lstrip("\\")
    norm = os.path.normpath(s)
    # 统一使用 URL 风格的分隔符，避免 Windows 下反斜杠进入 URL
    norm = norm.replace("\\", "/")
    # 拒绝 Windows 盘符/驱动器相对路径等形式
    if ":" in norm:
        return None
    if os.path.isabs(norm):
        return None
    if norm == ".." or norm.startswith("../") or norm.startswith(".."):
        return None
    ext = os.path.splitext(norm)[1].lower()
    if ext not in ALLOWED_IMG_EXTS:
        return None
    return norm


def safe_img_path(img: str) -> str | None:
    """安全拼接本地图片路径：确保最终路径仍在 IMG_DIR 内。"""
    rel = normalize_img_id(img)
    if not rel:
        return None
    base = os.path.abspath(IMG_DIR)
    cand = os.path.abspath(os.path.join(IMG_DIR, rel))
    try:
        if os.path.commonpath([base, cand]) != base:
            return None
    except Exception:
        return None
    return cand


def extract_today_wife(wife_data: object, today: str) -> tuple[str | None, str | None]:
    """从 cfg[uid] 中提取今日老婆 (img, owner_name)。返回 (None, None) 表示无效/过期。"""
    # 兼容旧格式 list: [img, date, nick]
    if isinstance(wife_data, list) and len(wife_data) >= 2:
        if wife_data[1] != today:
            return None, None
        img = wife_data[0] if isinstance(wife_data[0], str) and wife_data[0] else None
        owner = wife_data[2] if len(wife_data) > 2 and isinstance(wife_data[2], str) and wife_data[2] else None
        return img, owner

    # 新格式 dict（注意：若今日老婆是“背包槽位引用”，此函数无法解析 slot -> img，仅用于无槽位/临时态场景）
    if isinstance(wife_data, dict):
        if wife_data.get("date") != today:
            return None, None
        img = wife_data.get("img")
        owner = wife_data.get("nick")
        return (img, owner) if isinstance(img, str) and img else (None, owner if isinstance(owner, str) else None)

    return None, None


def get_cfg_nick(cfg: dict, uid: str, default: str | None = None) -> str:
    data = cfg.get(uid)
    if isinstance(data, list) and len(data) > 2 and isinstance(data[2], str) and data[2]:
        return data[2]
    if isinstance(data, dict) and isinstance(data.get("nick"), str) and data.get("nick"):
        return data.get("nick")
    return default or str(uid)


def load_group_config(group_id: str) -> dict:
    """加载群组配置"""
    return load_json(os.path.join(CONFIG_DIR, f"{group_id}.json"))


def save_group_config(group_id: str, config: dict) -> None:
    """保存群组配置"""
    save_json(os.path.join(CONFIG_DIR, f"{group_id}.json"), config)


def normalize_backpack(raw: object, size: int) -> list:
    """将背包槽位标准化为固定长度 list[entry|None]，并兼容旧格式。"""
    if size <= 0:
        return []

    items: list = []
    if isinstance(raw, list):
        normalized = []
        for x in raw:
            if not x:
                normalized.append(None)
            elif isinstance(x, str):
                normalized.append(x)
            elif isinstance(x, dict) and isinstance(x.get("img"), str) and x.get("img"):
                # {"img": "...", "note": "..."}
                normalized.append({"img": x.get("img"), "note": x.get("note")})
            else:
                normalized.append(None)
        items = normalized
    elif isinstance(raw, dict):
        # 允许用 {"1": "xx.jpg"} 这种形式初始化/迁移
        items = [None] * size
        for k, v in raw.items():
            try:
                idx = int(k)
            except Exception:
                continue
            if 1 <= idx <= size and v:
                if isinstance(v, str):
                    items[idx - 1] = v
                elif isinstance(v, dict) and isinstance(v.get("img"), str) and v.get("img"):
                    items[idx - 1] = {"img": v.get("img"), "note": v.get("note")}
    else:
        items = []

    if len(items) < size:
        items.extend([None] * (size - len(items)))
    elif len(items) > size:
        items = items[:size]
    return items


def get_today_slot_marks(cfg: dict) -> dict:
    """获取背包今日槽位绑定表：{uid: {date, slot}}。"""
    marks = cfg.get(BACKPACK_TODAY_SLOT_KEY, {})
    return marks if isinstance(marks, dict) else {}


def _read_today_slot_mark(marks: dict, uid: str, today: str, size: int) -> int | None:
    rec = marks.get(uid)
    if not isinstance(rec, dict):
        return None
    if rec.get("date") != today:
        return None
    slot = rec.get("slot")
    if type(slot) is not int:
        return None
    if 1 <= slot <= size:
        return slot
    return None


def _infer_today_slot_from_items(items: list, img: str) -> int | None:
    """尝试从背包里推断“今日老婆槽位”（兼容旧数据：抽老婆已入库但未记录绑定）。"""
    if not img or not isinstance(img, str):
        return None
    matches = [
        i
        for i, entry in enumerate(items, start=1)
        if backpack_entry_to_img_note(entry)[0] == img
    ]
    return matches[0] if len(matches) == 1 else None


def bind_today_slot(cfg: dict, uid: str, today: str, slot: int) -> None:
    """写入“今日老婆”绑定槽位。"""
    marks = get_today_slot_marks(cfg)
    marks[uid] = {"date": today, "slot": int(slot)}
    cfg[BACKPACK_TODAY_SLOT_KEY] = marks


def first_empty_slot(items: list) -> int | None:
    """返回第一个空槽位（1-based），没有空位则返回 None。"""
    for i, x in enumerate(items, start=1):
        if not x:
            return i
        if isinstance(x, dict) and not x.get("img"):
            return i
    return None


def backpack_entry_to_img_note(entry: object) -> tuple[str | None, str | None]:
    if not entry:
        return None, None
    if isinstance(entry, str):
        return entry, None
    if isinstance(entry, dict):
        img = entry.get("img")
        note = entry.get("note")
        return (img, note) if isinstance(img, str) and img else (None, None)
    return None, None


def make_backpack_entry(img: str, note: str | None = None) -> object:
    if note:
        return {"img": img, "note": note}
    return img


def normalize_today_record(raw: object, today: str, *, nick_default: str | None = None) -> dict | None:
    """标准化“今日老婆”记录（兼容旧 list/dict）。"""
    if isinstance(raw, list) and len(raw) >= 2:
        if raw[1] != today:
            return None
        img = raw[0] if isinstance(raw[0], str) and raw[0] else None
        nick = raw[2] if len(raw) > 2 and isinstance(raw[2], str) and raw[2] else (nick_default or None)
        out = {"date": today, "nick": nick}
        if img:
            out["img"] = img
        return out

    if isinstance(raw, dict):
        if raw.get("date") != today:
            return None
        out: dict = {"date": today}
        if isinstance(raw.get("nick"), str) and raw.get("nick"):
            out["nick"] = raw.get("nick")
        elif nick_default:
            out["nick"] = nick_default
        if isinstance(raw.get("img"), str) and raw.get("img"):
            out["img"] = raw.get("img")
        if type(raw.get("slot")) is int:
            out["slot"] = raw.get("slot")
        return out

    return None


def get_user_backpack(cfg: dict, uid: str, size: int) -> tuple[dict, list]:
    backpacks = cfg.get(BACKPACKS_KEY, {})
    if not isinstance(backpacks, dict):
        backpacks = {}
    items = normalize_backpack(backpacks.get(uid), size)
    return backpacks, items


def clear_today_binding(cfg: dict, uid: str, today: str) -> bool:
    """清理今日绑定槽位标记（仅当标记属于 today 时清理）。"""
    marks = get_today_slot_marks(cfg)
    rec = marks.get(uid)
    if isinstance(rec, dict) and rec.get("date") == today:
        try:
            del marks[uid]
        except KeyError:
            pass
        cfg[BACKPACK_TODAY_SLOT_KEY] = marks
        return True
    cfg[BACKPACK_TODAY_SLOT_KEY] = marks
    return False


def set_today_entity_slot(cfg: dict, uid: str, today: str, nick: str, size: int, slot: int, img: str, *, note: str | None = None) -> None:
    """把“今日老婆实体 w”落到背包槽位，并让今日老婆位引用该槽位（w 仅存在一处）。"""
    backpacks, items = get_user_backpack(cfg, uid, size)
    if 1 <= slot <= size:
        items[slot - 1] = make_backpack_entry(img, note)
        backpacks[uid] = items
        cfg[BACKPACKS_KEY] = backpacks
        cfg[uid] = {"date": today, "slot": int(slot), "nick": nick}
        bind_today_slot(cfg, uid, today, int(slot))


def set_today_entity_unsaved(cfg: dict, uid: str, today: str, nick: str, img: str) -> None:
    """把“今日老婆实体 w”存为临时态（背包满/不入库时），w 仅存在于 cfg[uid]。"""
    cfg[uid] = {"date": today, "img": img, "nick": nick}
    clear_today_binding(cfg, uid, today)


def resolve_today_entity(cfg: dict, uid: str, today: str, size: int, *, nick_default: str | None = None) -> tuple[str | None, int | None, str | None, str | None, bool]:
    """
    解析“今日老婆实体 w”：
    - 若 w 在背包：返回 (img, slot, nick, note, changed)，其中 img 来自背包槽位
    - 若 w 为临时态：返回 (img, None, nick, None, changed)，其中 img 来自 cfg[uid]["img"]
    并在必要时做迁移/去重/修复（确保 w 同时只存在一处）。
    """
    raw = cfg.get(uid)
    rec = normalize_today_record(raw, today, nick_default=nick_default)
    if not rec:
        # 如果 cfg 没有今日老婆记录，尝试清理悬挂绑定
        changed = clear_today_binding(cfg, uid, today)
        return None, None, None, None, changed

    nick = rec.get("nick") if isinstance(rec.get("nick"), str) and rec.get("nick") else (nick_default or None)
    img_field = rec.get("img") if isinstance(rec.get("img"), str) and rec.get("img") else None
    raw_slot = rec.get("slot")
    explicit_slot = raw_slot if type(raw_slot) is int and 1 <= raw_slot <= size else None

    backpacks, items = get_user_backpack(cfg, uid, size)
    raw_backpacks = cfg.get(BACKPACKS_KEY)
    raw_items = raw_backpacks.get(uid) if isinstance(raw_backpacks, dict) else None
    mark_slot = _read_today_slot_mark(get_today_slot_marks(cfg), uid, today, size)

    def entry_at(slot: int | None) -> tuple[str | None, str | None]:
        if slot is None:
            return None, None
        return backpack_entry_to_img_note(items[slot - 1])

    explicit_img, _ = entry_at(explicit_slot)
    marked_img, _ = entry_at(mark_slot)
    resolved_slot: int | None = None

    if explicit_img:
        resolved_slot = explicit_slot
    elif marked_img and (not img_field or marked_img == img_field):
        resolved_slot = mark_slot
    elif img_field:
        resolved_slot = _infer_today_slot_from_items(items, img_field)

    if resolved_slot is not None:
        resolved_img, note = entry_at(resolved_slot)
        canonical_record = {"date": today, "slot": resolved_slot, "nick": nick}
        canonical_mark = {"date": today, "slot": resolved_slot}
        marks_raw = cfg.get(BACKPACK_TODAY_SLOT_KEY)
        old_mark = marks_raw.get(uid) if isinstance(marks_raw, dict) else None
        changed = (
            raw != canonical_record
            or raw_items != items
            or not isinstance(raw_backpacks, dict)
            or old_mark != canonical_mark
        )
        backpacks[uid] = items
        cfg[BACKPACKS_KEY] = backpacks
        cfg[uid] = canonical_record
        bind_today_slot(cfg, uid, today, resolved_slot)
        return resolved_img, resolved_slot, nick, note, changed

    if img_field:
        canonical_record = {"date": today, "img": img_field, "nick": nick}
        changed = raw != canonical_record or raw_items != items
        if raw_items != items or not isinstance(raw_backpacks, dict):
            backpacks[uid] = items
            cfg[BACKPACKS_KEY] = backpacks
        cfg[uid] = canonical_record
        if clear_today_binding(cfg, uid, today):
            changed = True
        return img_field, None, nick, None, changed

    try:
        del cfg[uid]
    except Exception:
        pass
    clear_today_binding(cfg, uid, today)
    return None, None, nick, None, True


def remove_today_entity(cfg: dict, uid: str, today: str, size: int) -> tuple[str | None, int | None, bool]:
    """删除“今日老婆实体 w”：清空背包槽位(若存在)并移除今日老婆引用。"""
    img, slot, nick, note, changed = resolve_today_entity(cfg, uid, today, size)
    if not img:
        return None, None, changed
    backpacks, items = get_user_backpack(cfg, uid, size)
    if slot is not None and 1 <= slot <= size:
        items[slot - 1] = None
        backpacks[uid] = items
        cfg[BACKPACKS_KEY] = backpacks
        changed = True
    try:
        del cfg[uid]
    except Exception:
        pass
    if clear_today_binding(cfg, uid, today):
        changed = True
    return img, slot, changed


def format_backpack_item(entry: object) -> str:
    img, note = backpack_entry_to_img_note(entry)
    if not img:
        return "(空)"
    name = format_wife_name(img)
    if note:
        name += f"（{note}）"
    return name


def normalize_cmd_text(text: str) -> str:
    """兼容 /!# 前缀唤醒模式：返回去掉前缀后的文本。"""
    s = (text or "").strip()
    if s and s[0] in ("/", "!", "#"):
        return s[1:].lstrip()
    return s


def format_wife_name(img: str) -> str:
    """将图片文件名转换为展示名。"""
    name = os.path.splitext(img)[0].split("/")[-1]
    if "!" in name:
        source, chara = name.split("!", 1)
        return f"《{source}》的{chara}"
    return name


def load_ntr_statuses():
    """加载 NTR 开关状态"""
    raw = load_json(NTR_STATUS_FILE)
    ntr_statuses.clear()
    ntr_statuses.update(raw)


def save_ntr_statuses():
    """保存 NTR 开关状态"""
    save_json(NTR_STATUS_FILE, ntr_statuses)


# ==================== 数据加载和保存函数 ====================

def load_records():
    """加载所有记录数据"""
    raw = load_json(RECORDS_FILE)
    records.clear()
    records.update({
        "ntr": raw.get("ntr", {}),
        "change": raw.get("change", {}),
        "reset": raw.get("reset", {}),
        "swap": raw.get("swap", {})
    })


def save_records():
    """保存所有记录数据"""
    save_json(RECORDS_FILE, records)


def load_swap_requests():
    """加载交换请求并清理过期数据"""
    raw = load_json(SWAP_REQUESTS_FILE)
    today = get_today()
    cleaned = {}
    
    if not isinstance(raw, dict):
        raw = {}

    for gid, reqs in raw.items():
        if not isinstance(reqs, dict):
            continue
        valid = {uid: rec for uid, rec in reqs.items() if isinstance(rec, dict) and rec.get("date") == today}
        if valid:
            cleaned[gid] = valid
    
    swap_requests.clear()
    swap_requests.update(cleaned)
    if raw != cleaned:
        save_json(SWAP_REQUESTS_FILE, cleaned)


def save_swap_requests():
    """保存交换请求"""
    save_json(SWAP_REQUESTS_FILE, swap_requests)


# 初始加载所有数据
load_records()
load_swap_requests()
load_ntr_statuses()

# ==================== 主插件类 ====================


@register(
    "astrbot_plugin_animewife",
    "DerstedtCasper",
    "群二次元老婆插件（自用改版）",
    "1.9.0",
    "https://github.com/DerstedtCasper/astrbot_plugin_animewife",
)
class WifePlugin(Star):
    """二次元老婆插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._http_session: aiohttp.ClientSession | None = None
        self._wife_list_cache: list[str] | None = None
        self._wife_list_cache_expires_at = 0.0
        self._wife_list_retry_after = 0.0
        self._wife_list_lock = asyncio.Lock()
        self._init_config()
        self._init_commands()
        self.admins = self.load_admins()

    def _init_config(self):
        """初始化配置参数"""
        self.need_prefix = self.config.get("need_prefix")
        self.ntr_max = self.config.get("ntr_max")
        self.ntr_possibility = self.config.get("ntr_possibility")
        self.change_max_per_day = self.config.get("change_max_per_day")
        self.swap_max_per_day = self.config.get("swap_max_per_day")
        self.reset_max_uses_per_day = self.config.get("reset_max_uses_per_day")
        self.reset_success_rate = self.config.get("reset_success_rate")
        self.reset_mute_duration = self.config.get("reset_mute_duration")
        self.image_base_url = self.config.get("image_base_url")
        self.image_list_url = self.config.get("image_list_url")
        try:
            self.backpack_size = max(1, int(self.config.get("backpack_size") or 7))
        except Exception:
            self.backpack_size = 7

    def _init_commands(self):
        """初始化命令映射表"""
        self.commands = {
            "老婆帮助": self.wife_help,
            "抽老婆": self.animewife,
            "查老婆": self.search_wife,
            "替换老婆": self.replace_wife,
            "老婆背包": self.show_backpack,
            "发老婆": self.send_wife,
            "牛老婆": self.ntr_wife,
            "重置牛": self.reset_ntr,
            "切换ntr开关状态": self.switch_ntr,
            "换老婆": self.change_wife,
            "重置换": self.reset_change_wife,
            "交换老婆": self.swap_wife,
            "同意交换": self.agree_swap_wife,
            "拒绝交换": self.reject_swap_wife,
            "查看交换请求": self.view_swap_requests,
        }

    def load_admins(self) -> list:
        """加载管理员列表"""
        path = os.path.join("data", "cmd_config.json")
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
                admins = cfg.get("admins_id", [])
                return [str(admin_id) for admin_id in admins]
        except Exception:
            return []

    def parse_at_target(self, event: AstrMessageEvent) -> str | None:
        """解析消息中的@目标用户"""
        if not event.message_obj or not hasattr(event.message_obj, "message"):
            return None
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                return str(comp.qq)
        return None

    def parse_target(self, event: AstrMessageEvent) -> str | None:
        """解析命令目标用户"""
        target = self.parse_at_target(event)
        if target:
            return target
        
        msg = normalize_cmd_text(event.message_str)
        # 兼容“昵称 + 额外参数”的用法，例如：
        # - /牛老婆 昵称 3
        # - /查老婆 昵称
        for cmd in ("牛老婆", "查老婆"):
            if msg.startswith(cmd):
                rest = msg[len(cmd):].strip()
                if not rest:
                    return None
                first = rest.split()[0].strip()
                # 如果第一个参数是数字，通常是编号参数，不当作昵称匹配
                if first.isdigit():
                    return None
                group_id = str(event.message_obj.group_id)
                cfg = load_group_config(group_id)
                for uid, data in cfg.items():
                    if isinstance(data, list) and len(data) > 2 and data[2] == first:
                        return uid
                    if isinstance(data, dict) and data.get("nick") == first:
                        return uid
        return None

    # ==================== 消息处理 ====================

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_all_messages(self, event: AstrMessageEvent, *args, **kwargs):
        """消息分发处理（仅群聊监听）"""
        if not event.message_obj or not hasattr(event.message_obj, "group_id"):
            return
        
        # 检查是否需要前缀唤醒
        if self.need_prefix and not event.is_at_or_wake_command:
            return
        
        text = normalize_cmd_text(event.message_str)
        for cmd, func in self.commands.items():
            if text.startswith(cmd):
                async for res in func(event):
                    yield res
                break

    # ==================== 抽老婆相关 ====================

    async def animewife(self, event: AstrMessageEvent, *, record_to_backpack: bool = True):
        """抽老婆"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()
        size = self.backpack_size

        new_draw = False
        auto_slot: int | None = None
        backpack_full = False
        backpack_items: list | None = None

        img: str | None = None

        # 先在锁内检查是否已有今日老婆，避免无谓的外部请求
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            img, _, _, _, changed = resolve_today_entity(cfg, uid, today, size, nick_default=nick)
            if changed:
                save_group_config(gid, cfg)

        fetched_img: str | None = None
        if not img:
            fetched_img = await self._fetch_wife_image()
            if not fetched_img:
                yield event.plain_result("抱歉，今天的老婆获取失败了，请稍后再试~")
                return
        
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            # 二次检查：并发下可能已被其他协程写入
            img2, _, _, _, changed2 = resolve_today_entity(cfg, uid, today, size, nick_default=nick)
            if img2:
                img = img2
                if changed2:
                    save_group_config(gid, cfg)
            else:
                img = fetched_img

                # 抽老婆：实体 w 优先落入背包空槽位并绑定今日槽位；否则作为临时态保留（不重复存储）
                if record_to_backpack:
                    backpacks, items = get_user_backpack(cfg, uid, size)
                    slot = first_empty_slot(items)
                    if slot is not None:
                        auto_slot = slot
                        set_today_entity_slot(cfg, uid, today, nick, size, slot, img)
                    else:
                        backpack_full = True
                        backpack_items = items
                        set_today_entity_unsaved(cfg, uid, today, nick, img)
                else:
                    set_today_entity_unsaved(cfg, uid, today, nick, img)

                new_draw = True
                save_group_config(gid, cfg)
        
        extra_lines: list[str] = []
        if new_draw and record_to_backpack:
            if auto_slot is not None:
                extra_lines.append(f"已自动存入老婆背包：{auto_slot}号位（容量 {size}）")
            elif backpack_full:
                extra_lines.append(f"你的老婆背包已满（{size}/{size}），今天抽到的老婆不会自动保存。")
                extra_lines.append(f"如需保存，请发送 /替换老婆 <1-{size}> 选择一个位置替换；否则明天刷新后将消失。")
                if backpack_items is not None:
                    lines = []
                    for i, x in enumerate(backpack_items, start=1):
                        lines.append(f"{i}. {format_backpack_item(x)}")
                    extra_lines.append("当前背包：\n" + "\n".join(lines))

        # 生成并发送消息
        yield event.chain_result(self._build_wife_message(img, nick, extra_lines=extra_lines or None))

    async def _fetch_wife_image(self) -> str | None:
        """获取老婆图片"""
        imgs = await self._list_wife_images()
        return random.choice(imgs) if imgs else None

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=HTTP_TIMEOUT,
                trust_env=True,
            )
        return self._http_session

    def _read_local_wife_images(self) -> list[str]:
        list_path = os.path.join(PLUGIN_DIR, "list.txt")
        try:
            with open(list_path, "r", encoding="utf-8-sig") as f:
                imgs = self._parse_wife_image_list(f.read())
            if imgs:
                return imgs
        except (OSError, UnicodeError):
            pass

        try:
            return sorted(
                rel
                for name in os.listdir(IMG_DIR)
                if os.path.isfile(os.path.join(IMG_DIR, name))
                and (rel := normalize_img_id(name))
            )
        except OSError:
            return []

    @staticmethod
    def _parse_wife_image_list(text: str) -> list[str]:
        imgs: list[str] = []
        seen: set[str] = set()
        for line in text.splitlines():
            rel = normalize_img_id(line.strip())
            if rel and rel not in seen:
                seen.add(rel)
                imgs.append(rel)
        return imgs

    async def _list_wife_images(self) -> list[str]:
        """获取老婆图片列表：远端优先，失败时回退缓存与本地列表。"""
        url = (self.image_list_url or self.image_base_url or "").strip()
        now = time.monotonic()
        if self._wife_list_cache and now < self._wife_list_cache_expires_at:
            return list(self._wife_list_cache)

        async with self._wife_list_lock:
            now = time.monotonic()
            if self._wife_list_cache and now < self._wife_list_cache_expires_at:
                return list(self._wife_list_cache)
            if now < self._wife_list_retry_after:
                if self._wife_list_cache:
                    return list(self._wife_list_cache)
                return self._read_local_wife_images()

            if url:
                try:
                    session = await self._get_http_session()
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"image list returned HTTP {resp.status}")
                        imgs = self._parse_wife_image_list(await resp.text())
                    if not imgs:
                        raise ValueError("image list response contained no valid entries")
                    self._wife_list_cache = imgs
                    self._wife_list_cache_expires_at = time.monotonic() + WIFE_LIST_CACHE_TTL_SECONDS
                    self._wife_list_retry_after = 0.0
                    return list(imgs)
                except Exception:
                    self._wife_list_retry_after = time.monotonic() + WIFE_LIST_RETRY_COOLDOWN_SECONDS

            if self._wife_list_cache:
                return list(self._wife_list_cache)
            return self._read_local_wife_images()

    def _build_wife_message(self, img: str, nick: str, *, extra_lines: list[str] | None = None):
        """构建老婆消息链"""
        text = f"{nick}，你今天的老婆是{format_wife_name(img)}，请好好珍惜哦~"

        if extra_lines:
            text += "\n" + "\n".join(extra_lines)

        base_url = (self.image_base_url or "").strip()
        url_img = normalize_img_id(img)
        local_path = safe_img_path(img)
        try:
            chain: list = [Plain(text)]
            if local_path and os.path.exists(local_path):
                chain.append(Image.fromFileSystem(local_path))
            elif base_url and url_img:
                chain.append(Image.fromURL(base_url + url_img))
            return chain
        except Exception:
            return [Plain(text)]

    # ==================== 帮助命令 ====================

    async def wife_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """
【基础命令】
• 抽老婆 - 每天抽取一个二次元老婆
• 老婆背包 - 查看自己的老婆背包列表
• 查老婆 <编号> - 查看自己的背包老婆(带图)
• 查老婆 [@用户/昵称] - 查看对方完整背包(标注今日老婆)
• 替换老婆 <编号> - 用“今天的老婆”替换背包指定位置

【牛老婆功能】(概率较低😭)
• 牛老婆 [@用户] - 有概率抢走别人的今日老婆(额外入库到背包，不顶掉今日老婆位)
• 牛老婆 @用户 <编号> - 有概率抢走对方背包指定编号的老婆(额外入库到背包，不顶掉今日老婆位)
• 重置牛 [@用户] - 重置牛的次数(失败会禁言)

【换老婆功能】
• 换老婆 - 丢弃当前老婆换新的
• 重置换 [@用户] - 重置换老婆的次数(失败会禁言)

【交换功能】
• 交换老婆 [@用户] - 向别人发起老婆交换请求
• 同意交换 [@发起者] - 同意交换请求
• 拒绝交换 [@发起者] - 拒绝交换请求
• 查看交换请求 - 查看当前的交换请求

【管理员命令】
• 切换ntr开关状态 - 开启/关闭NTR功能
• 发老婆 @用户 <关键词> - 按关键词发一个老婆给对方(覆盖对方今日老婆，优先入库)

💡 提示：部分命令有每日使用次数限制
"""
        yield event.plain_result(help_text.strip())

    async def search_wife(self, event: AstrMessageEvent):
        """查老婆"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())

        # 用法1: 查老婆 <编号> -> 查自己的背包老婆
        msg = normalize_cmd_text(event.message_str)
        arg = msg[len("查老婆"):].strip() if msg.startswith("查老婆") else ""
        if arg:
            try:
                slot = int(arg.split()[0])
            except Exception:
                slot = None
            if slot is not None:
                async for res in self.view_backpack_wife(event, slot):
                    yield res
                return

        # 用法2: 查老婆 [@用户/昵称] -> 查看对方完整背包
        tid = self.parse_target(event)
        today = get_today()
        size = self.backpack_size

        if tid:
            tid = str(tid)
            async with get_config_lock(gid):
                cfg = load_group_config(gid)
                owner_nick = get_cfg_nick(cfg, tid, tid)
                today_img, today_slot, _, _, changed = resolve_today_entity(
                    cfg, tid, today, size, nick_default=owner_nick
                )
                _, items = get_user_backpack(cfg, tid, size)
                if changed:
                    save_group_config(gid, cfg)

            used = sum(1 for entry in items if backpack_entry_to_img_note(entry)[0])
            lines = []
            for slot_index, entry in enumerate(items, start=1):
                marker = "（今日老婆）" if slot_index == today_slot else ""
                lines.append(f"{slot_index}. {format_backpack_item(entry)}{marker}")
            if today_img and today_slot is None:
                lines.append(f"今日老婆未存入背包：{format_wife_name(today_img)}")
            elif not today_img:
                lines.append("当前没有今日老婆记录。")

            text = f"{owner_nick}的老婆背包（{used}/{size}）：\n" + "\n".join(lines)
            yield event.plain_result(text)
            return

        # 无目标时保留原行为：查询自己的今日老婆。
        tid = uid
        owner_nick = None
        note: str | None = None
        
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            # 兼容新“实体 w”结构：今日老婆可能只存引用(slot)。
            owner_nick = get_cfg_nick(cfg, str(tid), None) or "对方"
            img, _, _, note, changed = resolve_today_entity(
                cfg, str(tid), today, size, nick_default=owner_nick
            )
            if changed:
                save_group_config(gid, cfg)
            if not img:
                yield event.plain_result("没有发现老婆的踪迹，快去抽一个试试吧~")
                return

        extra = f"（{note}）" if note else ""
        text = f"{owner_nick}的老婆是{format_wife_name(img)}{extra}，羡慕吗？"

        base_url = (self.image_base_url or "").strip()
        url_img = normalize_img_id(img)
        local_path = safe_img_path(img)
        try:
            chain = [
                Plain(text),
                (
                    Image.fromFileSystem(local_path)
                    if local_path and os.path.exists(local_path)
                    else (Image.fromURL(base_url + url_img) if base_url and url_img else None)
                ),
            ]
            chain = [x for x in chain if x is not None]
            yield event.chain_result(chain)
        except Exception:
            yield event.plain_result(text)

    async def view_backpack_wife(self, event: AstrMessageEvent, slot: int):
        """查自己的背包老婆（编号槽位）。"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        size = self.backpack_size

        if slot < 1 or slot > size:
            yield event.plain_result(f"{nick}，编号范围是 1-{size}。用法：/查老婆 <编号>")
            return

        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            backpacks = cfg.get(BACKPACKS_KEY, {})
            if not isinstance(backpacks, dict):
                backpacks = {}

            items = normalize_backpack(backpacks.get(uid), size)
            entry = items[slot - 1] if slot - 1 < len(items) else None
            img, note = backpack_entry_to_img_note(entry)

        if not img:
            yield event.plain_result(f"{nick}，你的{slot}号老婆位还是空的哦~")
            return

        extra = f"（{note}）" if note else ""
        text = f"{nick}，你的{slot}号老婆是({format_wife_name(img)}{extra})，想起她了么~"
        base_url = (self.image_base_url or "").strip()
        url_img = normalize_img_id(img)
        local_path = safe_img_path(img)
        try:
            chain = [
                Plain(text),
                (
                    Image.fromFileSystem(local_path)
                    if local_path and os.path.exists(local_path)
                    else (Image.fromURL(base_url + url_img) if base_url and url_img else None)
                ),
            ]
            chain = [x for x in chain if x is not None]
            yield event.chain_result(chain)
        except Exception:
            yield event.plain_result(text)

    async def replace_wife(self, event: AstrMessageEvent):
        """用今天的老婆替换背包指定槽位。"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()
        size = self.backpack_size

        msg = event.message_str.strip()
        msg = normalize_cmd_text(msg)
        arg = msg[len("替换老婆"):].strip() if msg.startswith("替换老婆") else ""
        if not arg:
            yield event.plain_result(f"{nick}，用法：/替换老婆 <1-{size}>")
            return

        try:
            slot = int(arg.split()[0])
        except Exception:
            yield event.plain_result(f"{nick}，用法：/替换老婆 <1-{size}>")
            return

        if slot < 1 or slot > size:
            yield event.plain_result(f"{nick}，编号范围是 1-{size}。")
            return

        err: str | None = None
        img: str | None = None
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            img, prev_slot, _, note, changed = resolve_today_entity(cfg, uid, today, size, nick_default=nick)
            if changed:
                # 仅迁移/修复也要落盘，避免后续一致性问题
                save_group_config(gid, cfg)
            if not img:
                err = f"{nick}，你今天还没有老婆，先 /抽老婆 再来替换吧~"

            if img:
                # “替换老婆”语义调整为移动实体 w：w 在总记录中只存在一处
                backpacks, items = get_user_backpack(cfg, uid, size)
                if prev_slot is not None and 1 <= prev_slot <= size and prev_slot != slot:
                    items[prev_slot - 1] = None
                backpacks[uid] = items
                cfg[BACKPACKS_KEY] = backpacks

                set_today_entity_slot(cfg, uid, today, nick, size, slot, img, note=note)
                save_group_config(gid, cfg)

        if err:
            yield event.plain_result(err)
            return
        if not img:
            yield event.plain_result(f"{nick}，替换失败：未找到今天的老婆记录。")
            return

        yield event.plain_result(f"{nick}，已将今天的老婆存入{slot}号背包位：{format_wife_name(img)}")

    async def show_backpack(self, event: AstrMessageEvent):
        """显示自己的老婆背包列表。"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        size = self.backpack_size

        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            backpacks = cfg.get(BACKPACKS_KEY, {})
            if not isinstance(backpacks, dict):
                backpacks = {}
            items = normalize_backpack(backpacks.get(uid), size)

        used = sum(1 for x in items if backpack_entry_to_img_note(x)[0])
        lines = [f"{i}. {format_backpack_item(x)}" for i, x in enumerate(items, start=1)]
        text = (
            f"{nick}，你的老婆背包（{used}/{size}）：\n"
            + "\n".join(lines)
            + "\n\n用 /查老婆 <编号> 查看对应老婆(带图)。"
        )
        yield event.plain_result(text)

    async def send_wife(self, event: AstrMessageEvent):
        """按关键词给指定用户发老婆（覆盖今日老婆，并尝试优先入库）。"""
        gid = str(event.message_obj.group_id)
        sender_uid = str(event.get_sender_id())
        sender_nick = event.get_sender_name()
        today = get_today()
        size = self.backpack_size

        if sender_uid not in self.admins:
            yield event.plain_result(f"{sender_nick}，该命令仅管理员可用哦~")
            return

        tid = self.parse_at_target(event)
        if not tid:
            yield event.plain_result(f"{sender_nick}，用法：/发老婆 @用户 <关键词>")
            return

        msg = normalize_cmd_text(event.message_str)
        rest = msg[len("发老婆"):].strip() if msg.startswith("发老婆") else ""
        if rest.startswith("@"):
            parts = rest.split()
            keyword = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        else:
            keyword = rest.strip()

        # 兜底：从消息链 Plain 中提取关键词
        if not keyword and event.message_obj and hasattr(event.message_obj, "message"):
            plain_text = "".join(
                seg.text for seg in event.message_obj.message if isinstance(seg, Plain)
            ).strip()
            if plain_text.startswith("发老婆"):
                keyword = plain_text[len("发老婆"):].strip()

        if not keyword:
            yield event.plain_result(f"{sender_nick}，请在命令后提供关键词。例如：/发老婆 @用户 澪")
            return

        all_imgs = await self._list_wife_images()
        kw = keyword.lower()
        matches = [
            img
            for img in all_imgs
            if kw in img.lower() or kw in format_wife_name(img).lower()
        ]
        if not matches:
            yield event.plain_result(f"{sender_nick}，没有找到包含“{keyword}”的老婆图片。")
            return

        img = random.choice(matches)

        target_name = None
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(gid), user_id=int(tid)
            )
            target_name = info.get("card") or info.get("nickname")
        except Exception:
            target_name = None

        stored_slot: int | None = None
        is_full = False

        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            if not target_name:
                target_name = get_cfg_nick(cfg, str(tid), str(tid))

            # 覆盖今日老婆（遵循“实体 w”单一来源模型）：
            # - 若对方今日老婆已落在背包槽位：覆盖同一槽位（不新增、不复制）
            # - 若对方今日老婆为临时态：覆盖临时态
            # - 若对方今日没有老婆：优先入库到空槽位；满则临时态
            prev_img, prev_slot, _, _, changed = resolve_today_entity(
                cfg, tid, today, size, nick_default=target_name
            )
            if prev_img and prev_slot is not None and 1 <= prev_slot <= size:
                set_today_entity_slot(cfg, tid, today, target_name, size, prev_slot, img)
                stored_slot = prev_slot
            else:
                backpacks, items = get_user_backpack(cfg, tid, size)
                empty = first_empty_slot(items)
                if empty is not None:
                    set_today_entity_slot(cfg, tid, today, target_name, size, empty, img)
                    stored_slot = empty
                else:
                    set_today_entity_unsaved(cfg, tid, today, target_name, img)
                    is_full = True

            save_group_config(gid, cfg)

        cancel_msg = await self.cancel_swap_on_wife_change(gid, [tid])

        name = format_wife_name(img)
        extra = (
            f"已存入对方背包 {stored_slot} 号位。"
            if stored_slot is not None
            else (
                f"对方背包已满，本次未自动保存；可让对方用 /替换老婆 <1-{size}> 保存。"
                if is_full
                else ""
            )
        )
        text = f"{sender_nick} 给 {target_name} 发了一位老婆：{name}。{extra}"
        base_url = (self.image_base_url or "").strip()
        url_img = normalize_img_id(img)
        local_path = safe_img_path(img)
        try:
            chain = [
                Plain(text),
                (
                    Image.fromFileSystem(local_path)
                    if local_path and os.path.exists(local_path)
                    else (Image.fromURL(base_url + url_img) if base_url and url_img else None)
                ),
            ]
            chain = [x for x in chain if x is not None]
            yield event.chain_result(chain)
        except Exception:
            yield event.plain_result(text)

        if cancel_msg:
            yield event.plain_result(cancel_msg)

    # ==================== 牛老婆相关 ====================

    async def ntr_wife(self, event: AstrMessageEvent):
        """牛老婆"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        
        # 检查 NTR 功能是否启用
        if not ntr_statuses.get(gid, True):
            yield event.plain_result("牛老婆功能还没开启哦，请联系管理员开启~")
            return
        
        today = get_today()
        size = self.backpack_size

        # 解析可选背包编号：
        # - /牛老婆 @用户 <编号> : 牛走对方背包指定槽位
        # - /牛老婆 @用户       : 保持旧行为（牛走对方今日老婆）
        msg = normalize_cmd_text(event.message_str)
        rest = msg[len("牛老婆"):].strip() if msg.startswith("牛老婆") else ""
        slot: int | None = None
        if rest:
            parts = rest.split()
            if parts and parts[-1].isdigit():
                try:
                    slot_arg = int(parts[-1])
                except Exception:
                    slot_arg = None
                if slot_arg is not None:
                    if 1 <= slot_arg <= size:
                        slot = slot_arg
                    else:
                        yield event.plain_result(
                            f"{nick}，编号范围是 1-{size}。用法：/牛老婆 @用户 <编号>（不带编号则默认牛今天的老婆）"
                        )
                        return

        # 获取目标用户
        tid = self.parse_target(event)
        if not tid or tid == uid:
            if not tid:
                tip = "请@你想牛的对象，或输入完整的昵称哦~"
                if slot is not None or (rest.strip().isdigit() if rest else False):
                    tip = f"请@你想牛的对象。用法：/牛老婆 @用户 <1-{size}>（不带编号默认牛今天的老婆）"
                yield event.plain_result(f"{nick}，{tip}")
            else:
                yield event.plain_result(f"{nick}，不能牛自己呀，换个人试试吧~")
            return

        # 预检查：对方是否有可牛对象、自己是否有背包空位（不消耗次数）
        target_nick = None
        src_suffix = ""
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            target_nick = get_cfg_nick(cfg, str(tid), str(tid))

            my_backpacks, my_items = get_user_backpack(cfg, uid, size)
            my_empty_slot = first_empty_slot(my_items)
            if my_empty_slot is None:
                yield event.plain_result(f"{nick}，你的老婆背包已满（{size}/{size}），先清理/替换后再来牛吧~")
                return

            if slot is None:
                t_img, t_slot, _, _, changed = resolve_today_entity(
                    cfg, str(tid), today, size, nick_default=target_nick
                )
                if changed:
                    save_group_config(gid, cfg)
                if not t_img:
                    yield event.plain_result("对方今天还没有老婆可牛哦~")
                    return
            else:
                t_backpacks, t_items = get_user_backpack(cfg, str(tid), size)
                entry = t_items[slot - 1] if 0 <= slot - 1 < len(t_items) else None
                t_img, _ = backpack_entry_to_img_note(entry)
                if not t_img:
                    yield event.plain_result(f"对方背包的{slot}号位还是空的哦~")
                    return
                src_suffix = f"（来自对方背包{slot}号位）"
                # 若牛走的是对方“今日实体 w”的槽位，则等同牛走今日老婆：要清空今日位
                t_today_img, t_today_slot, _, _, changed2 = resolve_today_entity(
                    cfg, str(tid), today, size, nick_default=target_nick
                )
                if changed2:
                    save_group_config(gid, cfg)
                # 这里不直接修改数据，只用于提前校验/提示

        # 消耗一次牛老婆次数（原子 check+increment）
        async with records_lock:
            grp = records["ntr"].setdefault(gid, {})
            rec = grp.get(uid, {"date": today, "count": 0})
            if rec.get("date") != today:
                rec = {"date": today, "count": 0}
            if rec["count"] >= self.ntr_max:
                yield event.plain_result(f"{nick}，你今天已经牛了{self.ntr_max}次啦，明天再来吧~")
                return
            rec["count"] += 1
            grp[uid] = rec
            save_records()
            rem = self.ntr_max - rec["count"]

        # 判断牛老婆是否成功
        if random.random() >= self.ntr_possibility:
            yield event.plain_result(f"{nick}，很遗憾，牛失败了！你今天还可以再试{rem}次~")
            return

        stolen_img: str | None = None
        stored_slot: int | None = None
        cancel_ids: list[str] = []

        # 二次校验 + 原子迁移（同一把群配置锁内完成“从对方移除 + 写入自己背包”）
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            target_nick = get_cfg_nick(cfg, str(tid), str(tid))

            # 再确认自己仍有空位
            my_backpacks, my_items = get_user_backpack(cfg, uid, size)
            my_empty_slot = first_empty_slot(my_items)
            if my_empty_slot is None:
                stolen_img = None
            else:
                if slot is None:
                    t_img, _, _, _, changed = resolve_today_entity(
                        cfg, str(tid), today, size, nick_default=target_nick
                    )
                    if changed:
                        save_group_config(gid, cfg)
                    if not t_img:
                        stolen_img = None
                    else:
                        stolen_img = t_img
                        # 牛走“今日实体 w”：对方今日位与对应槽位(如有)必须一起消失
                        remove_today_entity(cfg, str(tid), today, size)
                        cancel_ids.append(str(tid))
                else:
                    t_backpacks, t_items = get_user_backpack(cfg, str(tid), size)
                    entry = t_items[slot - 1] if 0 <= slot - 1 < len(t_items) else None
                    t_img, _ = backpack_entry_to_img_note(entry)
                    if not t_img:
                        stolen_img = None
                    else:
                        stolen_img = t_img
                        # 若该槽位是对方“今日实体 w”，则清空今日位与槽位；否则仅清空该槽位
                        t_today_img, t_today_slot, _, _, _ = resolve_today_entity(
                            cfg, str(tid), today, size, nick_default=target_nick
                        )
                        if t_today_img and t_today_slot == slot:
                            remove_today_entity(cfg, str(tid), today, size)
                            cancel_ids.append(str(tid))
                        else:
                            t_items[slot - 1] = None
                            t_backpacks[str(tid)] = t_items
                            cfg[BACKPACKS_KEY] = t_backpacks

                if stolen_img:
                    note = f"牛自用户 {target_nick}" if target_nick else "牛自用户"
                    my_items[my_empty_slot - 1] = make_backpack_entry(stolen_img, note)
                    my_backpacks[uid] = my_items
                    cfg[BACKPACKS_KEY] = my_backpacks
                    stored_slot = my_empty_slot

                save_group_config(gid, cfg)

        # 二次校验失败：退还次数
        if not stolen_img or stored_slot is None:
            async with records_lock:
                grp = records["ntr"].setdefault(gid, {})
                rec = grp.get(uid, {"date": today, "count": 0})
                if rec.get("date") == today and rec.get("count", 0) > 0:
                    rec["count"] = max(0, rec["count"] - 1)
                    grp[uid] = rec
                    save_records()
            yield event.plain_result(f"{nick}，对方的老婆刚刚溜走了，这次不算次数，再试试吧~")
            return

        cancel_msg = await self.cancel_swap_on_wife_change(gid, cancel_ids) if cancel_ids else None

        name = format_wife_name(stolen_img)
        note_suffix = f"（牛自用户 {target_nick}）" if target_nick else ""
        keep_suffix = "不会顶掉你今天抽到的老婆位。"
        text = f"{nick}，牛老婆成功！你牛到了 {name}{note_suffix}{src_suffix}，已存入背包{stored_slot}号位~{keep_suffix}"

        base_url = (self.image_base_url or "").strip()
        url_img = normalize_img_id(stolen_img)
        local_path = safe_img_path(stolen_img)
        try:
            chain = [
                Plain(text),
                (
                    Image.fromFileSystem(local_path)
                    if local_path and os.path.exists(local_path)
                    else (Image.fromURL(base_url + url_img) if base_url and url_img else None)
                ),
            ]
            chain = [x for x in chain if x is not None]
            yield event.chain_result(chain)
        except Exception:
            yield event.plain_result(text)

        if cancel_msg:
            yield event.plain_result(cancel_msg)

    async def switch_ntr(self, event: AstrMessageEvent):
        """切换 NTR 开关（仅管理员）"""
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        
        if uid not in self.admins:
            yield event.plain_result(f"{nick}，你没有权限操作哦~")
            return
        
        gid = str(event.message_obj.group_id)
        async with ntr_lock:
            current_status = ntr_statuses.get(gid, True)
            ntr_statuses[gid] = not current_status
            save_ntr_statuses()
        
        state = "开启" if not current_status else "关闭"
        yield event.plain_result(f"{nick}，NTR已{state}")

    # ==================== 换老婆相关 ====================

    async def change_wife(self, event: AstrMessageEvent):
        """换老婆"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()
        size = self.backpack_size

        # 先原子占用一次“换老婆次数”，避免并发下超额；若后续失败再回滚
        reserved = False
        async with records_lock:
            recs = records["change"].setdefault(gid, {})
            rec = recs.get(uid, {"date": "", "count": 0})
            if rec.get("date") != today:
                rec = {"date": today, "count": 0}
            if int(rec.get("count", 0)) >= self.change_max_per_day:
                yield event.plain_result(f"{nick}，你今天已经换了{self.change_max_per_day}次老婆啦，明天再来吧~")
                return
            rec["count"] = int(rec.get("count", 0)) + 1
            recs[uid] = rec
            save_records()
            reserved = True

        # 检查是否有今日老婆（不再删除记录，直接覆盖并同步背包绑定槽位）
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            cur_img, _, _, _, changed = resolve_today_entity(cfg, uid, today, size, nick_default=nick)
            if changed:
                save_group_config(gid, cfg)
            if not cur_img:
                # 回滚占用次数
                async with records_lock:
                    recs = records["change"].setdefault(gid, {})
                    rec2 = recs.get(uid, {"date": today, "count": 0})
                    if rec2.get("date") == today and int(rec2.get("count", 0)) > 0:
                        rec2["count"] = max(0, int(rec2.get("count", 0)) - 1)
                        recs[uid] = rec2
                        save_records()
                yield event.plain_result(f"{nick}，你今天还没有老婆，先去抽一个再来换吧~")
                return

        new_img = await self._fetch_wife_image()
        if not new_img:
            # 回滚占用次数
            async with records_lock:
                recs = records["change"].setdefault(gid, {})
                rec2 = recs.get(uid, {"date": today, "count": 0})
                if rec2.get("date") == today and int(rec2.get("count", 0)) > 0:
                    rec2["count"] = max(0, int(rec2.get("count", 0)) - 1)
                    recs[uid] = rec2
                    save_records()
            yield event.plain_result("抱歉，今天的老婆获取失败了，请稍后再试~")
            return

        extra_lines: list[str] = []
        wife_chain: list | None = None
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            # 并发二次检查：确保仍有“今日老婆”记录（避免被其他操作清空/跨日）
            prev_img, prev_slot, _, _, changed2 = resolve_today_entity(cfg, uid, today, size, nick_default=nick)
            if not prev_img:
                # 回滚占用次数
                async with records_lock:
                    recs = records["change"].setdefault(gid, {})
                    rec2 = recs.get(uid, {"date": today, "count": 0})
                    if rec2.get("date") == today and int(rec2.get("count", 0)) > 0:
                        rec2["count"] = max(0, int(rec2.get("count", 0)) - 1)
                        recs[uid] = rec2
                        save_records()
                yield event.plain_result(f"{nick}，换老婆失败：你当前没有“今日老婆”记录了，请重新 /抽老婆~")
                return

            # 换老婆默认作用于“今日实体 w”本身：
            # - 若 w 已落在背包槽位，则覆盖该槽位（不新增、不复制），旧 w 从记录中消失
            # - 若 w 为临时态，则只更新临时态
            if prev_slot is not None and 1 <= prev_slot <= size:
                set_today_entity_slot(cfg, uid, today, nick, size, prev_slot, new_img)
                extra_lines.append(f"已同步更新老婆背包：{prev_slot}号位（容量 {size}）")
            else:
                set_today_entity_unsaved(cfg, uid, today, nick, new_img)
                backpacks, items = get_user_backpack(cfg, uid, size)
                if first_empty_slot(items) is None:
                    extra_lines.append(f"你的老婆背包已满（{size}/{size}），今天换到的老婆不会自动保存。")
                extra_lines.append(f"如需保存，请发送 /替换老婆 <1-{size}> 选择一个位置替换；否则明天刷新后将消失。")

            save_group_config(gid, cfg)

            wife_chain = self._build_wife_message(new_img, nick, extra_lines=extra_lines or None)

        # 取消相关交换请求（确认成功换老婆后再取消，避免“未换成功却取消了请求”）
        cancel_msg = await self.cancel_swap_on_wife_change(gid, [uid])
        if cancel_msg:
            yield event.plain_result(cancel_msg)

        if wife_chain is None:
            yield event.plain_result(f"{nick}，换老婆失败：消息构建失败，请稍后再试~")
            return

        # 立即展示新老婆
        yield event.chain_result(wife_chain)

    # ==================== 重置相关 ====================

    async def reset_ntr(self, event: AstrMessageEvent):
        """重置牛老婆次数"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()
        
        # 管理员可直接重置他人
        if uid in self.admins:
            tid = self.parse_at_target(event) or uid
            async with records_lock:
                if gid in records["ntr"] and tid in records["ntr"][gid]:
                    del records["ntr"][gid][tid]
                    save_records()
            yield event.chain_result([
                Plain("管理员操作：已重置"), At(qq=int(tid)), Plain("的牛老婆次数。")
            ])
            return
        
        # 普通用户使用重置机会
        async with records_lock:
            grp = records["reset"].setdefault(gid, {})
            rec = grp.get(uid, {"date": today, "count": 0})
            
            if rec.get("date") != today:
                rec = {"date": today, "count": 0}
            
            if rec["count"] >= self.reset_max_uses_per_day:
                yield event.plain_result(f"{nick}，你今天已经用完{self.reset_max_uses_per_day}次重置机会啦，明天再来吧~")
                return
            
            rec["count"] += 1
            grp[uid] = rec
            save_records()
        
        tid = self.parse_at_target(event) or uid
        
        if random.random() < self.reset_success_rate:
            async with records_lock:
                if gid in records["ntr"] and tid in records["ntr"][gid]:
                    del records["ntr"][gid][tid]
                    save_records()
            yield event.chain_result([
                Plain("已重置"), At(qq=int(tid)), Plain("的牛老婆次数。")
            ])
        else:
            try:
                await event.bot.set_group_ban(group_id=int(gid), user_id=int(uid), duration=self.reset_mute_duration)
            except Exception:
                pass
            yield event.plain_result(f"{nick}，重置牛失败，被禁言{self.reset_mute_duration}秒，下次记得再接再厉哦~")

    async def reset_change_wife(self, event: AstrMessageEvent):
        """重置换老婆次数"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()
        
        # 管理员可直接重置他人
        if uid in self.admins:
            tid = self.parse_at_target(event) or uid
            async with records_lock:
                grp = records["change"].setdefault(gid, {})
                if tid in grp:
                    del grp[tid]
                    save_records()
            yield event.chain_result([
                Plain("管理员操作：已重置"), At(qq=int(tid)), Plain("的换老婆次数。")
            ])
            return
        
        # 普通用户使用重置机会
        async with records_lock:
            grp = records["reset"].setdefault(gid, {})
            rec = grp.get(uid, {"date": today, "count": 0})
            
            if rec.get("date") != today:
                rec = {"date": today, "count": 0}
            
            if rec["count"] >= self.reset_max_uses_per_day:
                yield event.plain_result(f"{nick}，你今天已经用完{self.reset_max_uses_per_day}次重置机会啦，明天再来吧~")
                return
            
            rec["count"] += 1
            grp[uid] = rec
            save_records()
        
        tid = self.parse_at_target(event) or uid
        
        if random.random() < self.reset_success_rate:
            async with records_lock:
                grp2 = records["change"].setdefault(gid, {})
                if tid in grp2:
                    del grp2[tid]
                    save_records()
            yield event.chain_result([
                Plain("已重置"), At(qq=int(tid)), Plain("的换老婆次数。")
            ])
        else:
            try:
                await event.bot.set_group_ban(group_id=int(gid), user_id=int(uid), duration=self.reset_mute_duration)
            except Exception:
                pass
            yield event.plain_result(f"{nick}，重置换失败，被禁言{self.reset_mute_duration}秒，下次记得再接再厉哦~")

    # ==================== 交换老婆相关 ====================

    async def swap_wife(self, event: AstrMessageEvent):
        """发起交换老婆请求"""
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        tid = self.parse_at_target(event)
        nick = event.get_sender_name()
        today = get_today()
        size = self.backpack_size

        if not tid or tid == uid:
            yield event.plain_result(f"{nick}，请在命令后@你想交换的对象哦~")
            return
        
        # 检查双方是否都有老婆
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            changed_any = False
            for x, who in ((uid, nick), (tid, "对方")):
                img, _, _, _, changed = resolve_today_entity(
                    cfg, str(x), today, size, nick_default=get_cfg_nick(cfg, str(x), None) or str(x)
                )
                changed_any = changed_any or changed
                if not img:
                    yield event.plain_result(f"{who}，今天还没有老婆，无法进行交换哦~")
                    return
            if changed_any:
                save_group_config(gid, cfg)

        # 防止重复请求（先检查是否已存在）
        async with swap_lock:
            grp = swap_requests.setdefault(gid, {})
            existing = grp.get(uid)
            if isinstance(existing, dict) and existing.get("date") == today:
                yield event.plain_result(f"{nick}，你今天已经发起过交换请求了，用“查看交换请求”看看吧~")
                return

        # 记录交换请求次数（原子 check+increment）
        async with records_lock:
            grp_limit = records["swap"].setdefault(gid, {})
            rec_lim = grp_limit.get(uid, {"date": "", "count": 0})
            if rec_lim.get("date") != today:
                rec_lim = {"date": today, "count": 0}
            if rec_lim.get("count", 0) >= self.swap_max_per_day:
                yield event.plain_result(f"{nick}，你今天已经发起了{self.swap_max_per_day}次交换请求啦，明天再来吧~")
                return
            rec_lim["count"] = int(rec_lim.get("count", 0)) + 1
            grp_limit[uid] = rec_lim
            save_records()
        
        need_rollback = False
        async with swap_lock:
            grp = swap_requests.setdefault(gid, {})
            existing2 = grp.get(uid)
            if isinstance(existing2, dict) and existing2.get("date") == today:
                # 并发重复：不覆盖现有请求
                need_rollback = True
            else:
                grp[uid] = {"target": tid, "date": today}
                save_swap_requests()

        if need_rollback:
            # 回滚次数占用
            async with records_lock:
                grp_limit = records["swap"].setdefault(gid, {})
                rec_lim = grp_limit.get(uid, {"date": today, "count": 0})
                if rec_lim.get("date") == today and rec_lim.get("count", 0) > 0:
                    rec_lim["count"] = max(0, int(rec_lim.get("count", 0)) - 1)
                    grp_limit[uid] = rec_lim
                    save_records()
            yield event.plain_result(f"{nick}，你今天已经发起过交换请求了，用“查看交换请求”看看吧~")
            return
        
        yield event.chain_result([
            Plain(f"{nick} 想和 "), At(qq=int(tid)),
            Plain(" 交换老婆啦！请对方用\"同意交换 @发起者\"或\"拒绝交换 @发起者\"来回应~")
        ])

    async def agree_swap_wife(self, event: AstrMessageEvent):
        """同意交换老婆"""
        gid = str(event.message_obj.group_id)
        tid = str(event.get_sender_id())
        uid = self.parse_at_target(event)
        nick = event.get_sender_name()
        today = get_today()
        size = self.backpack_size

        if not uid:
            yield event.plain_result(f"{nick}，请在命令后@发起者，或用\"查看交换请求\"命令查看当前请求哦~")
            return
        
        # 检查和删除交换请求（原子操作）
        async with swap_lock:
            grp = swap_requests.get(gid, {})
            rec = grp.get(uid)
            
            if not rec or rec.get("target") != tid:
                yield event.plain_result(f"{nick}，请在命令后@发起者，或用\"查看交换请求\"命令查看当前请求哦~")
                return
            
            # 删除请求
            del grp[uid]
            save_swap_requests()
        
        # 执行交换
        swapped = False
        async with get_config_lock(gid):
            cfg = load_group_config(gid)
            u_nick = get_cfg_nick(cfg, str(uid), str(uid))
            t_nick = get_cfg_nick(cfg, str(tid), str(tid))

            u_img, u_slot, _, _, changed_u = resolve_today_entity(
                cfg, str(uid), today, size, nick_default=u_nick
            )
            t_img, t_slot, _, _, changed_t = resolve_today_entity(
                cfg, str(tid), today, size, nick_default=t_nick
            )
            if (changed_u or changed_t):
                save_group_config(gid, cfg)

            if not u_img or not t_img:
                swapped = False
            else:
                # 交换遵循“实体 w 单一来源模型”：保持每个人原本的存储方式（slot-ref 或临时态）
                if u_slot is not None and 1 <= u_slot <= size:
                    set_today_entity_slot(cfg, str(uid), today, u_nick, size, u_slot, t_img)
                else:
                    set_today_entity_unsaved(cfg, str(uid), today, u_nick, t_img)

                if t_slot is not None and 1 <= t_slot <= size:
                    set_today_entity_slot(cfg, str(tid), today, t_nick, size, t_slot, u_img)
                else:
                    set_today_entity_unsaved(cfg, str(tid), today, t_nick, u_img)

                save_group_config(gid, cfg)
                swapped = True

        if not swapped:
            # 交换失败：返还发起者次数（请求已删除）
            async with records_lock:
                grp_limit = records["swap"].setdefault(gid, {})
                rec_lim = grp_limit.get(uid, {"date": today, "count": 0})
                if rec_lim.get("date") == today and rec_lim.get("count", 0) > 0:
                    rec_lim["count"] = max(0, int(rec_lim.get("count", 0)) - 1)
                    grp_limit[uid] = rec_lim
                    save_records()
            yield event.plain_result("交换失败：你们其中一方的今日老婆已变更/消失，本次请求已取消并返还次数~")
            return
        
        # 取消相关交换请求
        cancel_msg = await self.cancel_swap_on_wife_change(gid, [uid, tid])
        
        yield event.plain_result("交换成功！你们的老婆已经互换啦，祝幸福~")
        if cancel_msg:
            yield event.plain_result(cancel_msg)

    async def reject_swap_wife(self, event: AstrMessageEvent):
        """拒绝交换老婆"""
        gid = str(event.message_obj.group_id)
        tid = str(event.get_sender_id())
        uid = self.parse_at_target(event)
        nick = event.get_sender_name()
        
        async with swap_lock:
            grp = swap_requests.get(gid, {})
            rec = grp.get(uid)
            
            if not rec or rec.get("target") != tid:
                yield event.plain_result(f"{nick}，请在命令后@发起者，或用\"查看交换请求\"命令查看当前请求哦~")
                return
            
            del grp[uid]
            save_swap_requests()
        
        yield event.chain_result([
            At(qq=int(uid)), Plain("，对方婉拒了你的交换请求，下次加油吧~")
        ])

    async def view_swap_requests(self, event: AstrMessageEvent):
        """查看当前交换请求"""
        gid = str(event.message_obj.group_id)
        me = str(event.get_sender_id())

        async with swap_lock:
            grp = dict(swap_requests.get(gid, {}) or {})
        cfg = load_group_config(gid)
        
        # 获取发起的和收到的请求
        sent_targets = [rec.get("target") for uid, rec in grp.items() if uid == me and isinstance(rec, dict)]
        sent_targets = [x for x in sent_targets if x]
        received_from = [uid for uid, rec in grp.items() if isinstance(rec, dict) and rec.get("target") == me]
        
        if not sent_targets and not received_from:
            yield event.plain_result("你当前没有任何交换请求哦~")
            return
        
        parts = []
        for tid in sent_targets:
            name = get_cfg_nick(cfg, str(tid), "未知用户")
            parts.append(f"→ 你发起给 {name} 的交换请求")
        
        for uid in received_from:
            name = get_cfg_nick(cfg, str(uid), "未知用户")
            parts.append(f"→ {name} 发起给你的交换请求")
        
        text = "当前交换请求如下：\n" + "\n".join(parts) + "\n请在\"同意交换\"或\"拒绝交换\"命令后@发起者进行操作~"
        yield event.plain_result(text)

    # ==================== 辅助方法 ====================

    async def cancel_swap_on_wife_change(self, gid: str, user_ids: list) -> str | None:
        """检查并取消与指定用户相关的交换请求"""
        today = get_today()
        to_cancel: list[str] = []

        # 先在 swap_lock 下原子删除请求并落盘，避免并发丢写
        async with swap_lock:
            grp = swap_requests.get(gid, {})
            if not isinstance(grp, dict):
                return None
            for req_uid, req in list(grp.items()):
                if req_uid in user_ids or (isinstance(req, dict) and req.get("target") in user_ids):
                    to_cancel.append(req_uid)
                    try:
                        del grp[req_uid]
                    except KeyError:
                        pass
            if to_cancel:
                save_swap_requests()

        if not to_cancel:
            return None

        # 返还次数（records_lock）
        async with records_lock:
            grp_limit = records["swap"].setdefault(gid, {})
            for req_uid in to_cancel:
                rec_lim = grp_limit.get(req_uid, {"date": "", "count": 0})
                if rec_lim.get("date") == today and rec_lim.get("count", 0) > 0:
                    rec_lim["count"] = max(0, int(rec_lim.get("count", 0)) - 1)
                    grp_limit[req_uid] = rec_lim
            save_records()

        return f"已自动取消 {len(to_cancel)} 条相关的交换请求并返还次数~"

    async def terminate(self):
        """插件卸载时清理资源"""
        global config_locks, records, swap_requests, ntr_statuses

        session, self._http_session = self._http_session, None
        if session and not session.closed:
            await session.close()
        
        # 清理群组配置锁
        config_locks.clear()
        
        # 清理全局数据
        records.clear()
        swap_requests.clear()
        ntr_statuses.clear()
