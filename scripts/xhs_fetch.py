#!/usr/bin/env python3
"""从「小红书」分享链接中提取图文笔记的正文与全部图片。

用法:
    python xhs_fetch.py <链接> [--out <输出目录>]

链接形式（三种都支持）:
    - 短链:  https://xhslink.cn/o/4jZDwSJjlOg
    - 长链:  https://www.xiaohongshu.com/discovery/item/<note_id>?xsec_token=...
    - 纯 ID: 6ad06c79000000000a01dc2f
             （⚠️ 纯 ID 通常缺 xsec_token，可能抓不到内容 —— 优先用带 token 的分享链接）

输出:
    <out>/desc.txt       笔记正文 + 标题
    <out>/URLS.txt       全部图片地址（按页序）
    <out>/img/01.jpg ... 内容页图片（已过滤缩略图、已按 MD5 去重）
    stdout               一份可直接读的报告（含图片清单与尺寸）

设计要点（踩坑换来的，改动前请先读 SKILL.md 的说明）:
    1. 必须用 iPhone UA —— PC UA 拿到的 HTML 结构不同
    2. 短链要先跟重定向拿 note_id
    3. 正文在 HTML 的 "desc" 字段里，但**必须用 json.loads 解析**，
       用 unicode_escape 会把字面 UTF-8 的中文吃掉（曾踩过）
    4. 图片字段名是 "url"（不是 urlDefault / urlPre）
    5. 图片地址是 http://，改 https 更稳
    6. 每张图有 2~3 个 URL 变体：
         `!h5` / `!h5_1080jpg`  内容页大图  ← 要的
         `!st`                  缩略图
         `!style_<hash>`        装饰图
       —— 用**包含** `!h5` 来筛（后缀名会变，别用 endswith）；没命中时回退"按体积过滤"
"""

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.request

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
HEADERS = {"User-Agent": IPHONE_UA, "Referer": "https://www.xiaohongshu.com/"}
MIN_CONTENT_BYTES = 30 * 1024  # 小于这个体积的，基本是缩略图/占位图


def log(msg=""):
    print(msg, flush=True)


