#!/usr/bin/env python3
"""更新情報ページから Atom / RSS を生成する。

流れ: Playwright でページを描画 -> 記事リンクを抽出 -> state.json と突き合わせ
      -> public/ に Atom・RSS・index.html を出力

使い方:
    python build_feeds.py                       # 通常実行
    python build_feeds.py --from-html page.html # 保存済みHTMLで試す(ブラウザ不要)
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree as ET

import yaml
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
DATE_RE = re.compile(r"((?:19|20)\d{2})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*日?")
STATE_LIMIT = 500  # state.json に残す最大件数(サイトごと)
ATOM_NS = "http://www.w3.org/2005/Atom"
XML_NS = "http://www.w3.org/XML/1998/namespace"


# ---------------------------------------------------------------- 取得

def _save_debug(page, debug_dir, sid):
    """失敗の調査用に、その時点のHTMLとスクリーンショットを保存する(失敗しても無視)。"""
    try:
        (debug_dir / f"{sid}.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[{sid}] デバッグHTMLを保存できませんでした: {exc}", file=sys.stderr)
    try:
        page.screenshot(path=str(debug_dir / f"{sid}.png"), full_page=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[{sid}] スクリーンショットを保存できませんでした: {exc}", file=sys.stderr)


def fetch_rendered(site, debug_dir, is_ready):
    """JavaScript を実行したあとの HTML を返す。

    - 読み込みは「DOMの構築完了」までで先に進める(計測スクリプト等で
      ネットワークが静かにならないサイトでもタイムアウトしないように)。
    - 記事が出るまで最大約20秒、2秒おきに確認する(is_ready で判定)。
    - 成功・失敗にかかわらず、その時点のHTMLとスクリーンショットを debug_dir に保存する。
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    sid, url = site["id"], site["url"]
    repo = os.environ.get("GITHUB_REPOSITORY", "local")
    ua = f"Mozilla/5.0 (compatible; FeedBuilderBot/1.0; +https://github.com/{repo})"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context(
                user_agent=ua, locale="ja-JP", viewport={"width": 1280, "height": 900}
            )
            page = ctx.new_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                print(f"[{sid}] HTTP {resp.status if resp else '?'} -> {page.url}")
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except PlaywrightTimeoutError:
                    print(f"[{sid}] ネットワークが静かにならなかったため、そのまま続行します")
                if site.get("wait_for"):
                    page.wait_for_selector(site["wait_for"], timeout=30_000)
                else:
                    for _ in range(10):
                        if is_ready(page.content()):
                            break
                        page.wait_for_timeout(2_000)
                html = page.content()
                print(f"[{sid}] 取得したHTML: {len(html)} 文字")
                return html
            finally:
                _save_debug(page, debug_dir, sid)
        finally:
            browser.close()


# ---------------------------------------------------------------- 抽出

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_date(text):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=JST)
    except ValueError:
        return None


def strip_date(text):
    return clean(DATE_RE.sub(" ", text))


def normalize_url(url):
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", p.query, ""))  # フラグメント除去


def find_date_near(tag, max_up=3, max_len=400):
    """リンクの周辺(親要素を数段まで)から日付を探す。"""
    node = tag
    for _ in range(max_up + 1):
        if node is None:
            break
        text = node.get_text(" ")
        if len(text) > max_len:
            break
        d = parse_date(text)
        if d:
            return d
        node = node.parent
    return None


def link_title(a):
    title = strip_date(a.get_text(" "))
    if not title:
        title = clean(a.get("aria-label", ""))
    if not title:
        img = a.find("img", alt=True)
        title = clean(img["alt"]) if img else ""
    return title


