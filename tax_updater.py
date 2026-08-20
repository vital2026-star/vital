#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
唯特偶税务数据库 · 自动更新器
================================
两路自动抓取，配合 GitHub Actions 定时运行（见 .github/workflows/update.yml），无需手动改文件：

  1) 每日税政归档  tax_updates_archive.json
     抓取中国及境外税务机关的可解析公开源（政策库 / 新闻稿 / RSS），自动去重合并写入。
  2) 税务处罚案例  tax_cases_archive.json
     抓取国家税务总局「税案通报」列表（含标题/日期/原文链接），进入详情页提取事实摘要，
     按标题与正文关键词自动打上税种/风险标签后合并写入；人工录入的「案例启示 lesson」字段
     保持不变。自动抓取条目带 "auto":true 标记，页面会标注「自动抓取·供参考」。

用法：
  python3 tax_updater.py                 # 默认同时更新说明文档中的两种归档
  python3 tax_updater.py --updates       # 只更新税政归档
  python3 tax_updater.py --cases         # 只更新处罚案例
  python3 tax_updater.py --dry           # 试运行（只打印，不写文件）
  python3 tax_updater.py --max 60        # 税政归档最大条数（默认 40）
  python3 tax_updater.py --cases-max 120 # 案例归档最大条数（默认 100）
  python3 tax_updater.py --today 2026-08-20   # 指定“今天”日期（测试用）

说明：
  - 案例抓取仅采信税务机关官方原文链接与原文摘要，不自行添加事实/金额，保证可信。
  - 各源互不影响：单个源超时/失败只记录警告，不影响其他源正常抓取。
