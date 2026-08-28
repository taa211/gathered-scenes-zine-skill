# -*- coding: utf-8 -*-
"""GPT 直读直出 - 批量出图（单标签页串行）

每张照片：
① /navigate 回 chatgpt.com（= 新对话起点）→ 等编辑器出现 + 确认进入新对话（user 消息=0）
② /upload 上传照片
③ /type 第一轮指令（风格模板：视觉理解 + 设计提示词）→ 发送并确认上屏
④ 等设计回复完成（am>=1 且文本连续 2 次不增，不依赖 stop）
⑤ 按模板类型提取提示词（mondo 回溯保留主体 / 蒸馏纸刊 照片1：分支）→ /type 第二轮 → 发送确认
⑥ 等成品图出现且数量连续 2 轮稳定 → 下载前再确认 → 下载（失败删半成品）

修复记录（2026-08-10，p12b 按 gpt-batch-direct-fix-analysis.md §5 F0 + §6 修正版执行）：
- F0 根因 D/E：eval 直接返回对象，eval_js 统一处理三种形态（dict / {"raw":文本} / {"err":...}），
  state()/gen_state() 对已解析 dict 不再二次 json.loads。
- F1 修正2：extract_prompt 按模板类型分派（--template-type mondo / distillation-zine）；
  mondo 提取定位风格词后回溯到所在段落的 [ 或句首（保留主体，主体在风格词之前），到结尾注前。
- F2 修正3：post() 捕获一切异常返回 {"err":...}；eval_js 检测 err 打印警告返回 None；
  state() 遇 err 返回 {}；wait_design/wait_image 对连续空状态计数报错（防无限超时静默吞掉）。
- F3 wait_design：am>=1 且 lastLen 连续 2 次（8s 间隔）不增即完成（不依赖 stop）。
- F4 补充1：send_msg 发送前记录 user 消息基数，发送后轮询确认 user 数增加或进入生成态；
  /type 检查 err。
- F5 wait_image：成品图（非 user 消息 estuary 图 + complete + naturalWidth>=300）>prev_gen
  且连续 2 轮（10s 间隔）不再变；do_download 下载前再确认。
- F6 补充2：navigate 后 wait_new_conversation（编辑器出现 + user 消息数=0，防旧对话残留误判）；
  每 2-3 张限流休息。
- 修正4：do_download 检查 r.ok，失败 return False 并删除半成品文件（防 --skip-existing 永久跳过补不回）。

用法：python scripts/gpt_batch_direct.py <照片1> <照片2> ... [--out 输出目录] [--template 风格模板] [--template-type mondo|distillation-zine] [--anchor 提示词锚点] [--suffix 后缀]
"""
import json, time, urllib.request, os, sys, re, argparse
from pathlib import Path

SERVER = "http://127.0.0.1:9223"

# 账号自动切换（--auto-switch-account，默认 False 不影响现有行为）
try:
    from switch_account import is_quota_blocked, switch_to_other
except ImportError:
    is_quota_blocked = None
    switch_to_other = None


def _retry_after_quota(photo, out_path, template, template_type, anchor, mode, round2_template, no_examples, auto_switch, face_level="auto", expression=None):
    """出图超时处理：auto_switch 且检测到限额 → 切到另一账号 → 重跑本张一次（防无限递归）；否则返回 False"""
    if auto_switch and is_quota_blocked is not None and is_quota_blocked():
        print("  ⚠️ 检测到限额提示，自动切换账号...", flush=True)
        new_acc = switch_to_other()
        if new_acc:
            print(f"  ✓ 已切换到账号 {new_acc}，重新跑本张（三阶段）", flush=True)
            return process_one(photo, out_path, template, template_type, anchor, mode, round2_template, no_examples, auto_switch=False, face_level=face_level, expression=expression)
    return False

# 20 位全量锚点库（master-anchor-20-design-20260810.md §1.1，顺序与模板一致）
MASTER_ANCHORS = [
    # Belle Époque 装饰复古
    "Jules Chéret", "Henri de Toulouse-Lautrec", "Alphonse Mucha", "Théophile Steinlen", "Eugène Grasset",
    # 现代主义极简
    "A.M. Cassandre", "Saul Bass", "Josef Müller-Brockmann", "Paul Rand",
    # 好莱坞电影传奇
    "Drew Struzan", "Milton Glaser",
    # Mondo 当代海报
    "Olly Moss", "Tyler Stout", "Martin Ansin", "Laurent Durieux", "Jay Ryan", "Kilian Eng",
    # 当代激进
    "Shepard Fairey", "Dan McCarthy", "Jock",
]


# 匿名风格代号表（prompt-safety-redesign-20260811.md §3.1）：对话内部使用（round2 标题/图片说明），
# 只含特征词不含人名；⚠️ 禁止用作 sanitize 替换词（§3.3 红线：代号进出图 prompt 违反 §2.1 原则）
MASTER_ANON_NAMES = {
    "Jules Chéret": "Belle-Époque Joyful Lithograph",
    "Henri de Toulouse-Lautrec": "Montmartre Flat-Block Cabaret",
    "Alphonse Mucha": "Art-Nouveau Ornate Floral",
    "Théophile Steinlen": "Social-Realist Expressive Line",
    "Eugène Grasset": "Gothic Stained-Glass Decorative",
    "A.M. Cassandre": "Cubist Art-Deco Travel",
    "Saul Bass": "Minimalist Geometric Metaphor",
    "Josef Müller-Brockmann": "Swiss Grid Rationalist",
    "Paul Rand": "Playful Corporate Geometry",
    "Drew Struzan": "Epic Cinematic Painted Glow",
    "Milton Glaser": "Psychedelic Pop Typography",
    "Olly Moss": "Ultra-Minimal Negative-Space Double Meaning",
    "Tyler Stout": "Maximalist Intricate Collage",
    "Martin Ansin": "Art-Deco Elegant Vintage",
    "Laurent Durieux": "Atmospheric Visual-Pun Mystery",
    "Jay Ryan": "Folksy Handmade Warm Texture",
    "Kilian Eng": "Geometric Futurist Precision",
    "Shepard Fairey": "Propaganda Halftone Stencil",
    "Dan McCarthy": "Ultra-Flat Geometric Abstraction",
    "Jock": "Gritty Expressive High-Contrast",
}


# 名字变体表（§3.2/§3.3 补丁 v2.1 条件4）：规范名 + 短名 + 无变音变体
# 匹配按 len 降序（长名优先："Saul Bass" 先于 "Bass"，防先删 "Bass" 残留孤立 "Saul"）；
# 大小写敏感（人名首字母大写，"bass guitar"/"moss"/"random"/"engine"/"jockey" 天然不匹配）；
# \b 词边界（\bRand\b 不匹配 "random"、\bEng\b 不匹配 "engine"）。
NAME_VARIANTS = {
    "Jules Chéret": ["Jules Chéret", "Jules Cheret"],
    "Henri de Toulouse-Lautrec": ["Henri de Toulouse-Lautrec", "Toulouse-Lautrec"],
    "Alphonse Mucha": ["Alphonse Mucha"],
    "Théophile Steinlen": ["Théophile Steinlen", "Theophile Steinlen", "Steinlen"],
    "Eugène Grasset": ["Eugène Grasset", "Eugene Grasset"],
    "A.M. Cassandre": ["A.M. Cassandre", "AM Cassandre", "Cassandre"],
    "Saul Bass": ["Saul Bass", "Bass"],
    "Josef Müller-Brockmann": ["Josef Müller-Brockmann", "Müller-Brockmann", "Muller-Brockmann"],
    "Paul Rand": ["Paul Rand", "Rand"],
    "Drew Struzan": ["Drew Struzan"],
    "Milton Glaser": ["Milton Glaser"],
    "Olly Moss": ["Olly Moss", "Moss"],
    "Tyler Stout": ["Tyler Stout"],
    "Martin Ansin": ["Martin Ansin"],
    "Laurent Durieux": ["Laurent Durieux"],
    "Jay Ryan": ["Jay Ryan", "Ryan"],
    "Kilian Eng": ["Kilian Eng", "Eng"],
    "Shepard Fairey": ["Shepard Fairey"],
    "Dan McCarthy": ["Dan McCarthy"],
    "Jock": ["Jock"],
}

# 全局变体列表，按 len 降序（长名优先匹配）
ALL_NAME_VARIANTS = sorted({v for vs in NAME_VARIANTS.values() for v in vs}, key=len, reverse=True)


def _key(s):
    """归一化键：NFKD 去变音（é→e, ü→u, è→e）+ lower + 去非字母数字（处理 A.M./AM/空格/连字符差异）
    如 'Josef Müller-Brockmann'/'Josef Muller-Brockmann'/'JOSEF MULLERBROCKMANN' → josefmullerbrockmann"""
    import unicodedata
    norm = unicodedata.normalize("NFKD", s)
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return "".join(ch for ch in norm.lower() if ch.isalnum())


