# scrpNews

用 Python 每天撈新聞，唸給你聽。

給幾個關鍵字，程式會到 Bing 新聞搜尋（需要的話再加 Google），把標題存成每天一份 CSV，
看過的標題下次不會重複，最後可以用 gTTS 把今天的新標題唸成 mp3。

## 安裝

```bash
pip install -r requirements.txt
```

## 用法

```bash
python3 scrp_news.py 台積電 聯發科            # 直接給關鍵字
python3 scrp_news.py                          # 沒給就讀 keywords.txt（一行一個）
python3 scrp_news.py 台積電 --recent 24h      # 只要最近 24 小時（或 7d）
python3 scrp_news.py --mp3                    # 抓完把新標題唸成 mp3
python3 scrp_news.py --workers 4 --out data   # 同時 4 條線抓、輸出到 data/
python3 scrp_news.py 台積電 --google          # Bing 之外也查 Google
```

想每天自動跑，加一行 cron 就好（例如每天早上六點）：

```
0 6 * * * cd /path/to/scrpNews && python3 scrp_news.py --recent 24h --mp3
```

## 輸出

| 檔案 | 內容 |
|---|---|
| `data/news_YYYYMMDD.csv` | 當天抓到的新標題：`date, keyword, source, title, url, age` |
| `data/seen_news.json` | 看過的標題（去重用） |
| `data/news_YYYYMMDD.mp3` | 加了 `--mp3` 才有 |

## 特色

- 只取近期新聞（`--recent 24h` / `7d`）。不限的話 Bing 會混進好幾年前的舊聞。
- 去重：標題算 hash 記在 `seen_news.json`，同一則不會抓第二次。
- 多個關鍵字平行抓（`--workers`），每條線各自放慢，不會打太快。
- Bing 版面改掉時有備援的解析方式；Bing 沒結果會自動改查 Google。
- 每則附發布時間（例如「4 小時前」）。
- mp3 依關鍵字分段唸。