"""
import json, re, sys, os, argparse, datetime, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

CHINATAX_BASE = "https://fgk.chinatax.gov.cn"
ARCHIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tax_updates_archive.json")
CASE_ARCHIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tax_cases_archive.json")

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

# 税务处罚案例抓取源：kind=listpage(列表页：<li><a title>标题</a><span>日期</span></li>)
CASE_SOURCES = [
    {
        "name": "国家税务总局·税案通报（最新）",
        "kind": "listpage",
        "url": "https://www.chinatax.gov.cn/chinatax/n810219/c102025/common_listwyc.html?xxgkhide=1",
        "base": "https://www.chinatax.gov.cn",
        "juris": "中国内地",
        "pages": 3,          # 抓取前几页（每页约20条）
        "detail": "#zoomcon", # 详情页正文容器 id
    },
    {
        "name": "国家税务总局江苏省税务局·税案通报",
        "kind": "listpage",
        "url": "https://jiangsu.chinatax.gov.cn/col/col24019/index.html",
        "base": "https://jiangsu.chinatax.gov.cn",
        "juris": "中国内地",
        "pages": 1,
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

def load_cases():
    if not os.path.exists(CASE_ARCHIVE):
        return {"updated": None, "count": 0, "cases": []}, []
    with open(CASE_ARCHIVE, encoding="utf-8") as f:
        doc = json.load(f)
    items = doc.get("cases") or []
    return doc, items

def save_cases(items, meta_updated):
    doc = {"updated": meta_updated, "count": len(items), "cases": items}
    tmp = CASE_ARCHIVE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CASE_ARCHIVE)
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

CASE_TAX_RULES = [
    ("出口退税|骗税|骗取退税|出口退", "出口退税"),
    ("增值税专用发票|虚开|发票虚开", "虚开发票"),
    ("研发费用|加计扣除", "研发费用加计扣除"),
    ("个税|个人所得税|主播|网红|扣缴|薪酬", "个人所得税"),
    ("消费税", "消费税"),
    ("偷税|偷逃税|隐匿收入|账外|私户|资金回流|影子账本", "偷逃税/隐匿收入"),
    ("股权转让|股息|分红", "股权转让/股息分红"),
    ("预提|非居民|对外支付", "预提税/扣缴"),
    ("发票|电子发票|数电票", "发票管理"),
    ("注销|吊销|逃逸式注销", "注销/吊销合规"),
    ("高新技术企业|高新", "高新技术企业"),
    ("企业所得税|汇算", "企业所得税"),
    ("增值税", "增值税"),
]

def infer_case_tags(title, body):
    txt = (title + " " + body)[:600]
    tags = []
    for kw, tag in CASE_TAX_RULES:
        if re.search(kw, txt):
            tags.append(tag)
    tags = list(dict.fromkeys(tags))
    if "网络" in txt or "直播" in txt:
        tags.append("网络直播")
    if "税务人员" in txt or "内外勾结" in txt:
        tags.append("税务人员问责")
    if "刑事" in txt or "被判" in txt:
        tags.append("刑事追责")
    return tags[:4]

def fetch_case_list(src, pg):
    """抓取列表页第 pg 页，返回 [{title,url,date}]"""
    url = src["url"]
    if pg > 1:
        url = re.sub(r'(index\.html|listwyc\.html.*|\.html.*$)', f'index_{pg}.html', url)
        # 兼容无页码链接：附加参数形式
        if url == src["url"]:
            url = src["url"] + ("" if ("?" in url) else "?") + f"&_p={pg}"
    try:
        html = http_get(url)
    except Exception:
        return []
    items = []
    # 国家税务总局列表：<li><a href title>标题</a><span class="time">YYYY-MM-DD</span></li>
    for href, title, date in re.findall(
        r'<li>\s*<a[^>]+href="([^"]+)"[^>]*title="([^"]*)"[^>]*>(?:[\s\S]*?)</a>\s*<span[^>]*>\s*([\d]{4}-\d{2}-\d{2})\s*</span>', html):
        t = title.strip()
        if t and href:
            items.append({"title": t, "url": norm_url(href, src.get("base", "")), "date": date})
    if not items:
        # 兼容 title/href 顺序互换：<li><a title=".." href="..">..</a><span>date</span></li>
        for title_txt, href, date in re.findall(
            r'<li>\s*<a[^>]+title="([^"]*)"[^>]*href="([^"]+)"[^>]*>(?:[\s\S]*?)</a>\s*<span[^>]*>\s*([\d]{4}-\d{2}-\d{2})\s*</span>', html):
            t = title_txt.strip()
            if t and href:
                items.append({"title": t, "url": norm_url(href, src.get("base", "")), "date": date})
    return items

def fetch_case_detail(url, container_id):
    """抓取详情页正文，返回纯文本摘要（截取前 ~320 字）"""
    try:
        html = http_get(url)
    except Exception:
        return ""
    candidates = [container_id, "zoomcon", "zoom", "content", "TRS_Editor"]
    body = ""
    for cid in candidates:
        m = re.search(r'<div[^>]+id="' + re.escape(cid) + r'"[\s\S]*?</div>', html)
        if m:
            body = m.group(0)
            break
    if not body:
        # 退而求其次：取文章正文主体区
        m = re.search(r'<div[^>]*class="[^"]*(?:article-content|news-con|art-con)[^"]*"[\s\S]*?(?=<div|<script|$)', html)
        body = m.group(0) if m else html
    body = re.sub(r'<script[\s\S]*?</script>', '', body)
    body = re.sub(r'<style[\s\S]*?</style>', '', body)
    text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body)).strip()
    return text[:320]

def fetch_cases():
    """抓取全部案例源，返回 [(src, {title,url,date,summary,tax,tags,auto}]"""
    out, failed = [], []
    seen = set()
    for src in CASE_SOURCES:
        try:
            rows = []
            for pg in range(1, (src.get("pages") or 1) + 1):
                rows += fetch_case_list(src, pg)
        except Exception as e:
            rows = []
            failed.append(f"{src['name']}: {e}")
        # 去重（列表多页/多源可能重复）
        uniq_rows = []
        for r in rows:
            k = r["url"].split("?")[0]
            if k in seen:
                continue
            seen.add(k)
            uniq_rows.append(r)
        if not uniq_rows:
            failed.append(f"{src['name']}: 未解析到列表条目")
        for r in uniq_rows:
            detail_id = src.get("detail", "zoomcon")
            summary = fetch_case_detail(r["url"], detail_id) or (f"来源：{src['name']}。点击原文查阅全文。")
            title = r["title"]
            tags = infer_case_tags(title, summary)
            out.append({
                "date": r.get("date") or datetime.date.today().isoformat(),
                "juris": src.get("juris") or "中国内地",
                "tax": "、".join(tags) or "税收违法",
                "title": title,
                "summary": summary,
                "tags": tags,
                "url": r["url"],
                "auto": True,
            })
    return out, failed

def merged_cases_dedup(cases):
    """去重同一案件的“通报+揭秘”孪生条目：保留标题更短者（通常是官方通报），删除被其包含的长标题版本"""
    norm = lambda s: re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")
    keep = []
    rejected = set()
    auto = [c for c in cases if c.get("auto")]
    for i in range(len(cases)):
        c = cases[i]
        if not c.get("auto") or c["url"] in rejected:
            keep.append(c)
            continue
        ni = norm(c["title"])
        dup = False
        for o in auto:
            if o is c or o["url"] in rejected:
                continue
            no = norm(o["title"])
            if no and no != ni and (no in ni or ni in no):
                keep_short = o if len(no) < len(ni) else c
                drop = c if keep_short is o else o
                rejected.add(drop["url"])
                dup = True
                break
        if not dup:
            keep.append(c)
    return keep

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
    ap.add_argument("--cases-max", type=int, default=100, help="案例归档保留的最大条数")
    ap.add_argument("--today", default=datetime.date.today().isoformat())
    ap.add_argument("--updates", action="store_true", help="只更新税政归档")
    ap.add_argument("--cases", action="store_true", help="只更新处罚案例")
    args = ap.parse_args()

    today = args.today

    # ---------- 第一路：税政归档 ----------
    if args.updates or not args.cases:
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
        print(f"[税政归档] 抓取日期 : {today}")
        print(f"  扫描来源 : {len(SOURCES)} → 扫描到 {total} 条，新收录 {len(new_items)} 条")
        if failed:
            print("  跳过/失败:")
            for f in failed:
                print("     ⚠", f)
        print(f"  归档合计 : {len(merged)} 条（上限 {args.max}）")
        for it in new_items[:5]:
            print("   ·", it["title"][:48], "→", it["url"][:70])
        if not args.dry and new_items:
            save(merged, today)
            print("  已写入", ARCHIVE)
        elif not new_items:
            print("  无新增条目，未改动文件。")
        else:
            print("  (--dry) 未写入文件。")

    # ---------- 第二路：处罚案例 ----------
    if args.cases or not args.updates:
        print("=" * 60)
        print(f"[处罚案例] 抓取日期 : {today}")
        case_doc, case_items = load_cases()
        seen_cases = {it.get("url", "").split("?")[0] for it in case_items}
        fetched, failed_cases = fetch_cases()
        new_cases = []
        for c in fetched:
            if c["url"].split("?")[0] in seen_cases:
                continue
            seen_cases.add(c["url"].split("?")[0])
            new_cases.append(c)
        merged_cases = sorted(
            case_items + new_cases,
            key=lambda x: (x.get("date") or "0000-00-00", x.get("title") or ""),
            reverse=True)[: args.cases_max]
        merged_cases = merged_cases_dedup(merged_cases)
        print(f"  扫描来源 : {len(CASE_SOURCES)} → 扫描到 {len(fetched)} 条，新收录 {len(new_cases)} 条")
        if failed_cases:
            print("  跳过/失败:")
            for f in failed_cases:
                print("     ⚠", f)
        print(f"  案例合计 : {len(merged_cases)} 条（上限 {args.cases_max}）")
        kept_auto = sum(1 for c in merged_cases if c.get("auto"))
        kept_manual = len(merged_cases) - kept_auto
        print(f"  其中自动抓取 {kept_auto} 条 · 人工录入 {kept_manual} 条")
        for c in new_cases[:5]:
            print("   ·", c["title"][:48], f"({c.get('date')})", "→", c["url"][:70])
        if not args.dry and new_cases:
            save_cases(merged_cases, today)
            print("  已写入", CASE_ARCHIVE)
        elif not new_cases:
            print("  无新增案例，未改动文件。")
        else:
            print("  (--dry) 未写入文件。")

    print("=" * 60)

if __name__ == "__main__":
    main()