# 示例图按大师归类（example-images-by-master.md §2/§8）：仅 5 位有图（主图+备选），其余 15 位无图只靠文字
# 修正⑧：路径基于 Path(__file__).resolve().parent.parent 绝对推导，不依赖 cwd
_MASTER_EX_DIR = Path(__file__).resolve().parent.parent / "examples"
MASTER_EXAMPLE_IMAGES = {
    "Saul Bass": {"main": "imdb-05-12-angry-men.png", "alt": "imdb-09-pulp-fiction.png"},
    "Olly Moss": {"main": "example-negative-space.png", "alt": "imdb-03-dark-knight.png"},
    "Jay Ryan": {"main": "usecase-moments-poster.png", "alt": "usecase-xiaohongshu-mood.png"},
    "Martin Ansin": {"main": "imdb-02-godfather.png", "alt": "imdb-04-godfather-2.png"},
    "Laurent Durieux": {"main": "imdb-07-schindlers-list.png", "alt": "imdb-06-lotr-return.png"},
}
for _m, _img in MASTER_EXAMPLE_IMAGES.items():
    _img["main"] = str(_MASTER_EX_DIR / _img["main"])
    _img["alt"] = str(_MASTER_EX_DIR / _img["alt"])


def load_master_modifiers(path=None):
    """解析 artist-styles.md → {大师名: Prompt Modifiers 文本}（E 方案）
    解析规则：按 `## N. 名 (年份)` 切段；段内找 `**Prompt Modifiers:**` 后第一个 ``` 代码块。
    默认路径 = 项目根 references/artist-styles.md（修正②：非项目根 cwd 也可用）；
    加载失败 → 打印一次性警告 + 返回 {}（调用方降级）。"""
    if path is None:
        path = str(Path(__file__).resolve().parent.parent / "references" / "artist-styles.md")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️ 大师库加载失败: {e}，大师风格注入降级为仅真名", flush=True)
        return {}
    result = {}
    sections = re.split(r"(?m)^## \d+\.\s*", text)
    for sec in sections[1:]:
        name = sec.split("\n", 1)[0].strip()
        name = re.sub(r"\s*\(\d{4}.*?\)\s*$", "", name).strip()  # 去 (年份)
        m = re.search(r"\*\*Prompt Modifiers:\*\*\s*```\s*(.*?)```", sec, re.S)
        if m and name:
            result[name] = m.group(1).strip()
    return result


MASTER_MODIFIERS = load_master_modifiers()  # 一次性加载（E 方案）


def get_master_modifiers(master):
    """按 _key 归一化查询大师 Prompt Modifiers（兼容 artist-styles.md 标题差异，
    如 Théophile-Alexandre Steinlen 与锚点 Théophile Steinlen：词级匹配兜底）"""
    mk = _key(master)
    for k, v in MASTER_MODIFIERS.items():
        kk = _key(k)
        if mk == kk or mk in kk or kk in mk:
            return v
    # 兜底：按原始名分词（空格/连字符分隔），每个词的 _key 都在候选键中（处理中间名差异）
    raw_words = re.findall(r"[A-Za-zÀ-ÿ]+", master)
    words = [_key(w) for w in raw_words if len(_key(w)) >= 3]
    for k, v in MASTER_MODIFIERS.items():
        kk = _key(k)
        if words and all(w in kk for w in words):
            return v
    return ""


def anonymize_modifiers(master, mods):
    """把 modifiers 文本中所有大师名清除，保留纯风格特征（prompt-safety-redesign-20260811.md §3.2）。
    执行顺序（补丁 v2.1 条件2 红线）：①删前缀（删到第一个逗号）→ ③特例 → ②中间名删除 → ④清理。
    ⚠️ ③必须在②之前：若②先删 "Saul Bass"，③找不到原文 → Moss 段残留孤立 "influence"。"""
    if not mods:
        return mods
    # ① 前缀删除：删到第一个逗号。实测 artist-styles.md 全部 20 条第一段 100% 含大师名
    #    （含短名 Toulouse-Lautrec/Steinlen/Cassandre，不依赖名字表天然覆盖）。
    idx = mods.find(",")
    if idx != -1:
        mods = mods[idx + 1:].lstrip(" ,")
    # ③ 特例替换（语义保留）——必须在②中间名删除之前
    mods = re.sub(r"Saul Bass influence", "minimalist geometry influence", mods, flags=re.IGNORECASE)
    mods = re.sub(r"OBEY aesthetic", "", mods, flags=re.IGNORECASE)
    # ② 中间名删除：变体表长名优先，\b 词边界 + 大小写敏感（人名首字母大写防误伤普通英文词）
    for v in ALL_NAME_VARIANTS:
        mods = re.sub(r"\b" + re.escape(v) + r"\b", "", mods)
    # ④ 清理
    mods = re.sub(r",\s*,", ",", mods)
    mods = re.sub(r"[ \t]{2,}", " ", mods)
    return mods.strip(" ,")


# 特征短语静态映射 NAME_FEATURE_MAP（§3.3 补丁 v2.1 条件3）：名字变体 → 特征短语（前 4-6 词），
# 模块级一次性预生成（build_msg2 签名无 master 参数，无法动态取词）；
# ⚠️ 禁止用 MASTER_ANON_NAMES 代号作替换词——代号只用于对话内部，进出图 prompt 违反 §2.1 原则。
def _build_name_feature_map():
    m = {}
    for master in MASTER_ANCHORS:
        mods = get_master_modifiers(master)
        if not mods:
            continue
        anon = anonymize_modifiers(master, mods)
        words = re.findall(r"[A-Za-zÀ-ÿ'’-]+", anon)
        feat = " ".join(words[:6])
        if not feat:
            continue
        for v in NAME_VARIANTS.get(master, []):
            m[v] = feat
    return m


NAME_FEATURE_MAP = _build_name_feature_map()


def sanitize_prompt(text):
    """对最终消息全文清洗（build_msg2 最后一步调用，A3：拼接后完整消息统一去名，two-phase mods 天然覆盖）。
    规则按序：① 模式替换（整模式优先）→ ③ 特例 → ② 孤立名字删除 → ④ 清理 → ⑤ 日志。
    名字匹配：变体表长名优先 + \b 词边界 + 大小写敏感（A5 防 bass guitar/random/engine/moss/jockey 误伤）。"""
    if not text:
        return text
    changed = []
    # ① 模式替换（整模式优先，防"名字删除后留下 in the style of 残句"）
    for v in ALL_NAME_VARIANTS:
        feat = NAME_FEATURE_MAP.get(v)
        if not feat:
            continue
        new, n = re.subn(r"\bin the style of\s+" + re.escape(v) + r"(?=\W)", "in the style of " + feat, text)
        if n:
            changed.append(v)
            text = new
        new, n = re.subn(r"\binspired by\s+" + re.escape(v) + r"(?=\W)", "with " + feat, text)
        if n:
            changed.append(v)
            text = new
        new, n = re.subn(r"\b" + re.escape(v) + r"\s+style\b", feat, text)
        if n:
            changed.append(v)
            text = new
    # ③ 特例（顺序同样先特例后名字删除，同 anonymize_modifiers）
    if re.search(r"Saul Bass influence", text, re.IGNORECASE):
        text = re.sub(r"Saul Bass influence", "minimalist geometry influence", text, flags=re.IGNORECASE)
        changed.append("Saul Bass influence")
    if re.search(r"OBEY aesthetic", text, re.IGNORECASE):
        text = re.sub(r"OBEY aesthetic", "", text, flags=re.IGNORECASE)
        changed.append("OBEY aesthetic")
    # ② 孤立名字删除（长名优先 + \b 边界 + 大小写敏感）
    for v in ALL_NAME_VARIANTS:
        new, n = re.subn(r"\b" + re.escape(v) + r"\b", "", text)
        if n:
            changed.append(v)
            text = new
    # ④ 清理（只压空格不压换行，防破坏 "：\n\n" 前缀结构）
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r",(?=\s*(?:\n|$))", "", text)
    text = text.strip()
    # ⑤ 日志（人工核对）
    if changed:
        print(f"  ⚠️ sanitize: 检出大师名 {sorted(set(changed))}，已替换", flush=True)
    return text


def _face_rule_text(face_level):
    """--face-level 强制指令文本（asian-aesthetic-redesign-20260811.md §4.5）：默认 auto → 空串（GPT 自主判断）。"""
    if face_level == "L0":
        return "⚠️ 本单氛围优先：脸部统一 L0 全剪影档"
    if face_level == "L1":
        return "⚠️ 本单客户要求认出家人/宠物：脸部统一 L1 简化特征档"
    if face_level == "L2":
        return "⚠️ 本单客户明确要求尽量像：脸部统一 L2 半写实剪影档"
    return ""


