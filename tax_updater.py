#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
唯特偶税务数据库 · 自动更新器
================================
抓取中国及境外税务机关的可解析公开源（政策库 / 新闻稿 / RSS），
自动去重合并写入 tax_updates_archive.json。
配合 GitHub Actions 定时运行（见 .github/workflows/update.yml），用户无需手动改文件。

用法：
  python3 tax_updater.py            # 正常运行（合并写入）
  python3 tax_updater.py --dry      # 试运行（只打印，不写文件）
  python3 tax_updater.py --max 60   # 归档最大条数（默认 40）
  python3 tax_updater.py --today 2026-08-20   # 指定“今天”日期（测试用）

说明：
  - 只更新“每日更新归档”（政策/公告/新闻类，自动可信度可保证）。
  - “税务处罚案例”（tax_cases_archive.json）涉及事实认定、金额字号与定性，
    为保证准确，不自动抓取，仍由人工审核后录入。
  - 各源互不影响：单个源超时/失败只记录警告，不影响其他源正常抓取。
"""
import json, re, sys, os, argparse, datetime, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

CHINATAX_BASE = "https://fgk.chinatax.gov.cn"
ARCHIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tax_updates_archive.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 抓取源配置：
#   kind   = publist(政策库分页) / rss(Atom或RSS) / html(新闻列表页正则)
#   juris  = 司法辖区；tags = 归档标签；prefer_today = 是否按“今天”录入
SOURCES = [
    {
        "name": "国家税务总局·政策指引库（最新发布）",
        "kind": "publist",
        "cid": "c100022",
        "page": 2,
        "base": CHINATAX_BASE,
        "juris": "中国内地",
        "tags": ["税务总局", "指引"],
        "prefer_today": True,
    },
    {
        "name": "美国国税局 IRS·新闻稿",
        "kind": "html",
        "url": "https://www.irs.gov/newsroom",
        "base": "https://www.irs.gov",
        "pattern": r'<h3[^>]*>\s*<a href="(/newsroom/[^"]+)"[^>]*>([\s\S]*?)</a>',
        "juris": "美国",
        "tags": ["IRS", "美国"],
        "prefer_today": True,
        "limit": 8,
    },
    {
        "name": "英国税务及海关总署 HMRC·最新动态",
        "kind": "rss",
        "url": "https://www.gov.uk/government/organisations/hm-revenue-customs.atom",
        "base": "https://www.gov.uk",
        "juris": "英国",
        "tags": ["HMRC", "英国"],
        "prefer_today": False,
        "limit": 8,
    },
    {
        "name": "美国税务基金会 Tax Foundation·税制研究",
        "kind": "rss",
        "url": "https://taxfoundation.org/feed/",
        "base": "https://taxfoundation.org",
        "juris": "美国",
        "tags": ["美国", "税制研究"],
        "prefer_today": False,
        "limit": 8,
    },
]

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/atom+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")

def norm_url(u, base):
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http"):
        return u
    return base + (u if u.startswith("/") else "/" + u)

def fetch_publist(cid, page=1):
    """抓取 fgk.chinatax.gov.cn 政策库分类发布列表页，返回 [{title,url}]"""
    if page == 1:
        url = f"{CHINATAX_BASE}/zcfgk/{cid}/publist.html"
    else:
        url = f"{CHINATAX_BASE}/zcfgk/{cid}/publist_{page}.html"
    try:
        html = http_get(url)
    except Exception:
        return []
    items = []
    for href, body in re.findall(r'<li>\s*<a href="([^"]+)"[^>]*>\s*<p class="xh">[^<]*</p>([\s\S]*?)</a>\s*</li>', html):
        bt = re.search(r'<p class="bt">([\s\S]*?)</p>', body)
        if not bt:
            continue
        title = re.sub(r"\s+", " ", bt.group(1)).strip()
        if title:
            items.append({"title": title, "url": norm_url(href, CHINATAX_BASE)})
    return items

def _local(tag):
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]

def fetch_rss(url):
    """抓取 Atom/RSS 源，返回 [{title,url}]（只取直接子节点，避免嵌套污染）"""
    try:
        data = http_get(url)
    except Exception:
        return []
    items = []
    try:
        root = ET.fromstring(data)
        for node in root.iter():
            if _local(node.tag) not in ("item", "entry"):
                continue
            title, link = "", ""
            for child in node:
                name = _local(child.tag)
                if name == "title" and not title:
                    title = " ".join(child.itertext()).strip()
                elif name == "link":
                    if child.get("href"):
                        link = child.get("href")
                    elif child.text and child.text.strip():
                        link = child.text.strip()
            if title and link:
                items.append({"title": title, "url": norm_url(link, "")})
    except Exception:
        return []
    return items

def fetch_html(url, pattern, base=""):
    """抓取新闻列表页，用正则提取 [{title,url}]（pattern 需含两个捕获组：href、标题）"""
    try:
        html = http_get(url)
    except Exception:
        return []
    items = []
    for href, body in re.findall(pattern, html):
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        if title:
            items.append({"title": title, "url": norm_url(href, base or url)})
    return items

def load_existing():
    if not os.path.exists(ARCHIVE):
        return {"updated": None, "archives": []}, []
    with open(ARCHIVE, encoding="utf-8") as f:
        doc = json.load(f)
    items = doc.get("archives") or doc.get("items") or doc.get("updates") or []
    return doc, items

def save(items, meta_updated):
    doc = {"updated": meta_updated, "count": len(items), "archives": items}
    tmp = ARCHIVE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ARCHIVE)
    return doc

def fetch_source(src):
    """按 kind 抓取单个源，返回条目列表 [{title,url}] 或空列表"""
    kind = src.get("kind")
    if kind == "publist":
        rows = []
        for pg in range(1, (src.get("page") or 1) + 1):
            rows += fetch_publist(src.get("cid"), pg)
        return rows
    if kind == "rss":
        return fetch_rss(src["url"])
    if kind == "html":
        return fetch_html(src["url"], src["pattern"], src.get("base", ""))
    return []

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只做抓取与比对，不写入")
    ap.add_argument("--max", type=int, default=40, help="归档保留的最大条数")
    ap.add_argument("--today", default=datetime.date.today().isoformat())
    args = ap.parse_args()

    today = args.today
    existing_doc, existing = load_existing()
    seen = {it.get("url", "") for it in existing}

    new_items, total, failed = [], 0, []
    for src in SOURCES:
        try:
            rows = fetch_source(src)
        except Exception as e:
            rows = []
            failed.append(f"{src['name']}: {e}")
        limit = src.get("limit")
        if limit and rows:
            rows = rows[:limit]
        if not rows:
            failed.append(f"{src['name']}: 未解析到任何条目")
        for r in rows:
            total += 1
            if r["url"] and r["url"] in seen:
                continue
            if r["url"]:
                seen.add(r["url"])
            new_items.append({
                "date": today,
                "juris": src.get("juris") or "海外",
                "title": r["title"],
                "summary": "来源：" + src["name"] + "。点击原文查阅全文。",
                "tags": src.get("tags") or [],
                "url": r["url"],
            })

    merged = sorted(existing + new_items,
                    key=lambda x: (x.get("date") or "0000-00-00", x.get("title") or ""),
                    reverse=True)[: args.max]

    print("=" * 60)
    print(f"抓取日期 : {today}")
    print(f"扫描来源 : {len(SOURCES)} → 扫描到 {total} 条，新收录 {len(new_items)} 条")
    if failed:
        print("跳过/失败:")
        for f in failed:
            print("   ⚠", f)
    print(f"归档合计 : {len(merged)} 条（上限 {args.max}）")
    print("新收录示例:")
    for it in new_items[:5]:
        print("   ·", it["title"][:48], "→", it["url"][:70])
    if not args.dry and new_items:
        save(merged, today)
        print("已写入", ARCHIVE)
    elif not new_items:
        print("无新增条目，未改动文件。")
    else:
        print("(--dry) 未写入文件。")
    print("=" * 60)

if __name__ == "__main__":
    main()