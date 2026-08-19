#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
唯特偶税务数据库 · 自动更新器
================================
从国内官方可解析源抓取最新税务政策/指引，自动去重合并写入 tax_updates_archive.json。
配合 macOS launchd 定时运行（见 com.vital.taxupdater.plist），用户无需手动改文件。

用法：
  python3 tax_updater.py            # 正常运行（合并写入）
  python3 tax_updater.py --dry      # 试运行（只打印，不写文件）
  python3 tax_updater.py --max 60   # 归档最大条数（默认 40）
  python3 tax_updater.py --today 2026-08-20   # 指定“今天”日期（测试用）

说明：
  - 只更新“每日更新归档”（政策/公告类，自动可信度可保证）。
  - “税务处罚案例”（tax_cases_archive.json）涉及事实认定、金额字号与定性，
    为保证准确，不自动抓取，仍由人工审核后录入。
  - 境外源（IRS/IRAS 等）受网络限制且无稳定 RSS，本机直连不稳，
    无法保证自动抓取；如需境外源，请使用能访问外网的环境另行配置。
"""
import json, re, sys, os, argparse, datetime, urllib.request, urllib.parse

BASE_URL = "https://fgk.chinatax.gov.cn"
ARCHIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tax_updates_archive.json")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# 抓取源配置：name = 来源说明；cid = 政策库分类；page = 抓取页数（第2页起自动试）
SOURCES = [
    {
        "name": "国家税务总局·政策指引库（最新发布）",
        "cid": "c100022",
        "page": 2,
        "tags": ["税务总局", "指引"],
        "prefer_today": True,
    },
]

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")

def norm_url(u):
    u = u.strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http"):
        return u
    return BASE_URL + (u if u.startswith("/") else "/" + u)

def fetch_publist(cid, page=1):
    """抓取 fgk.chinatax.gov.cn 政策库分类发布列表页，返回 [{title,url}]"""
    if page == 1:
        url = f"{BASE_URL}/zcfgk/{cid}/publist.html"
    else:
        url = f"{BASE_URL}/zcfgk/{cid}/publist_{page}.html"
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
            items.append({"title": title, "url": norm_url(href)})
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
        rows = []
        for pg in range(1, (src.get("page") or 1) + 1):
            try:
                rows += fetch_publist(src.get("cid"), pg)
            except Exception as e:
                failed.append(f"{src['name']} 第{pg}页: {e}")
        if not rows:
            failed.append(f"{src['name']}: 未解析到任何条目")
        for r in rows:
            total += 1
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            new_items.append({
                "date": today,
                "juris": "中国内地",
                "title": r["title"],
                "summary": "来源：" + src["name"] + "。" + ("近日发布的税收政策/指引，建议点击原文核对全文。" if src.get("prefer_today") else "政策法规库/发布列表条目，可点击原文查阅全文。"),
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