def extract_master(text):
    """从第一轮回复提取「选定大师：<名>」，返回库内规范名（含变音）或 None（20 位锚点库）
    正则（修正① 理由变体覆盖）：r"选定大师[：:]\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\- ]{1,40}?)(?=（|\(|，|,|。|\n)"
    ——覆盖 （理由）/（reason）/。理由/, 理由/换行 等分隔；先清星号（GPT 可能加粗 **名**）；
    _key() 变音归一化比对；不在锚点库 → None。"""
    if not text:
        return None
    clean = text.replace("*", "")  # 清 Markdown 星号（修正①：正则以字母开头，星号会阻匹配）
    m = re.search(r"选定大师[：:]\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\- ]{1,40}?)(?=（|\(|，|,|。|\n)", clean)
    if not m:
        return None
    name = m.group(1).strip()
    name_key = _key(name)
    for anchor in MASTER_ANCHORS:
        if name_key == _key(anchor):
            return anchor  # 返回库内规范名（含变音）
    print(f"  ⚠️ 大师名 {name!r} 不在锚点库（20 位），跳过注入", flush=True)
    return None


def build_msg2(prompt, mods=""):
    """构造第二轮出图消息（E 方案修正③）：mods 内联进提示词末尾（单一英文块，无中文标签段）
    mods 插在结尾注之前（prompt 提取时已截到结尾注前，故直接追加；若意外含结尾注则插其前）。
    A3：内部最后一步调用 sanitize_prompt（对拼接后完整消息去名，two-phase 的 mods 拼接天然覆盖）。"""
    if mods:
        # 若 prompt 意外含结尾注，mods 插在其前
        for mark in ("照片仅作语义参考", "photo only as semantic reference"):
            idx = prompt.find(mark)
            if idx != -1:
                prompt = prompt[:idx].rstrip() + ", " + mods + ", " + prompt[idx:]
                break
        else:
            prompt = prompt.rstrip() + ", " + mods
    return sanitize_prompt("请严格按照以下提示词生成图片：\n\n" + prompt)


def extract_round1(text, template_type="mondo", anchor="in Mondo poster style"):
    """解析第一轮回复（three-phase）→ {"master", "mood", "has_prompt", "features", "expression", "paradox"}
    - master：extract_master（锚点库校验 + 星号清理）
    - mood：r"照片情绪[：:]\s*([^\n]+)"（星号清理）
    - features：r"照片特征[：:]\s*([^\n]+)"（多样性：主体类型+构图潜力+色彩结构；旧格式无此行 → None 懒解析不崩）
    - expression（v2.3 照片灵魂保真）：r"表情[：:]\s*([^\n]+)"（FACS 式 4 要素编码；旧格式无此行 → None 不崩）
    - paradox（进化版理念，三阶段融合版）：r"视觉悖论[：:]\s*([^\n]+)"（概念钩子；旧格式无此行 → None 不崩）
    - has_prompt：extract_prompt(text) 长度 ≥100（懒降级判断）"""
    result = {"master": None, "mood": "", "has_prompt": False, "features": None, "expression": None, "paradox": None}
    if not text:
        return result
    result["master"] = extract_master(text)
    m = re.search(r"照片情绪[：:]\s*([^\n]+)", text)
    if m:
        result["mood"] = m.group(1).strip().replace("*", "").strip()
    f = re.search(r"照片特征[：:]\s*([^\n]+)", text)
    if f:
        result["features"] = f.group(1).strip().replace("*", "").strip()
    e = re.search(r"表情[：:]\s*([^\n]+)", text)
    if e:
        result["expression"] = e.group(1).strip().replace("*", "").strip()
    p = re.search(r"视觉悖论[：:]\s*([^\n]+)", text)
    if p:
        result["paradox"] = p.group(1).strip().replace("*", "").strip()
    prompt = extract_prompt(text, template_type, anchor)
    result["has_prompt"] = len(prompt) >= 100
    return result