def extract_items(html, site):
    """HTML から [{url, title, date}] を返す(ページ上の並び順)。"""
    soup = BeautifulSoup(html, "html.parser")
    base = site["url"]
    base_host = urlparse(base).netloc
    sel = site.get("selectors") or {}
    found = {}  # url -> item(重複URLは1件にまとめる)

    def add(url, title, date):
        cur = found.get(url)
        if cur is None:
            found[url] = {"url": url, "title": title, "date": date}
        else:
            if len(title) > len(cur["title"]):
                cur["title"] = title
            cur["date"] = cur["date"] or date

    if sel.get("item"):  # 手動指定モード
        for el in soup.select(sel["item"]):
            a = el if el.name == "a" else el.select_one(sel.get("link", "a[href]"))
            if not a or not a.get("href"):
                continue
            url = normalize_url(urljoin(base, a["href"]))
            t_el = el.select_one(sel["title"]) if sel.get("title") else a
            title = strip_date(t_el.get_text(" ")) if t_el else ""
            d_el = el.select_one(sel["date"]) if sel.get("date") else el
            date = parse_date(d_el.get_text(" ")) if d_el else None
            add(url, title or link_title(a) or url, date)
    else:  # 自動検出モード
        prefixes = site.get("link_prefixes") or [urlparse(base).path]
        index_paths = {p.rstrip("/") for p in prefixes} | {urlparse(base).path.rstrip("/")}
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            url = normalize_url(urljoin(base, href))
            p = urlparse(url)
            if p.netloc != base_host:
                continue
            if not any(p.path.startswith(pre) for pre in prefixes):
                continue
            if p.path.rstrip("/") in index_paths and (not p.query or "page=" in p.query):
                continue  # 一覧ページ自身・ページ送り
            add(url, link_title(a) or "", find_date_near(a))

    items = []
    for it in found.values():
        if not it["title"]:
            it["title"] = it["url"].rstrip("/").rsplit("/", 1)[-1]
        items.append(it)
    return items


# ---------------------------------------------------------------- 状態

def load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def sort_key(rec):
    return rec["date"] or rec["first_seen"]


def merge_state(site_state, items, now):
    """スクレイプ結果を状態に反映する。最近のページ順を保つため first_seen を少しずつずらす。"""
    for i, it in enumerate(items):
        rec = site_state.get(it["url"])
        date_iso = it["date"].isoformat() if it["date"] else None
        if rec is None:
            site_state[it["url"]] = {
                "title": it["title"],
                "date": date_iso,
                "first_seen": (now - timedelta(seconds=i)).isoformat(),
            }
        else:
            rec["title"] = it["title"] or rec["title"]
            rec["date"] = date_iso or rec.get("date")
    if len(site_state) > STATE_LIMIT:
        keep = sorted(site_state.items(), key=lambda kv: sort_key(to_rec(kv[1])), reverse=True)
        site_state.clear()
        site_state.update(dict(keep[:STATE_LIMIT]))


def to_rec(raw):
    return {
        "date": datetime.fromisoformat(raw["date"]) if raw.get("date") else None,
        "first_seen": datetime.fromisoformat(raw["first_seen"]),
    }


def entries_from_state(site_state, limit):
    entries = []
    for url, raw in site_state.items():
        r = to_rec(raw)
        entries.append({"url": url, "title": raw["title"], "date": r["date"], "first_seen": r["first_seen"]})
    entries.sort(key=sort_key, reverse=True)
    return entries[:limit]


# ---------------------------------------------------------------- フィード出力

def _write_xml(root, path):
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def build_atom(site, entries, self_url, now, path):
    ET.register_namespace("", ATOM_NS)
    q = lambda n: f"{{{ATOM_NS}}}{n}"
    feed = ET.Element(q("feed"), {f"{{{XML_NS}}}lang": "ja"})
    ET.SubElement(feed, q("title")).text = site["title"]
    ET.SubElement(feed, q("link"), {"rel": "alternate", "type": "text/html", "href": site["url"]})
    ET.SubElement(feed, q("link"), {"rel": "self", "type": "application/atom+xml", "href": self_url})
    ET.SubElement(feed, q("id")).text = self_url
    newest = max((sort_key(e) for e in entries), default=now)
    ET.SubElement(feed, q("updated")).text = newest.isoformat()
    ET.SubElement(ET.SubElement(feed, q("author")), q("name")).text = site["title"]
    for e in entries:
        ent = ET.SubElement(feed, q("entry"))
        ET.SubElement(ent, q("title")).text = e["title"]
        ET.SubElement(ent, q("link"), {"href": e["url"]})
        ET.SubElement(ent, q("id")).text = e["url"]
        ET.SubElement(ent, q("updated")).text = sort_key(e).isoformat()
        if e["date"]:
            ET.SubElement(ent, q("published")).text = e["date"].isoformat()
    _write_xml(feed, path)


