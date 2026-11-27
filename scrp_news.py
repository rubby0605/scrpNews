#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每天撈新聞、唸給你聽。

用關鍵字到 Bing／Google 新聞搜尋，把標題存成每天一份 CSV，
跑過的標題會記起來、下次不重複，最後可以用 gTTS 把今天的新標題唸成 mp3。

用法：
    python3 scrp_news.py 台積電 聯發科            # 直接給關鍵字
    python3 scrp_news.py                          # 沒給就讀 keywords.txt（一行一個）
    python3 scrp_news.py 台積電 --recent 24h      # 只要最近 24 小時（或 7d）
    python3 scrp_news.py --mp3                    # 抓完把新標題唸成 mp3
    python3 scrp_news.py --workers 4 --out data   # 同時 4 條線抓、輸出到 data/

輸出（預設在 ./data）：
    news_YYYYMMDD.csv    當天抓到的新標題（date, keyword, source, title, url, age）
    seen_news.json       看過的標題（用來去重）
    news_YYYYMMDD.mp3    加了 --mp3 才有
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.6',
}
MAX_PER_KEYWORD = 10


def get_page(url):
    """抓一頁，回傳 BeautifulSoup；失敗回傳 None。"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.encoding = 'utf-8'
        return BeautifulSoup(r.text, 'html.parser')
    except requests.exceptions.RequestException:
        return None


def clean_text(text):
    if not text:
        return ''
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s一-鿿，。！？、：；「」『』（）]', '', text)
    return text.strip()


# ---------- 去重 ----------

def news_hash(title):
    return hashlib.md5(re.sub(r'\s+', '', str(title).lower()).encode('utf-8')).hexdigest()[:16]


def load_seen(path):
    try:
        with open(path, encoding='utf-8') as f:
            return set(json.load(f).get('hashes', []))
    except Exception:
        return set()


def save_seen(path, seen):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'hashes': sorted(seen), 'count': len(seen),
                   'last_updated': datetime.datetime.now().isoformat(timespec='seconds')}, f, ensure_ascii=False)


# ---------- 搜尋 ----------

def bing_news(keyword, recent=None):
    """Bing 新聞。recent='24h' 或 '7d' 只取近期（不限的話 Bing 會混進好幾年前的舊聞）。"""
    url = 'https://www.bing.com/news/search?q=%22' + quote(keyword) + '%22&qs=n&form=QBNT&sp=-1'
    if recent == '24h':
        url += '&qft=interval%3d%227%22'
    elif recent == '7d':
        url += '&qft=interval%3d%228%22'
    bs = get_page(url)
    if bs is None:
        return []
    out = []
    cards = (bs.find_all('div', class_='news-card') or bs.find_all('div', class_='newsitem')
             or bs.find_all('a', class_='title'))
    for card in cards[:MAX_PER_KEYWORD]:
        a = card.find('a', class_='title') or card.find('a') or (card if card.name == 'a' else None)
        if not a:
            continue
        title = clean_text(a.get_text())
        if len(title) <= 10:
            continue
        item = {'keyword': keyword, 'source': 'Bing', 'title': title, 'url': a.get('href', ''), 'age': ''}
        sp = card.find('span', attrs={'aria-label': True}) if hasattr(card, 'find') else None
        age = clean_text(sp.get('aria-label', '')) if sp else ''
        if age and ('前' in age or '/' in age or 'ago' in age):
            item['age'] = age          # 發布時間，例如「4 小時前」
        out.append(item)
    if not out:                        # 版面改掉時的備援：掃所有連結，標題要含關鍵字
        seen_titles = set()
        for a in bs.find_all('a', href=True):
            text, href = clean_text(a.get_text()), a['href']
            if (len(text) > 15 and keyword in text and href.startswith('http')
                    and 'bing.com' not in href and text not in seen_titles):
                seen_titles.add(text)
                out.append({'keyword': keyword, 'source': 'Bing', 'title': text[:200], 'url': href, 'age': ''})
                if len(out) >= MAX_PER_KEYWORD:
                    break
    return out


def google_news(keyword, recent=None):
    """Google 新聞搜尋（常被擋，當備援用）。"""
    url = 'https://www.google.com/search?q=' + quote(keyword) + '&tbm=nws'
    if recent == '24h':
        url += '&tbs=qdr:d'
    elif recent == '7d':
        url += '&tbs=qdr:w'
    bs = get_page(url)
    if bs is None:
        return []
    out, seen_titles = [], set()
    for a in bs.find_all('a', href=True):
        text, href = clean_text(a.get_text()), a['href']
        if href.startswith('/url?q='):
            href = href[7:].split('&')[0]
        if (len(text) > 15 and keyword in text and href.startswith('http')
                and 'google.' not in href and text not in seen_titles):
            seen_titles.add(text)
            out.append({'keyword': keyword, 'source': 'Google', 'title': text[:200], 'url': href, 'age': ''})
            if len(out) >= MAX_PER_KEYWORD:
                break
    return out


def collect_keyword(keyword, recent=None, use_google=False):
    """一個關鍵字：Bing 為主；Bing 沒東西（或指定 --google）再試 Google。"""
    news = bing_news(keyword, recent)
    if use_google or not news:
        time.sleep(random.uniform(1, 2))
        have = {n['title'] for n in news}
        news += [n for n in google_news(keyword, recent) if n['title'] not in have]
    time.sleep(random.uniform(2, 4))   # 每條線各自放慢，不要打太快
    return keyword, news


def collect_all(keywords, seen, recent=None, workers=4, use_google=False):
    """多個關鍵字平行抓，回傳沒看過的新聞（並把它們記進 seen）。"""
    fresh = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(collect_keyword, k, recent, use_google) for k in keywords]
        for fut in as_completed(futs):
            try:
                keyword, news = fut.result()
            except Exception as e:
                print(f'抓取失敗: {e}')
                continue
            new = []
            for n in news:
                h = news_hash(n['title'])
                if h not in seen:
                    seen.add(h)
                    new.append(n)
            print(f'{keyword}: 抓到 {len(news)} 則，其中新的 {len(new)} 則')
            fresh += new
    return fresh


# ---------- 輸出 ----------

def save_csv(news, out_dir, today):
    path = os.path.join(out_dir, f'news_{today:%Y%m%d}.csv')
    is_new = not os.path.exists(path)
    with open(path, 'a', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(['date', 'keyword', 'source', 'title', 'url', 'age'])
        for n in news:
            w.writerow([f'{today:%Y%m%d}', n['keyword'], n['source'], n['title'], n['url'], n.get('age', '')])
    return path


def save_mp3(news, out_dir, today, lang='zh-TW'):
    """把今天的新標題唸成 mp3（依關鍵字分段）。"""
    from gtts import gTTS
    by_kw = {}
    for n in news:
        by_kw.setdefault(n['keyword'], []).append(n['title'])
    parts = [f'{today.month}月{today.day}日新聞。']
    for kw, titles in by_kw.items():
        parts.append(f'{kw}，共{len(titles)}則。' + '。'.join(titles) + '。')
    path = os.path.join(out_dir, f'news_{today:%Y%m%d}.mp3')
    gTTS(text=''.join(parts), lang=lang, slow=False).save(path)
    return path


def read_keywords(path='keywords.txt'):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [x.strip() for x in f if x.strip() and not x.startswith('#')]


def main():
    ap = argparse.ArgumentParser(description='每天撈新聞、唸給你聽')
    ap.add_argument('keywords', nargs='*', help='關鍵字（沒給就讀 keywords.txt）')
    ap.add_argument('--recent', choices=['24h', '7d'], help='只取最近 24 小時或 7 天')
    ap.add_argument('--workers', type=int, default=4, help='同時幾條線抓（預設 4）')
    ap.add_argument('--google', action='store_true', help='Bing 之外也查 Google')
    ap.add_argument('--mp3', action='store_true', help='把新標題唸成 mp3（需要 gTTS）')
    ap.add_argument('--out', default='data', help='輸出資料夾（預設 ./data）')
    a = ap.parse_args()

    keywords = a.keywords or read_keywords()
    if not keywords:
        ap.error('請給關鍵字，或在 keywords.txt 一行寫一個')
    os.makedirs(a.out, exist_ok=True)
    today = datetime.date.today()
    seen_path = os.path.join(a.out, 'seen_news.json')
    seen = load_seen(seen_path)

    news = collect_all(keywords, seen, a.recent, a.workers, a.google)
    save_seen(seen_path, seen)
    if not news:
        print('沒有新的新聞。')
        return
    print(f'共 {len(news)} 則新的 → {save_csv(news, a.out, today)}')
    if a.mp3:
        print(f'語音 → {save_mp3(news, a.out, today)}')


if __name__ == '__main__':
    main()