def build_round2_msg(master, modifiers="", mood="", has_image=False, template_path=None, face_level="auto", features="", expression="", paradox=None):
    """构造第二轮「重新设计」指令（three-phase）
    读 round2 模板（修正⑥：默认绝对路径，不依赖 cwd），替换 {master}/{modifiers}/{mood}/{image_note}/{face_rule}/{features}/{expression}/{paradox}；
    - A 方案：{master} → 匿名风格代号（MASTER_ANON_NAMES，标题+image_note 均用代号）；
      {modifiers} → anonymize_modifiers 匿名结果；modifiers 空 → 匿名一句话占位（不含真名）
    - B 方案：{face_rule} → --face-level 强制指令（默认 auto → 空串）
    - P3（v2.3）：{features}/{expression}/{mood} 三占位——任一为空删除对应"；字段"段（先拆段再删空再拼回）；
      三占位括号句不匹配（内联降级模板无 {features}/{expression}）→ 回退 mood 空删整句
    - 修正③：mood 为空 → 删除整个括号句（防残句）
    - paradox（进化版理念融合）：{paradox} 为空/None → 删除【视觉悖论设计】整段（防残句）
    - has_image=True → image_note=图片说明段（代号替代真名，修正④）；False → 空
    - 模板读取失败 → 内联降级指令（A4：匿名措辞；补丁 v2.1 条件5：降级路径不含任务 B 脸部决策条，可接受）"""
    if template_path is None:
        template_path = str(Path(__file__).resolve().parent.parent / "plans" / "gpt-direct-round2-template-mondo.txt")
    try:
        tpl = Path(template_path).read_text(encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️ 读取 round2 模板失败: {e}，用内联降级指令", flush=True)
        tpl = (
            "现在请基于以下大师的完整风格特征，为这张照片（你刚才判断的照片情绪：{mood}）重新设计一条完整的 Mondo 风格英文图片生成提示词。\n\n"
            "【大师完整风格 - {master}】\n{modifiers}\n\n"
            "【设计要求】1. 严格遵守第一轮消息中的 Mondo 风格规则 8 条；2. 把大师风格特征深度融入构图/色彩/质感/情绪的所有设计决策，风格锚点用纯特征描述表达（如 refined atmospheric approach with visual pun composition），⚠️ 提示词中禁止出现任何艺术家姓名/人名，禁止 in the style of <人名>、inspired by <人名>、<人名> style 等含人名的写法；"
            "3. 提示词以 [主体] in Mondo poster style 开头；4. 画幅完全自由，由你按构图最佳效果决定；5. 结尾注明：照片仅作语义参考，全重绘，无照片像素。\n\n【重要】只输出提示词文字，不要生成任何图片。"
        )
    anon = MASTER_ANON_NAMES.get(master, master)
    # P3（v2.3）：三占位空删——先拆段再删空再拼回（任一字段为空删对应段）
    bracket3 = "（照片特征：{features}；表情：{expression}；你刚才判断的照片情绪：{mood}）"
    if bracket3 in tpl:
        segs = []
        if features:
            segs.append(f"照片特征：{features}")
        if expression:
            segs.append(f"表情：{expression}")
        if mood:
            segs.append(f"你刚才判断的照片情绪：{mood}")
        tpl = tpl.replace(bracket3, ("（" + "；".join(segs) + "）") if segs else "")
    else:
        # 内联降级模板/回退模板无三占位 → 回退：mood 空删整句；features/expression 非 None 才替换（None 时模板无占位，跳过防崩）
        if not mood:
            tpl = re.sub(r"（你刚才判断的照片情绪：\{mood\}）", "", tpl)
        if features:
            tpl = tpl.replace("{features}", features)
        if expression:
            tpl = tpl.replace("{expression}", expression)
    if not modifiers:
        modifiers = "以极简、负空间、符号化视觉语言著称的匿名风格，请以其典型风格重新设计。"
    else:
        modifiers = anonymize_modifiers(master, modifiers)
    face_rule = _face_rule_text(face_level)
    image_note = ""
    if has_image:
        image_note = f"【图片说明】本轮消息中新增的 1 张图片是 {anon} 的示例作品，仅作风格参考（构图/质感/色彩可借鉴，不得复制其内容与元素）；你设计的目标照片是对话中第一轮上传的那张照片。"
    # paradox（进化版理念）：空/None → 删除【视觉悖论设计】整段（防残句，兼容旧模板无 {paradox} 占位）
    if paradox:
        tpl = tpl.replace("{paradox}", paradox)
    else:
        tpl = re.sub(r"\n*【视觉悖论设计】.*?(?=\n【设计要求】|\Z)", "", tpl, flags=re.S)
    return (tpl.replace("{master}", anon)
               .replace("{modifiers}", modifiers)
               .replace("{mood}", mood)
               .replace("{face_rule}", face_rule)
               .replace("{image_note}", image_note))


def post(endpoint, payload, timeout=90):
    """POST JSON；捕获一切网络/HTTP 异常返回 {"err": ...}，不抛异常（F2）"""
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{SERVER}{endpoint}", data=body, headers={"Content-Type": "application/json"})
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8")
    except Exception as e:
        return {"err": str(e)}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def eval_js(code, warn=True):
    """统一 eval 结果解析（F0 + 修正3）：
    - {"err": ...} → 打印警告 + 返回 None（哨兵，调用方计数防静默吞）
    - {"raw": 文本}（server 对非 JSON 字符串直接 text 返回）→ 取 raw 文本
    - dict（server 对对象 JSON.stringify → post 已 json.loads）→ 直接用，不再二次解析
    - 其他（bool/int）→ 原值"""
    r = post("/eval", {"code": code})
    if isinstance(r, dict) and "err" in r:
        if warn:
            print(f"  ⚠️ eval err: {str(r['err'])[:120]}", flush=True)
        return None
    if isinstance(r, dict) and "raw" in r:
        return r["raw"]
    return r


def state():
    """当前页状态：是否生成中 / 成品图数量 / assistant 消息数 / 最后一条 assistant 文本长度。
    ⚠️ eval 直接返回对象（server 会 JSON.stringify），不要用 JSON.stringify 包裹（F0）；
    err 或解析失败 → {}（修正3）"""
    st = eval_js("""(() => {
      const stop = document.querySelector('[data-testid=stop-button]');
      const imgs = Array.from(document.querySelectorAll('img[src*="estuary"]')).filter(i => {
        const msg = i.closest('[data-message-author-role]');
        return !(msg && msg.getAttribute('data-message-author-role') === 'user')
          && i.src.startsWith('http') && i.complete && i.naturalWidth >= 300;
      });
      const am = document.querySelectorAll('[data-message-author-role=assistant]');
      const last = am.length ? am[am.length - 1].innerText : '';
      return { stop: !!stop, gen: imgs.length, am: am.length, lastLen: last.length };
    })()""")
    if st is None:
        return {}
    if isinstance(st, dict):
        return st
    try:
        return json.loads(st)
    except Exception:
        return {}


def last_assistant():
    """最后一条 assistant 消息全文（纯文本）。
    ⚠️ 修正1：innerText 非 JSON → post 返回 {"raw": text} → eval_js 返回字符串，本就正常，勿二次解析"""
    return eval_js("(() => { const am = document.querySelectorAll('[data-message-author-role=assistant]'); return am.length ? am[am.length-1].innerText : ''; })()")


def assistant_count():
    """当前 assistant 消息数（发送确认判据：GPT 开始回复=发送必然成功，不受 user 渲染延迟影响）"""
    return eval_js("(() => document.querySelectorAll('[data-message-author-role=assistant]').length)()")


def user_msg_count():
    """当前 user 消息数量（发送确认 F4 / 新对话确认 F6 用）"""
    return eval_js("(() => document.querySelectorAll('[data-message-author-role=user]').length)()")


def click_send(wait_btn=20):
    """轮询发送按钮出现（最多 wait_btn 秒，间隔 1s；连跑时上传附件后按钮可能 10-20s 才渲染）再点击；
    存在并点击返回 "sent"，超时返回 "no_btn"（生成态按钮不渲染）。
    no_btn 修复：wait_btn 15→20s（治标辅助，no_btn 主修复=send_msg 刷新重试路径）。"""
    t0 = time.time()
    while time.time() - t0 < wait_btn:
        r = eval_js('(() => { const b = document.querySelector("[data-testid=send-button]"); if (b) { b.click(); return "sent"; } return "no_btn"; })()')
        if r == "sent":
            return "sent"
        time.sleep(1)
    return "no_btn"


# ── 提示词提取（F1 修正2：按模板类型分派 + mondo 回溯保留主体）──

def extract_prompt(text, template_type="mondo", anchor="in Mondo poster style"):
    """按模板类型分派提取函数"""
    if not text:
        return ""
    if template_type == "distillation-zine":
        return _extract_prompt_zine(text)
    if template_type == "distillation-zine-full":
        return _extract_prompt_zine_full(text)
    if template_type == "gathered-zine-full":
        # 拾景纸刊完整版：同样只输出英文 final_prompt（四段式），无大师行 → 复用全文提取
        return _extract_prompt_zine_full(text)
    if template_type == "gc-minimal-zine":
        # GC 极简纸刊完整版：同样只输出英文 final_prompt（四段式），无大师行 → 复用全文提取
        return _extract_prompt_zine_full(text)
    if template_type == "gathered-zine-a0":
        # 拾景纸刊 A0 验证版：同样只输出英文 final_prompt（四段式，≤500 词），无大师行 → 复用全文提取
        return _extract_prompt_zine_full(text)
    if template_type == "gathered-zine-short":
        # 拾景纸刊短句版（7 句 ≤150 字）：同样无大师行 → 复用全文提取
        return _extract_prompt_zine_full(text)
    if template_type == "gathered-zine-evolved":
        # 拾景纸刊进化版（qwen 设计，8 维）：同样无大师行 → 复用全文提取
        return _extract_prompt_zine_full(text)
    if template_type == "gathered-zine-evolved-v2":
        # 拾景纸刊进化版 v2（背景丰富度增强）：同样无大师行 → 复用全文提取
        return _extract_prompt_zine_full(text)
    if template_type == "gathered-zine-evolved-v3":
        # 拾景纸刊进化版 v3（特效位置硬约束）：同样无大师行 → 复用全文提取
        return _extract_prompt_zine_full(text)
    if template_type == "mondo-evolved":
        # 进化版 Mondo（qwen 第一性原则，概念悖论体系）：只输出英文 final_prompt → 复用全文提取
        return _extract_prompt_zine_full(text)
    if template_type == "gathered-zine-evolved-v4-landscape":
        # 拾景纸刊进化版 v4 风景（场域型路由，qwen 8 维风景对照表）：同样无大师行 → 复用全文提取
        return _extract_prompt_zine_full(text)
    return _extract_prompt_mondo(text, anchor)


def _extract_prompt_mondo(text, anchor="in Mondo poster style"):
    """mondo 模板：真实提示词形如 `[Small seated cat inside a pet carrier in Mondo poster style], ...`
    主体在风格词之前 → 定位风格词后回溯到所在段落的 [ 或段首（保留主体），到结尾注前（中/英文）。"""
    # 候选文本 = 结尾注之前（中英文都处理）
    cand = text
    for mark in ("照片仅作语义参考", "photo only as semantic reference"):
        idx = cand.find(mark)
        if idx != -1:
            cand = cand[:idx]
            break
    # 定位 anchor 最后一次出现（提示词段落通常在回复末尾）
    idx_a = cand.rfind(anchor)
    if idx_a == -1:
        return ""
    # 回溯到 anchor 所在行（段落）的开头
    line_start = cand.rfind("\n", 0, idx_a) + 1
    start = line_start
    # 若段首前不远（同逻辑行）有 [（主体方括号），从 [ 开始
    prev_bracket = cand.rfind("[", 0, line_start)
    if prev_bracket != -1 and line_start - prev_bracket <= 200:
        start = prev_bracket
    # 若 anchor 所在行本身含 [ 在 anchor 前，也回溯到 [（防段落内主体未从段首开始）
    inline_bracket = cand.find("[", line_start, idx_a)
    if inline_bracket != -1:
        start = inline_bracket
    out = cand[start:].strip()
    # E 方案防回归：若提取结果意外含「选定大师」行（模板第一行），截断到该行之后
    idx_m = out.find("选定大师")
    if idx_m != -1:
        line_end = out.find("\n", idx_m)
        out = out[line_end + 1:].strip() if line_end != -1 else ""
    return out


def _extract_prompt_zine(text):
    """蒸馏纸刊模板：找「照片1：」字段或「Distillation zine poster」段（保留旧分支）"""
    m = re.search(r"照片1[：:]\s*(.+?)(?:；|;)?\s*\n*\s*照片2|照片1[：:]\s*(.+)", text, re.S)
    if m:
        return (m.group(1) or m.group(2)).strip()
    m2 = re.search(r"(distillation zine poster.*?)(?:照片2|$)", text, re.S | re.I)
    if m2:
        return m2.group(1).strip()
    return ""


def _extract_prompt_zine_full(text):
    """完整版蒸馏纸刊模板（gpt-direct-round1-template-distillation-zine-full.txt）：
    GPT 只输出英文五段式 final_prompt（段间空行分隔），无大师选择行/照片N：字段。
    策略：剥离开头元文本（中文引导语/Here is 等）与代码块标记，正文起点 = 第一个
    含英文单词且非引导语的行，从该行取到末尾（final_prompt 完整传给第二轮）。"""
    if not text:
        return ""
    lines = text.splitlines()
    # 引导语识别（行首匹配，大小写不敏感）
    LEADERS = ("好的", "以下是", "这是", "下面", "请", "当然", "已经", "收到",
               "here", "ok", "okay", "sure", "of course", "```")
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        # 无英文单词的行（纯中文引导/空壳）→ 跳过
        if not re.search(r"[A-Za-z]", s):
            continue
        # 含 final_prompt 字样的短行（元文本说明）→ 跳过
        if re.search(r"final[ _-]?prompt", low) and len(s) < 120:
            continue
        # 中文字符明显多于英文 → 判为中文元文本（正文是英文五段式）
        cn = len(re.findall(r"[\u4e00-\u9fff]", s))
        en_words = len(re.findall(r"[A-Za-z]+", s))
        if cn > en_words * 3 and cn > 20:
            continue
        # 已知引导语开头 → 跳过
        if any(low.startswith(ld) for ld in LEADERS):
            continue
        start = i
        break
    if start is None:
        return ""
    out = "\n".join(lines[start:]).strip()
    # 去除残留代码块标记行，压缩多余空行
    out = re.sub(r"(?m)^```[a-z]*$", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ── 等待与发送（F3/F4/F5/F6）──

def wait_editor(timeout=30):
    """轮询 contenteditable 编辑器出现（F6）"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = eval_js("(() => !!document.querySelector('[contenteditable=true]'))()")
        if r is True:
            return True
        time.sleep(2)
    return False


def wait_new_conversation(timeout=45):
    """F6 补充2：等编辑器出现 + 确认进入新对话（user 消息数=0）。
    否则旧对话残留会让 wait_design 的 am>=1 立即误判通过、提取到旧内容。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if wait_editor(timeout=15):
            n = user_msg_count()
            if n == 0:
                return True
            print(f"  ⚠️ 检测到旧对话残留（user 消息 {n} 条），重新导航回主页", flush=True)
            post("/navigate", {"url": "https://chatgpt.com/"})
            time.sleep(5)
        time.sleep(2)
    return False


def click_coords(x, y):
    """按视口坐标真实鼠标点击（server.js /click-coords；React 按钮 JS click 可能无效）"""
    r = post("/click-coords", {"x": int(x), "y": int(y)})
    return isinstance(r, dict) and r.get("ok") is True


ERROR_MARKERS = ["消息流中的错误", "Something went wrong", "出了点问题", "An error occurred",
                # A6：追加 ChatGPT 防护限制文案（出图 prompt 触发"第三方内容相似性"防护时的报错提示）
                "防护限制", "第三方内容相似性", "相似性", "safety", "policy"]


def detect_error():
    """检测页面是否出现 ChatGPT 侧错误提示（消息流中的错误/重试/防护限制）。
    返回 (is_error: bool, retry_xy: tuple|None)。
    优先找「重试」按钮（含 重试/Retry 文本的 button）拿坐标；无按钮但有错误标记文本 → (True, None)。
    A6：除 body.innerText 前 3000 字符外，追加检查最后一条 assistant 消息全文
    （防护提示在消息流下部时 body 前缀查不到；现状 06-ZT8y3 实测 gen=0 干等 240s）。"""
    coord = eval_js("""(() => {
      const btns = Array.from(document.querySelectorAll('button'));
      const b = btns.find(x => { const t = (x.innerText || '').trim(); return t.includes('重试') || t.includes('Retry'); });
      if (b) {
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) return [Math.round(r.x + r.width/2), Math.round(r.y + r.height/2)];
      }
      return null;
    })()""")
    if isinstance(coord, list) and len(coord) == 2:
        return True, (int(coord[0]), int(coord[1]))
    text = eval_js("(() => document.body ? document.body.innerText.slice(0, 3000) : '')()")
    if isinstance(text, str) and any(m in text for m in ERROR_MARKERS):
        return True, None
    # A6 追加：最后一条 assistant 消息全文（防护提示在消息流下部时 body 前缀查不到）
    last = last_assistant()
    if isinstance(last, str) and any(m in last for m in ERROR_MARKERS):
        return True, None
    return False, None


def auto_retry():
    """检测到消息流错误 → 自动点重试按钮。返回 True=已处理错误（调用方重置等待基线继续），False=无错误。"""
    is_err, xy = detect_error()
    if not is_err:
        return False
    if xy:
        ok = click_coords(*xy)
        print(f"  ⚠️ 检测到消息流错误，已点击重试按钮 ({'ok' if ok else '点击失败'})", flush=True)
    else:
        print("  ⚠️ 检测到消息流错误但无重试按钮，按重试计数继续", flush=True)
    return True


def wait_design(prev_len, timeout=180, max_retry=3):
    """等第一轮设计回复完成：am>=1 且 lastLen 连续 2 次（间隔 8s）不增即完成（F3）。
    ⚠️ 不依赖 stop 按钮（已知 UI bug）；连续空状态计数报错（修正3 防静默吞 err）。"""
    t0 = time.time()
    last = prev_len
    stable = 0
    empty_streak = 0
    retry_count = 0
    while time.time() - t0 < timeout:
        # 顺带检测消息流错误 → 自动点重试（任务：GPT 回复中断显示错误+重试按钮）
        if auto_retry():
            retry_count += 1
            if retry_count > max_retry:
                print(f"  ⚠️ 错误自动重试超过 {max_retry} 次，放弃本张", flush=True)
                return False
            print(f"    错误重试 {retry_count}/{max_retry}，重置基线继续等待", flush=True)
            last = prev_len  # 重置长度基线（GPT 重新生成提示词）
            stable = 0
            empty_streak = 0
            time.sleep(3)
            continue
        st = state()
        if not st:
            empty_streak += 1
            if empty_streak >= 6:
                print("  ⚠️ 连续 6 次获取状态为空（疑似 err），中止等待", flush=True)
                return False
        else:
            empty_streak = 0
        cur = st.get("lastLen", 0)
        am = st.get("am", 0)
        if cur > last + 10:
            last = cur
            stable = 0
        else:
            stable += 1
        print(f"    等设计: am={am} lastLen={cur} stable={stable}/2 ({(time.time()-t0):.0f}s)", flush=True)
        if am >= 1 and last > prev_len and stable >= 2:
            return True
        time.sleep(8)
    return False


def wait_image(prev_gen, timeout=240, max_retry=3):
    """等出图：成品图数量 > prev_gen 且连续 2 轮（间隔 10s）不再变（F5）。
    成品图定义见 state()（非 user 消息 estuary 图 + complete + naturalWidth>=300）。
    顺带检测消息流错误 → 自动点重试（出图超时可能是同类错误）。"""
    t0 = time.time()
    last_gen = prev_gen
    stable = 0
    empty_streak = 0
    retry_count = 0
    while time.time() - t0 < timeout:
        if auto_retry():
            retry_count += 1
            if retry_count > max_retry:
                print(f"  ⚠️ 错误自动重试超过 {max_retry} 次，放弃本张", flush=True)
                return False
            print(f"    错误重试 {retry_count}/{max_retry}，重置基线继续等待", flush=True)
            last_gen = prev_gen
            stable = 0
            empty_streak = 0
            time.sleep(3)
            continue
        st = state()
        if not st:
            empty_streak += 1
            if empty_streak >= 6:
                print("  ⚠️ 连续 6 次获取状态为空（疑似 err），中止等待", flush=True)
                return False
        else:
            empty_streak = 0
        cur = st.get("gen", 0)
        if cur == last_gen:
            stable += 1
        else:
            last_gen = cur
            stable = 0
        print(f"    等出图: gen={cur} (prev={prev_gen}) stable={stable}/2 ({(time.time()-t0):.0f}s)", flush=True)
        if cur > prev_gen and stable >= 2:
            return True
        # 2026-08-23：10s→4s（成品图定义已过滤加载中缩略图，计数近似单调；
        # 2 轮稳判保留防候选图渐进加载波动，检测延迟 ~20s→~8s，用户实测图早出而脚本干等）
        time.sleep(4)
    return False


def attachment_count():
    """当前待发送附件数：上传缩略图的删除按钮数（aria-label 含 移除文件/Remove file）。
    no_btn 补强（需求1）：刷新后检查附件是否还在用。"""
    return eval_js("""(() => {
      const btns = Array.from(document.querySelectorAll('button')).filter(b => {
        const t = (b.getAttribute('aria-label') || '');
        return t.includes('移除文件') || t.includes('Remove file') || t.includes('remove file');
      });
      return btns.length;
    })()""")


def make_photo_hook(photo):
    """send_msg 刷新后回调（no_btn 补强需求1）：检查照片附件是否还在，丢失则重新上传。
    返回值 None 表示 eval 异常（不确定附件状态）→ 不重传（防重复上传双附件）。"""
    def hook():
        n = attachment_count()
        if n == 0:
            print("  ⚠️ 刷新后照片附件丢失，重新上传", flush=True)
            upload_photo(photo)
    return hook


def make_example_hook(master, no_examples):
    """send_msg 刷新后回调（no_btn 补强需求1）：检查大师示例图附件是否还在，丢失则重新上传。"""
    def hook():
        if no_examples or not master or not MASTER_EXAMPLE_IMAGES.get(master):
            return
        n = attachment_count()
        if n == 0:
            print("  ⚠️ 刷新后大师示例图附件丢失，重新上传", flush=True)
            upload_master_example(master, False)
    return hook


def current_cid():
    """当前对话 cid（pipeline 用）：从 location.href 提取 /c/<cid>，无则空串"""
    return eval_js("(() => { const m = location.href.match(/\\/c\\/([0-9a-f-]+)/); return m ? m[1] : ''; })()")


def send_msg(msg, allow_refresh=True, refresh_hook=None):
    """发送消息（F4 补充1）：点发送前记录 user 消息基数，发送后 sleep 3s 轮询确认 user 数增加
    或编辑器清空+send-button 消失（进入生成态）；确认不了/no_btn 刷新重试；
    刷新后等 10-15s 再重输（草稿会丢需重输），重试 3 次。
    ⚠️ no_btn 修复方案1（2026-08-12，用户实测）：no_btn/确认失败时**统一走刷新重试**——
    用户实测刷新页面后草稿保留 + send-button 出现 + 可继续发送（附件不丢），刷新是"已知可恢复"路径；
    因此即使 allow_refresh=False（带附件消息：示例图+照片）也走刷新重试，不再直接失败本张
    （历史修正④"刷新会丢附件"已被用户实测推翻；若未来发现刷新丢附件，退化为"刷新后重新上传附件再发送"）。
    allow_refresh 仅保留确认判据 4（user 数上屏）的作用：附件消息 user 上屏会误增，不认该判据。
    refresh_hook（no_btn 补强需求1）：刷新后先检查附件是否还在（attachment_count），
    丢失则重新上传（make_photo_hook/make_example_hook）→ 再确认编辑器 → 再点发送；附件在则直接继续。"""
    r = post("/type", {"text": msg})
    if isinstance(r, dict) and r.get("err"):
        print(f"  ❌ /type 失败: {r['err']}", flush=True)
        return False
    time.sleep(2)
    for attempt in range(3):
        base = user_msg_count()
        am_before = assistant_count()
        ed_before = eval_js("(() => { const ed = document.querySelector('[contenteditable=true]'); return ed ? (ed.innerText || '').length : -1; })()")
        s = click_send(wait_btn=5)
        if s == "no_btn":
            # 2026-08-23 新 UI 修复：/type 塞入的文字 React/ProseMirror 感知不到（编辑器"无内容"
            # → send-button 不渲染）。用页面内 execCommand insertText（走原生编辑管线）重输唤醒。
            retype = eval_js(
                '(function(){const p=document.querySelector("#prompt-textarea"); '
                'if(!p) return false; p.focus(); '
                'document.execCommand("selectAll"); document.execCommand("delete"); '
                'document.execCommand("insertText", false, ' + json.dumps(msg, ensure_ascii=False) + '); '
                'return true;})()'
            )
            if retype:
                print("  ⚙️ no_btn→execCommand 重输唤醒编辑器（2026-08-23 新UI）", flush=True)
                time.sleep(2)
                s = click_send()
        if s == "sent":
            # 发送后确认（5 次×5s=25s 窗口，p12d 修正③：连跑/上传附件后发送链路慢）
            # 判据优先级（带附件消息优先生成态确认，user 计数降为辅助）：
            # 1. 生成态（编辑器清空 + send-button 消失）  2. 编辑器清空（send-button 残留兜底）
            # 3. assistant 消息数增加（GPT 已开始回复=发送必然成功，不受 user 渲染延迟影响）
            # 4. user 数增加（仅纯文本 allow_refresh=True，附件上屏会误增 user 不可靠）
            confirmed = False
            for _ in range(5):
                time.sleep(5)
                st = eval_js("(() => { const ed = document.querySelector('[contenteditable=true]'); const sb = document.querySelector('[data-testid=send-button]'); return { edLen: ed ? (ed.innerText || '').length : -1, hasSend: !!sb }; })()")
                editor_cleared = isinstance(st, dict) and st.get("edLen") == 0 and isinstance(ed_before, int) and ed_before > 0
                if editor_cleared and not st.get("hasSend"):
                    print("    ✓ 确认进入生成态（编辑器清空+发送按钮消失）", flush=True)
                    confirmed = True
                    break
                if editor_cleared:
                    print("    ✓ 确认编辑器已清空（消息发送成功）", flush=True)
                    confirmed = True
                    break
                am = assistant_count()
                if isinstance(am, int) and am > am_before:
                    print(f"    ✓ 确认 GPT 已开始回复（am {am_before}→{am}）", flush=True)
                    confirmed = True
                    break
                now = user_msg_count()
                if allow_refresh and isinstance(now, int) and now > base:
                    print(f"    ✓ 确认 user 消息上屏 (+{now - base})", flush=True)
                    confirmed = True
                    break
            if confirmed:
                return True
            print(f"  发送后未确认到上屏(第{attempt+1}次)，刷新页面重试", flush=True)
        else:
            print(f"  发送按钮未出现(第{attempt+1}次)，刷新页面重试（no_btn 方案1：刷新后草稿保留可恢复）", flush=True)
        # 刷新重试（no_btn 方案1：刷新后草稿/附件保留，重输覆盖；send-button 恢复）
        try:
            eval_js("location.reload()")
        except Exception:
            pass
        time.sleep(12)  # F6：刷新后等 10-15s
        if not wait_editor(timeout=15):
            print("  ❌ 刷新后编辑器未出现，放弃本张", flush=True)
            return False
        # 需求1（no_btn 补强）：刷新后先检查附件是否还在，丢失则重新上传 → 再确认编辑器 → 再发送
        if refresh_hook is not None:
            refresh_hook()
        r = post("/type", {"text": msg})
        if isinstance(r, dict) and r.get("err"):
            print(f"  ❌ 重输 /type 失败: {r['err']}", flush=True)
            return False
        time.sleep(2)
    return False


def do_download(out_path, prev_gen):
    """下载成品图（修正4）：下载前再确认成品图存在；
    下载失败检查 r.ok，return False 并删除半成品文件（防 --skip-existing 永久跳过补不回）"""
    st = state()
    if st.get("gen", 0) <= prev_gen:
        print("  ❌ 下载前确认：成品图数量未增加，跳过下载", flush=True)
        return False
    r = post("/download-latest-image", {"outputPath": str(out_path), "minWidth": 300})
    if not (isinstance(r, dict) and r.get("ok") is True):
        print(f"  ❌ 下载失败: {r}", flush=True)
        if out_path.exists():
            try:
                out_path.unlink()
                print(f"  ✅ 已删除半成品: {out_path.name}", flush=True)
            except Exception as e:
                print(f"  ⚠️ 删除半成品失败: {e}", flush=True)
        return False
    print(f"  ✅ 下载成功: {r}", flush=True)
    return True


def upload_photo(photo):
    """第一轮：只传目标照片（示例图移到第二轮按大师传，example-images-by-master.md §3）
    补强（2026-08-12 事故）：/upload 返回 ok 但附件可能未上屏（缩略图未渲染）——
    上传后轮询 attachment_count 确认附件真的出现在输入框，否则重传（最多 3 次）。"""
    r = post("/upload", {"path": photo, "selector": "#upload-photos"})
    if not (isinstance(r, dict) and r.get("ok")):
        print(f"  ❌ 照片上传失败: {r}", flush=True)
        return False
    print(f"  ✓ 上传成功: {Path(photo).name}", flush=True)
    time.sleep(5)  # 等附件缩略图渲染/上传处理完成（连跑时 React 渲染慢）
    for attempt in range(3):
        n = attachment_count()
        if isinstance(n, int) and n >= 1:
            return True
        print(f"  ⚠️ 附件未上屏（删除按钮数={n}，第{attempt + 1}次），重新上传", flush=True)
        r = post("/upload", {"path": photo, "selector": "#upload-photos"})
        if not (isinstance(r, dict) and r.get("ok")):
            print(f"  ❌ 重传失败: {r}", flush=True)
            return False
        time.sleep(5)
    print("  ❌ 附件多次上传仍未上屏，放弃本张（防无照片参考出图）", flush=True)
    return False


def upload_master_example(master, no_examples=False):
    """第二轮：按大师传 1 张示例图（主图→备选→容错继续，修正⑤）。
    返回上传的文件名或 None（未上传/失败）。"""
    if no_examples or not master:
        return None
    mapping = MASTER_EXAMPLE_IMAGES.get(master)
    if not mapping:
        print(f"  ℹ️ 大师 {master} 无示例图，仅文字注入（无图）", flush=True)
        return False
    for key in ("main", "alt"):
        p = Path(mapping[key])
        if not p.exists():
            continue
        r = post("/upload", {"path": str(p), "selector": "#upload-photos"})
        if isinstance(r, dict) and r.get("ok"):
            print(f"  ✓ 已上传大师示例图: {master} {p.name}", flush=True)
            time.sleep(5)  # 等附件缩略图渲染 + 生成态过渡（连跑时按钮渲染慢）
            return p.name
        print(f"  ⚠️ 大师示例图上传失败({p.name}): {r}", flush=True)
    print(f"  ⚠️ 大师 {master} 示例图全部上传失败，容错继续（无示例图）", flush=True)
    return None



def process_one(photo, out_path, template, template_type, anchor, mode="three-phase", round2_template=None, no_examples=False, auto_switch=False, face_level="auto", expression=None):
    """单张照片处理。mode=three-phase（默认）：①选大师→②重新设计→③出图；mode=two-phase：现行 E 行为（回归/对比用）。
    降级矩阵（修正⑦）：master 有效（无论 mods 空否）→ 三阶段；master 无效 + has_prompt → 懒降级 E 旧路径；都无 → 失败本张。
    face_level：--face-level 强制指令（B 方案，默认 auto=GPT 按 round2 模板规则自主判断 L0/L1）。
    expression（v2.3）：--expression 人工修正表情行（覆盖 round1 表情行，None → 用 r1 解析值）。"""
    name = Path(photo).stem
    if out_path.exists():
        print(f"[{name}] 已存在，跳过（断点续跑）")
        return True
    print(f"===== [{name}] =====", flush=True)
    # ① 回主页（= 新对话起点）+ 等编辑器 + 确认新对话（F6 补充2）
    r = post("/navigate", {"url": "https://chatgpt.com/"})
    if isinstance(r, dict) and r.get("err"):
        print(f"  ❌ navigate 失败: {r['err']}", flush=True)
        return False
    if not wait_new_conversation(timeout=45):
        print("  ❌ 新对话未就绪（编辑器 45s 未出现或旧对话残留），放弃本张", flush=True)
        return False
    print("  ✓ 已回主页，新对话就绪（user 消息=0）", flush=True)
    # ② 第一轮只传目标照片（示例图按大师在第二轮传，example-images-by-master.md §3）
    if not upload_photo(photo):
        return False
    time.sleep(2)
    # ③ 第一轮：风格模板（修正④：带附件消息 allow_refresh=False，刷新会丢附件→确认失败直接失败本张；
    #    需求1补强：刷新后检查照片附件，丢失则重传）
    if not send_msg(template, allow_refresh=False, refresh_hook=make_photo_hook(photo)):
        print("  ❌ 第一轮发送失败", flush=True)
        return False
    print("  ✓ 第一轮已发送，等待回复...", flush=True)
    if not wait_design(0, timeout=180):
        print("  ❌ 第一轮超时", flush=True)
        return False
    text1 = last_assistant()
    print(f"  ✓ 第一轮回复 {len(text1)}字 | 首行: {text1.splitlines()[0][:60] if text1.splitlines() else ''}", flush=True)

    if mode == "two-phase":
        # ── 现行 E 行为（回归/对比用）──
        prompt = extract_prompt(text1, template_type, anchor)
        print(f"  提取结果: {len(prompt)}字 | 前80字: {prompt[:80]}", flush=True)
        if len(prompt) < 100:
            print("  ❌ 提示词提取失败（<100字），打印完整回复供人工核对：", flush=True)
            print(text1, flush=True)
            return False
        print(f"  ✓ 提示词 OK ({len(prompt)}字)", flush=True)
        master = extract_master(text1)
        mods = ""
        if master:
            mods = get_master_modifiers(master)
            if mods:
                print(f"  ✓ 注入大师风格: {master} ({len(mods)}字)", flush=True)
            else:
                print(f"  ⚠️ 大师 {master} 无风格文本，round2 用匿名占位", flush=True)
        else:
            print("  ⚠️ 未检测到选定大师行/大师不在锚点库，跳过注入", flush=True)
        msg2 = build_msg2(prompt, mods)
        prev_gen = state().get("gen", 0)
        if not send_msg(msg2):
            print("  ❌ 第二轮发送失败", flush=True)
            return False
        print(f"  ✓ 第二轮已发送（prev_gen={prev_gen}），等待出图...", flush=True)
        if not wait_image(prev_gen, timeout=240):
            if _retry_after_quota(photo, out_path, template, template_type, anchor, mode, round2_template, no_examples, auto_switch, face_level):
                return True
            print("  ❌ 出图超时", flush=True)
            return False
        return do_download(out_path, prev_gen)

    # ── three-phase（默认）：④ 第一轮解析 → ⑤⑥ 第二轮重新设计 → ⑦⑧ 第三轮出图 ──
    r1 = extract_round1(text1, template_type, anchor)
    master = r1["master"]
    mood = r1["mood"]
    print(f"  第一轮解析: master={master} mood={mood!r} has_prompt={r1['has_prompt']}", flush=True)
    if master:
        # 修正⑦：master 有效（无论 mods 空否、无论 has_prompt）→ 走三阶段（尊重用户意图）
        mods = get_master_modifiers(master)
        if mods:
            print(f"  ✓ 选定大师: {master}（完整风格 {len(mods)}字）", flush=True)
        else:
            print(f"  ⚠️ 大师 {master} 无风格文本，round2 用匿名占位", flush=True)
        # 第二轮发送前：按大师上传 1 张示例图（主图→备选→容错；修正②④）
        upload_master_example(master, no_examples)
        msg2 = build_round2_msg(master, mods, mood, has_image=bool(MASTER_EXAMPLE_IMAGES.get(master)), template_path=round2_template, face_level=face_level,
                                features=r1["features"], expression=expression or r1["expression"], paradox=r1.get("paradox"))
        if not send_msg(msg2, allow_refresh=False, refresh_hook=make_example_hook(master, no_examples)):  # 需求1：刷新后示例图附件丢失则重传
            print("  ❌ 第二轮（重新设计）发送失败", flush=True)
            return False
        print("  ✓ 第二轮已发送（重新设计），等待回复...", flush=True)
        # 修正④：prev_len=第一轮长度；超时打警告继续（防流式停顿提前判稳/误判超时）
        if not wait_design(len(text1), timeout=180):
            st = state()
            print(f"  ⚠️ 第二轮判稳超时（am={st.get('am')} lastLen={st.get('lastLen')}，可能第一轮超长或流式停顿），按当前内容继续", flush=True)
        text2 = last_assistant()
        if len(text2) < len(text1):
            print(f"  ⚠️ sanity: 第二轮回复({len(text2)}字)短于第一轮({len(text1)}字)，可能流式中断/截断", flush=True)
        prompt = extract_prompt(text2, template_type, anchor)
        print(f"  第二轮提取: {len(prompt)}字 | 前80字: {prompt[:80]}", flush=True)
        if len(prompt) < 100:
            print("  ❌ 第二轮提示词提取失败（<100字），打印完整回复供人工核对：", flush=True)
            print(text2, flush=True)
            return False
        print(f"  ✓ 第二轮提示词 OK ({len(prompt)}字)", flush=True)
        msg2 = build_msg2(prompt, "")  # 修正⑤/⑧：完整风格已融入设计，第三轮不再尾部内联
    elif r1["has_prompt"]:
        # 懒降级：master 无效但第一轮含完整提示词 → E 旧路径（不浪费产出）
        print("  ⚠️ 未检出选定大师但第一轮含完整提示词 → 懒降级走 E 旧路径", flush=True)
        prompt = extract_prompt(text1, template_type, anchor)
        print(f"  提取结果: {len(prompt)}字 | 前80字: {prompt[:80]}", flush=True)
        if len(prompt) < 100:
            print("  ❌ 提示词提取失败（<100字），打印完整回复供人工核对：", flush=True)
            print(text1, flush=True)
            return False
        print(f"  ✓ 提示词 OK ({len(prompt)}字)", flush=True)
        msg2 = build_msg2(prompt, "")  # master=None → 不注入 mods
    else:
        # 两者皆无 → 失败本张（断点续跑可重试）
        print("  ❌ 第一轮既无选定大师也无完整提示词（模型完全跑偏），打印完整回复：", flush=True)
        print(text1, flush=True)
        return False
    # ⑦⑧ 第三轮：出图（修正⑤：prev_gen 在发送前取，防第二轮违规出图被误当第三轮图）
    prev_gen = state().get("gen", 0)
    if not send_msg(msg2):
        print("  ❌ 第三轮发送失败", flush=True)
        return False
    print(f"  ✓ 第三轮已发送（prev_gen={prev_gen}），等待出图...", flush=True)
    if not wait_image(prev_gen, timeout=240):
        if _retry_after_quota(photo, out_path, template, template_type, anchor, mode, round2_template, no_examples, auto_switch, face_level):
            return True
        print("  ❌ 出图超时", flush=True)
        return False
    # 下载（修正4：检查 r.ok + 删半成品）
    return do_download(out_path, prev_gen)


def process_pipeline(photos, out_dir, template, template_type, anchor, suffix="-gpt-mondo", mode="two-phase",
                     round2_template=None, no_examples=False, face_level="auto", expression=None):
    """pipeline 流水线模式（需求2）：**保持单标签页**（多标签并行触发免费版限流铁律）。
    Phase A：逐张（上传→第一轮设计→第二轮发图指令→记录对话 cid）→ 全部发完（发完即 navigate 走，后台出图）；
    Phase B：从后往前回各对话 → wait_image → 下载（/download-latest-image）。
    关键假设（用户已实测确认）：发图指令后 navigate 走（对话不在前台），ChatGPT 后台继续生成。
    断点续跑：Phase A 完成写 .pipeline_jobs.json；重跑时若存在则直接进 Phase B；
    Phase B 每下载成功一个即从 jobs 移除并写回，中断后重跑只补剩余。"""
    out_dir = Path(out_dir)
    jobs_file = out_dir / ".pipeline_jobs.json"
    jobs = []
    if jobs_file.exists():
        try:
            jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
            print(f"  📂 检测到未完成 pipeline jobs（{len(jobs)} 个），直接进入下载阶段", flush=True)
        except Exception as e:
            print(f"  ⚠️ jobs 文件解析失败: {e}，重新 Phase A", flush=True)
            jobs = []
    if not jobs:
        # ── Phase A：全部照片发完图指令（切走后台出图）──
        print("===== Phase A: 逐张发设计+图指令（切走后台出图） =====", flush=True)
        for i, photo in enumerate(photos, 1):
            name = Path(photo).stem
            out = out_dir / f"{i:02d}-{name}{suffix}.png"
            if out.exists():
                print(f"[{name}] 已存在，跳过（断点续跑）", flush=True)
                continue
            print(f"===== [{name}] =====", flush=True)
            r = post("/navigate", {"url": "https://chatgpt.com/"})
            if isinstance(r, dict) and r.get("err"):
                print(f"  ❌ navigate 失败: {r['err']}", flush=True)
                continue
            if not wait_new_conversation(timeout=45):
                print("  ❌ 新对话未就绪（45s 超时或旧对话残留），放弃本张", flush=True)
                continue
            if not upload_photo(photo):
                continue
            if not send_msg(template, allow_refresh=False, refresh_hook=make_photo_hook(photo)):
                print("  ❌ 第一轮发送失败", flush=True)
                continue
            if not wait_design(0, timeout=180):
                print("  ❌ 第一轮超时", flush=True)
                continue
            text1 = last_assistant()
            print(f"  ✓ 第一轮回复 {len(text1)}字 | 首行: {text1.splitlines()[0][:50] if text1.splitlines() else ''}", flush=True)
            if mode == "two-phase":
                prompt = extract_prompt(text1, template_type, anchor)
                if len(prompt) < 100:
                    print("  ❌ 提示词提取失败（<100字），跳过本张", flush=True)
                    continue
                print(f"  ✓ 提示词 OK ({len(prompt)}字)", flush=True)
                msg2 = build_msg2(prompt, "")
            else:
                r1 = extract_round1(text1, template_type, anchor)
                master = r1["master"]
                if master:
                    mods = get_master_modifiers(master)
                    upload_master_example(master, no_examples)
                    msg2 = build_round2_msg(master, mods, r1["mood"], has_image=bool(MASTER_EXAMPLE_IMAGES.get(master)),
                                            template_path=round2_template, face_level=face_level,
                                            features=r1["features"], expression=expression or r1["expression"], paradox=r1.get("paradox"))
                    if not send_msg(msg2, allow_refresh=False, refresh_hook=make_example_hook(master, no_examples)):
                        print("  ❌ 第二轮（重新设计）发送失败", flush=True)
                        continue
                    if not wait_design(len(text1), timeout=180):
                        print("  ⚠️ 第二轮判稳超时，按当前内容继续", flush=True)
                    text2 = last_assistant()
                    prompt = extract_prompt(text2, template_type, anchor)
                    if len(prompt) < 100:
                        print("  ❌ 第二轮提示词提取失败，跳过本张", flush=True)
                        continue
                    msg2 = build_msg2(prompt, "")
                elif r1["has_prompt"]:
                    prompt = extract_prompt(text1, template_type, anchor)
                    if len(prompt) < 100:
                        print("  ❌ 提示词提取失败，跳过本张", flush=True)
                        continue
                    msg2 = build_msg2(prompt, "")
                else:
                    print("  ❌ 第一轮无大师也无完整提示词，跳过本张", flush=True)
                    continue
            # 发图指令 + 记录 cid（切走后台出图）
            prev_gen = state().get("gen", 0)
            if not send_msg(msg2):
                print("  ❌ 图指令发送失败，跳过本张", flush=True)
                continue
            cid = current_cid()
            if not cid:
                print("  ❌ 未取到对话 cid（无法回访下载），跳过本张", flush=True)
                continue
            print(f"  ✓ 图指令已发，对话 {cid[:8]}... 后台出图中（切走继续）", flush=True)
            jobs.append({"cid": cid, "out": str(out), "prev_gen": prev_gen, "name": name})
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs_file.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"===== Phase A 完成：{len(jobs)} 个对话在后台出图 =====", flush=True)
    # ── Phase B：从后往前回各对话下载 ──
    print("===== Phase B: 从后往前回各对话下载 =====", flush=True)
    ok = fail = 0
    for job in list(reversed(jobs)):
        print(f"===== [下载] {job['name']} ({job['cid'][:8]}...) =====", flush=True)
        r = post("/navigate", {"url": f"https://chatgpt.com/c/{job['cid']}"})
        if isinstance(r, dict) and r.get("err"):
            print(f"  ❌ navigate 失败: {r['err']}", flush=True)
            fail += 1
            continue
        time.sleep(6)  # 等消息加载
        if not wait_image(job["prev_gen"], timeout=240):
            print("  ❌ 出图超时", flush=True)
            fail += 1
            continue
        if do_download(Path(job["out"]), job["prev_gen"]):
            ok += 1
            jobs.remove(job)
            jobs_file.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            fail += 1
    if not jobs:
        try:
            jobs_file.unlink(missing_ok=True)
        except Exception:
            pass
    print(f"\n完成：✅ {ok} 张 | ❌ {fail} 张失败")
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("photos", nargs="+")
    ap.add_argument("--out", default=r"C:\Users\Administrator\Desktop\p12text\出图-8-9故事-gpt直出-mondo")
    ap.add_argument("--template", default=r"F:\xianyu-workspace\project-12-story-poster\plans\gpt-direct-round1-template-mondo.txt")
    ap.add_argument("--template-type", default="mondo", choices=["mondo", "distillation-zine", "distillation-zine-full", "gathered-zine-full", "gc-minimal-zine", "gathered-zine-a0", "gathered-zine-short", "gathered-zine-evolved", "gathered-zine-evolved-v2", "gathered-zine-evolved-v3", "mondo-evolved", "gathered-zine-evolved-v4-landscape"])
    ap.add_argument("--anchor", default="in Mondo poster style")
    ap.add_argument("--suffix", default="-gpt-mondo")
    ap.add_argument("--mode", default="three-phase", choices=["three-phase", "two-phase"],
                    help="three-phase=选大师→重新设计→出图（默认）；two-phase=现行 E 方案（回归/对比）")
    ap.add_argument("--round2-template", default=None,
                    help="round2 模板路径（默认 plans/gpt-direct-round2-template-mondo.txt）")
    ap.add_argument("--no-examples", action="store_true",
                    help="关闭全部示例图注入（含第二轮按大师示例图；A/B 对比用）。默认开：第二轮按 MASTER_EXAMPLE_IMAGES 映射传该大师示例图")
    ap.add_argument("--auto-switch-account", action="store_true",
                    help="出图超时且检测到限额时自动切到另一账号并重试本张（默认 False）")
    ap.add_argument("--face-level", default="auto", choices=["auto", "L0", "L1", "L2"],
                    help="脸部特征档位强制指令（B 方案）：auto=GPT 按 round2 模板规则自主判断（默认）；L0=全剪影；L1=简化特征；L2=半写实剪影（客户明确要求时人工指定）")
    ap.add_argument("--expression", default=None,
                    help="人工修正表情行（v2.3 照片灵魂保真；覆盖 round1 表情行，可选；格式如 '嘴=嘴角上扬+露齿、眼=弯月、眉=平'）")
    ap.add_argument("--pipeline", action="store_true",
                    help="pipeline 流水线模式（需求2）：全部照片发完图指令后从后往前逐个下载（出图等待时间重叠，保持单标签页）；默认串行")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    template = Path(args.template).read_text(encoding="utf-8")

    print(post("/connect", {"port": 9224}), flush=True)
    if args.pipeline:
        process_pipeline(args.photos, out_dir, template, args.template_type, args.anchor, args.suffix,
                         args.mode, args.round2_template, args.no_examples, args.face_level, args.expression)
        return
    ok = fail = 0
    total = len(args.photos)
    for i, photo in enumerate(args.photos, 1):
        out = out_dir / f"{i:02d}-{Path(photo).stem}{args.suffix}.png"
        try:
            if process_one(photo, out, template, args.template_type, args.anchor, args.mode, args.round2_template, args.no_examples, args.auto_switch_account, args.face_level, args.expression):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            print(f"  ❌ [{Path(photo).stem}] 异常: {e}", flush=True)
        # F6：每 2-3 张休息 30-60s（限流），其余间隔 3s
        if i % 3 == 0 and i < total:
            print(f"  --- 限流休息 45s（{i}/{total}）---", flush=True)
            time.sleep(45)
        else:
            time.sleep(3)
    print(f"\n完成：✅ {ok} 张 | ❌ {fail} 张失败")


if __name__ == "__main__":
    main()