def build_rss(site, entries, self_url, now, path):
    ET.register_namespace("atom", ATOM_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = site["title"]
    ET.SubElement(ch, "link").text = site["url"]
    ET.SubElement(ch, "description").text = f"{site['title']}(自動生成フィード)"
    ET.SubElement(ch, "language").text = "ja"
    newest = max((sort_key(e) for e in entries), default=now)
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(newest)
    ET.SubElement(ch, f"{{{ATOM_NS}}}link", {"rel": "self", "type": "application/rss+xml", "href": self_url})
    for e in entries:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = e["title"]
        ET.SubElement(item, "link").text = e["url"]
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = e["url"]
        ET.SubElement(item, "pubDate").text = format_datetime(sort_key(e))
    _write_xml(rss, path)


def write_index(out_dir, sites_written):
    rows = "\n".join(
        f'    <li>{s["title"]}: <a href="{s["id"]}.atom.xml">Atom</a> / '
        f'<a href="{s["id"]}.rss.xml">RSS</a></li>'
        for s in sites_written
    )
    (out_dir / "index.html").write_text(
        '<!doctype html>\n<html lang="ja"><meta charset="utf-8">\n'
        "<title>Feeds</title>\n<h1>Feeds</h1>\n<ul>\n" + rows + "\n</ul>\n</html>\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- メイン

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="sites.yml")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--out", default="public")
    ap.add_argument("--debug-dir", default="debug")
    ap.add_argument("--from-html", help="取得の代わりに保存済みHTMLを使う(動作確認用)")
    args = ap.parse_args()

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    out_dir, debug_dir = Path(args.out), Path(args.debug_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)
    base_url = os.environ.get("FEED_BASE_URL", config.get("base_url", "./"))
    if not base_url.endswith("/"):
        base_url += "/"

    state = load_state(args.state)
    now = datetime.now(JST).replace(microsecond=0)
    written, failures = [], []

    for site in config["sites"]:
        sid = site["id"]
        site_state = state.setdefault(sid, {})
        try:
            if args.from_html:
                html = Path(args.from_html).read_text(encoding="utf-8")
            else:
                html = fetch_rendered(site, debug_dir, lambda h: bool(extract_items(h, site)))
            (debug_dir / f"{sid}.html").write_text(html, encoding="utf-8")
            items = extract_items(html, site)
            if not items:
                # ログだけで原因を推測できるよう、ページ上のリンクの様子を出力する
                hrefs = [a["href"] for a in BeautifulSoup(html, "html.parser").find_all("a", href=True)]
                print(f"[{sid}] ページ内のリンク数: {len(hrefs)}", file=sys.stderr)
                for h in hrefs[:30]:
                    print(f"[{sid}]   {h}", file=sys.stderr)
                raise RuntimeError("記事リンクを1件も抽出できませんでした(構造変更またはセレクタ不一致)")
            merge_state(site_state, items, now)
            print(f"[{sid}] {len(items)} 件を抽出")
        except Exception as exc:  # noqa: BLE001
            failures.append(sid)
            print(f"::error::[{sid}] 取得に失敗: {exc}", file=sys.stderr)

        entries = entries_from_state(site_state, site.get("max_items", 30))
        if not entries:
            continue  # 過去データもなければフィードは作らない
        build_atom(site, entries, f"{base_url}{sid}.atom.xml", now, out_dir / f"{sid}.atom.xml")
        build_rss(site, entries, f"{base_url}{sid}.rss.xml", now, out_dir / f"{sid}.rss.xml")
        written.append(site)

    Path(args.state).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_index(out_dir, written)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