def fetch(url, timeout=40):
    """抓取 URL，返回 str。自动 http→https 重试。"""
    last = None
    for candidate in (url, re.sub(r"^http://", "https://", url)):
        try:
            req = urllib.request.Request(candidate, headers=HEADERS)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read().decode("utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"抓取失败: {last}")


def fetch_bytes(url, timeout=40):
    for candidate in (re.sub(r"^http://", "https://", url), url):
        try:
            req = urllib.request.Request(candidate, headers=HEADERS)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            continue
    return None


def resolve_url(raw):
    """把短链 / 纯 ID 规整成可抓取的 URL。"""
    raw = raw.strip()
    if raw.startswith("http"):
        return raw
    if re.fullmatch(r"[0-9a-f]{16,}", raw):  # 看起来是 note_id
        return f"https://www.xiaohongshu.com/discovery/item/{raw}"
    raise SystemExit(f"无法识别的输入: {raw}")


def _json_field(html, key):
    """取出 HTML 内嵌 JSON 里某个字符串字段，并正确反转义。

    ⚠️ 两个坑：
    1. 必须用 json.loads 反转义。用 unicode_escape 会把字面 UTF-8 的中文吃掉。
    2. 结束符要用通用的 `","`，**不要枚举** `type|noteId|...` —— 枚举会漏掉
       `desc` 这类键，导致 title 一路吞到几百字之后（实测踩过）。
       JSON 字符串内部的引号一定转义成 `\\"`，所以 `","` 不会误判。
    """
    for pattern in (r'"%s":"(.*?)","', r'"%s":"(.*?)"\}'):
        m = re.search(pattern % key, html, re.S)
        if m:
            try:
                return json.loads('"' + m.group(1) + '"')
            except Exception:  # noqa: BLE001
                return m.group(1)
    return ""


def parse_note(html):
    """从 HTML 中取出 标题 / 正文 / note_id / 图片 URL 列表。"""
    out = {"title": "", "desc": "", "note_id": "", "images": []}

    out["title"] = _json_field(html, "title")
    out["desc"] = _json_field(html, "desc")

    m = re.search(r'"noteId":"([^"]+)"', html)
    if m:
        out["note_id"] = m.group(1)

    # —— 图片 URL ——
    idx = html.find('"imageList"')
    seg = html[idx : idx + 60000] if idx != -1 else html
    urls = re.findall(r'"(?:url|urlDefault|urlPre|masterUrl)":"(http[^"]+)"', seg)
    urls = [u.replace("\\u002F", "/") for u in urls]
    urls = [u for u in urls if "xhscdn" in u]
    out["images"] = list(dict.fromkeys(urls))
    return out


def pick_content_urls(urls):
    """优先选内容页大图。

    ⚠️ 大图后缀在不同版本/不同笔记里会变，实测见过：
        `!h5`        （面试题截图类）
        `!h5_1080jpg`（长图类）
        缩略图/装饰图则是 `!st`、`!style_<hash>`
    所以用**包含**匹配 `!h5`，不要用 endswith —— 后者曾漏判。
    没有命中时原样返回，交给体积过滤兜底。
    """
    h5 = [u for u in urls if "!h5" in u]
    return h5 if h5 else urls


def download_images(urls, outdir, skip_small_instead_of_dedupe):
    """下载图片。返回 [(文件名, 字节数)]。

    skip_small=True 时按体积过滤（适合"没有 !h5 标记"的情况），
    否则按 MD5 去重（每个 fileId 会有 h5/st 两个变体）。
    """
    os.makedirs(outdir, exist_ok=True)
    saved, seen = [], set()
    for u in urls:
        data = fetch_bytes(u)
        if not data:
            continue
        if skip_small_instead_of_dedupe:
            if len(data) < MIN_CONTENT_BYTES:
                continue
        else:
            h = hashlib.md5(data).hexdigest()
            if h in seen:
                continue
            seen.add(h)
        name = f"{len(saved) + 1:02d}.jpg"
        with open(os.path.join(outdir, name), "wb") as f:
            f.write(data)
        saved.append((name, len(data)))
    return saved


def main():
    ap = argparse.ArgumentParser(description="提取小红书图文笔记正文与图片")
    ap.add_argument("url", help="分享链接（短链/长链/note_id）")
    ap.add_argument("--out", default="xhs_note", help="输出目录（默认 ./xhs_note）")
    args = ap.parse_args()

    url = resolve_url(args.url)

    log("=" * 56)
    log("① 抓取页面")
    html = fetch(url)
    log(f"   HTML 大小: {len(html)} 字节")

    log("② 解析内容")
    note = parse_note(html)
    log(f"   note_id : {note['note_id'] or '(未取到)'}")
    log(f"   标题    : {note['title'] or '(无)'}")
    log(f"   正文长度: {len(note['desc'])} 字")
    log(f"   候选图片: {len(note['images'])} 个 URL")

    os.makedirs(args.out, exist_ok=True)

    with open(os.path.join(args.out, "desc.txt"), "w", encoding="utf-8") as f:
        f.write(note["title"] + "\n\n" + note["desc"])

    picked = pick_content_urls(note["images"])
    by_size = len(picked) == len(note["images"])  # 没命中 !h5 → 走体积过滤

    with open(os.path.join(args.out, "URLS.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(picked))

    log("③ 下载图片")
    saved = download_images(picked, os.path.join(args.out, "img"), by_size)
    log(f"   策略: {'按体积过滤' if by_size else '优先 !h5 + MD5 去重'}")

    log("④ 结果")
    log("-" * 56)
    log(f"输出目录: {args.out}")
    log(f"   desc.txt   正文（含标题）")
    log(f"   URLS.txt   {len(picked)} 个图片地址")
    log(f"   img/       共 {len(saved)} 张")
    for name, size in saved:
        log(f"     {name}   {size / 1024:.0f} KB")
    log("-" * 56)

    if not saved:
        log("⚠️ 没下到图片。常见原因：")
        log("   · 链接缺 xsec_token（用 App「复制链接」拿到完整分享链接）")
        log("   · 笔记是视频笔记（内容在视频里，不在图里）")
        log("   · 该笔记需要登录才能看")
        return 1

    log(f"\n下一步：用 Read 工具逐张读 {args.out}/img/*.jpg 取文字内容。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
