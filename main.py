"""
북마크 자동 정리 스크립트

동작:
1. Notion 데이터베이스에서 "한줄요약"과 "상태"가 비어 있는 줄(= 아직 처리 전)을 찾는다.
2. URL에서 내용을 가져온다. (유튜브: 자막 / 웹페이지: 본문 / 인스타 등: 캡션)
3. Gemini로 "한줄요약 + 내용 정리"를 만든다.
4. 내용 정리는 페이지 본문에, 한줄요약/종류/상태는 속성 칸에 채운다.

환경변수:
    NOTION_TOKEN        (필수) Notion 통합 시크릿
    NOTION_DATABASE_ID  (필수) 북마크 데이터베이스 ID
    GEMINI_API_KEY      (필수) Google AI Studio 키
    GEMINI_MODEL        (선택) 모델 이름. 비우면 DEFAULT_MODEL 사용
    MAX_ITEMS           (선택) 한 번 실행에 처리할 최대 개수 (기본 8)
"""

import os
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

import requests
import trafilatura
from google import genai
from google.genai import types
from youtube_transcript_api import YouTubeTranscriptApi

# ---------------------------------------------------------------- 설정
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

DEFAULT_MODEL = "gemini-flash-latest"
MODEL = os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
MAX_ITEMS = int(os.environ.get("MAX_ITEMS") or 8)
SLEEP_BETWEEN = 6  # 초. 무료 티어의 분당 요청 제한을 넘지 않기 위한 간격
MAX_SOURCE_CHARS = 400_000  # 아주 긴 자막/본문은 여기서 자른다

# Notion 속성 이름 (표의 컬럼 이름과 정확히 같아야 함)
P_TITLE = "제목"
P_URL = "URL"
P_KIND = "종류"
P_SUMMARY = "한줄요약"
P_STATUS = "상태"

STATUS_DONE = "안 봄"

NOTION_VERSION = "2022-06-28"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ko,en;q=0.8",
}
URL_RE = re.compile(r"https?://[^\s<>\"]+")

SYSTEM_PROMPT = """너는 저장해 둔 영상/글을 "원본을 다시 보지 않아도 내용을 이해할 수 있게" 정리하는 도우미다.
반드시 한국어로 쓴다. 원문이 영어 등 다른 언어여도 한국어로 정리한다.

출력 형식은 정확히 아래를 따른다.
- 첫 줄: 한줄요약. 이 자료가 무엇에 대한 내용인지 보고 지울지 판단할 수 있게 한 문장(60자 안팎)으로 쓴다. "한줄요약:" 같은 머리말은 붙이지 않는다.
- 빈 줄 한 줄.
- 그다음부터: 내용 정리.
  - 원문이 전개되는 순서대로 정리한다.
  - 소제목은 "## 소제목" 형식, 핵심 항목은 "- 항목" 형식, 나머지는 평범한 문장으로 쓴다.
  - 핵심 주장, 근거, 수치, 고유명사(사람/제품/장소), 결론, 실행 방법이 있으면 빠뜨리지 않는다.
  - 분량은 원문 길이에 비례한다. 짧은 자료는 짧게, 긴 강연이나 긴 글은 충분히 길게 쓴다.
  - 굵은 글씨(별표 **) 표시는 쓰지 않는다. 이모지도 쓰지 않는다.
  - 원문에 없는 내용을 지어내지 않는다. 불확실하거나 알아듣기 어려운 부분은 그렇다고 적는다.
  - 인사말, 서론, 맺음말 같은 군더더기 없이 정리 내용만 출력한다."""

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# ---------------------------------------------------------------- Notion
def notion(method, path, **kwargs):
    """Notion API 호출. 429(요청 과다)면 기다렸다가 재시도한다."""
    url = f"https://api.notion.com/v1{path}"
    for attempt in range(4):
        r = requests.request(method, url, headers=NOTION_HEADERS, timeout=60, **kwargs)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1)
            continue
        if not r.ok:
            raise RuntimeError(f"Notion {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError(f"Notion {method} {path}: 재시도 초과")


def query_pending():
    """한줄요약과 상태가 모두 비어 있는 줄을 오래된 순으로 가져온다."""
    body = {
        "filter": {
            "and": [
                {"property": P_SUMMARY, "rich_text": {"is_empty": True}},
                {"property": P_STATUS, "select": {"is_empty": True}},
            ]
        },
        "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
        "page_size": max(1, min(MAX_ITEMS, 100)),
    }
    return notion("POST", f"/databases/{DATABASE_ID}/query", json=body)["results"]


def plain(rich_text_list):
    return "".join(t.get("plain_text", "") for t in rich_text_list or [])


def chunk_text(text, size=1900):
    """Notion은 텍스트 한 덩어리가 2000자를 넘으면 거부하므로 나눈다."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def text_to_blocks(text):
    """'## 소제목', '- 항목', 일반 문장을 Notion 블록으로 바꾼다."""
    blocks = []
    for raw in text.splitlines():
        line = raw.replace("**", "").strip()
        if not line:
            continue
        if line.startswith("#"):
            kind, content = "heading_3", line.lstrip("#").strip()
        elif line.startswith(("- ", "* ", "• ")):
            kind, content = "bulleted_list_item", line[2:].strip()
        else:
            kind, content = "paragraph", line
        if not content:
            continue
        for piece in chunk_text(content):
            blocks.append(
                {
                    "object": "block",
                    "type": kind,
                    kind: {"rich_text": [{"type": "text", "text": {"content": piece}}]},
                }
            )
    return blocks


def append_body(page_id, text):
    blocks = text_to_blocks(text)
    for i in range(0, len(blocks), 90):  # 한 번에 최대 100개
        notion("PATCH", f"/blocks/{page_id}/children", json={"children": blocks[i : i + 90]})


def update_page(page_id, *, title=None, url=None, kind=None, summary=None, status=None):
    props = {}
    if title is not None:
        props[P_TITLE] = {"title": [{"type": "text", "text": {"content": title[:1900]}}]}
    if url is not None:
        props[P_URL] = {"url": url}
    if kind is not None:
        props[P_KIND] = {"select": {"name": kind}}
    if summary is not None:
        props[P_SUMMARY] = {"rich_text": [{"type": "text", "text": {"content": summary[:1900]}}]}
    if status is not None:
        props[P_STATUS] = {"select": {"name": status}}
    notion("PATCH", f"/pages/{page_id}", json={"properties": props})


# ---------------------------------------------------------------- URL 판별
def kind_of(url):
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "유튜브"
    if "instagram.com" in host:
        return "인스타"
    return "웹페이지"


def youtube_id(url):
    u = urlparse(url)
    host = u.netloc.lower()
    if "youtu.be" in host:
        vid = u.path.strip("/").split("/")[0]
        return vid or None
    if "youtube.com" in host:
        if u.path == "/watch":
            return (parse_qs(u.query).get("v") or [None])[0]
        m = re.match(r"^/(shorts|live|embed|v)/([\w-]{6,})", u.path)
        if m:
            return m.group(2)
    return None


# ---------------------------------------------------------------- 내용 가져오기
def youtube_meta(url):
    """oEmbed로 제목과 채널명을 가져온다. 실패해도 무시."""
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            headers=BROWSER_HEADERS,
            timeout=20,
        )
        if r.ok:
            j = r.json()
            return j.get("title"), j.get("author_name")
    except Exception as e:  # noqa: BLE001
        print(f"  oEmbed 실패: {type(e).__name__}")
    return None, None


def youtube_transcript(video_id):
    """자막 텍스트를 반환. 못 가져오면 None."""
    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=["ko", "en"])
    except Exception as e1:  # noqa: BLE001
        print(f"  자막(ko/en) 실패: {type(e1).__name__}")
        try:  # 다른 언어라도 있으면 그걸 쓴다
            listing = api.list(video_id)
            first = next(iter(listing))
            fetched = first.fetch()
        except Exception as e2:  # noqa: BLE001
            print(f"  자막(기타 언어) 실패: {type(e2).__name__}")
            return None
    text = " ".join(s.text for s in fetched).strip()
    return text or None


def fetch_webpage(url):
    """(제목, 본문) 반환. 본문이 부족하면 (제목, None)."""
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    if r.encoding is None or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    html = r.text
    meta = trafilatura.extract_metadata(html)
    title = meta.title if meta and meta.title else None
    text = trafilatura.extract(html, include_comments=False, include_tables=True)
    if (not text or len(text) < 200) and meta and meta.description:
        # 인스타 등 본문이 없는 곳은 설명(캡션)이라도 사용
        text = meta.description
    if not text or len(text) < 30:
        return title, None
    return title, text


# ---------------------------------------------------------------- Gemini
def ask_gemini(parts):
    resp = client.models.generate_content(
        model=MODEL,
        contents=parts,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.3),
    )
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini가 빈 응답을 반환")
    return text


def ask_gemini_with_retry(parts):
    last = None
    for attempt in range(3):
        try:
            return ask_gemini(parts)
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  Gemini 호출 실패({attempt + 1}/3): {type(e).__name__}: {str(e)[:150]}")
            time.sleep(20 * (attempt + 1))
    raise last


def summarize_text(title, url, channel, text):
    header = f"제목: {title or '(없음)'}\nURL: {url}\n"
    if channel:
        header += f"채널: {channel}\n"
    body = f"{header}\n[원문]\n{text[:MAX_SOURCE_CHARS]}"
    return ask_gemini_with_retry(body)


def summarize_youtube_by_url(video_id):
    """자막을 못 가져왔을 때: Gemini에게 유튜브 URL을 직접 보여준다."""
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    parts = types.Content(
        parts=[
            types.Part(file_data=types.FileData(file_uri=watch_url)),
            types.Part(text="이 영상의 내용을 위 지침대로 정리해 줘."),
        ]
    )
    return ask_gemini_with_retry(parts)


def split_summary(text):
    lines = text.strip().splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    first = lines[i].strip() if i < len(lines) else ""
    body = "\n".join(lines[i + 1 :]).strip()
    first = re.sub(r"^(한줄요약|한 줄 요약)\s*[:：]\s*", "", first).replace("**", "").strip()
    return first, body


# ---------------------------------------------------------------- 한 줄 처리
def process(page):
    page_id = page["id"]
    props = page["properties"]
    title = plain((props.get(P_TITLE) or {}).get("title"))
    url = (props.get(P_URL) or {}).get("url")

    url_from_title = False
    if not url:  # 폰 공유 등으로 링크가 제목에 들어간 경우
        m = URL_RE.search(title)
        if m:
            url = m.group(0).rstrip(").,]")
            url_from_title = True
    if not url:
        print(f"- [{title[:30]}] URL을 찾지 못해 건너뜀")
        return

    kind = kind_of(url)
    print(f"- [{kind}] {url}")

    clean_title = URL_RE.sub("", title).strip() if url_from_title else title.strip()
    fetched_title, channel, source_text, fail_reason = None, None, None, None
    vid = youtube_id(url) if kind == "유튜브" else None

    if kind == "유튜브":
        if not vid:
            fail_reason = "유튜브 영상 주소를 해석하지 못함"
        else:
            fetched_title, channel = youtube_meta(url)
            source_text = youtube_transcript(vid)
    else:
        try:
            fetched_title, source_text = fetch_webpage(url)
        except Exception as e:  # noqa: BLE001
            fail_reason = f"페이지를 열지 못함({type(e).__name__})"
        else:
            if not source_text:
                fail_reason = "본문을 가져오지 못함(로그인 필요 등)"

    # 제목 결정: 기존 제목이 비었거나 링크뿐이면 가져온 제목을 쓴다
    new_title = None
    if not clean_title:
        new_title = fetched_title or url
    elif url_from_title:
        new_title = clean_title

    # 요약 생성
    try:
        if source_text:
            answer = summarize_text(clean_title or fetched_title, url, channel, source_text)
        elif vid:
            print("  자막 없음 -> Gemini가 영상 URL을 직접 보도록 시도")
            answer = summarize_youtube_by_url(vid)
        else:
            answer = None
    except Exception as e:  # noqa: BLE001
        # 일시적 오류일 수 있으니 표시하지 않고 다음 실행에서 다시 시도한다
        print(f"  요약 실패, 다음 실행에서 재시도: {type(e).__name__}: {str(e)[:150]}")
        return

    if answer:
        one_liner, body = split_summary(answer)
        if body:
            append_body(page_id, body)
        update_page(
            page_id,
            title=new_title,
            url=url if url_from_title else None,
            kind=kind,
            summary=one_liner or "정리 완료",
            status=STATUS_DONE,
        )
        print("  완료")
    else:
        reason = fail_reason or "내용을 가져오지 못함"
        update_page(
            page_id,
            title=new_title,
            url=url if url_from_title else None,
            kind=kind,
            summary=f"요약 불가: {reason}",
            status=STATUS_DONE,
        )
        print(f"  요약 불가로 표시: {reason}")


def main():
    missing = [n for n, v in [("NOTION_TOKEN", NOTION_TOKEN), ("NOTION_DATABASE_ID", DATABASE_ID), ("GEMINI_API_KEY", GEMINI_API_KEY)] if not v]
    if missing:
        print(f"환경변수가 없습니다: {', '.join(missing)}")
        sys.exit(1)

    pages = query_pending()
    print(f"처리 대기 {len(pages)}건 (모델: {MODEL})")
    for n, page in enumerate(pages):
        try:
            process(page)
        except Exception as e:  # noqa: BLE001
            print(f"  처리 중 오류: {type(e).__name__}: {str(e)[:200]}")
        if n < len(pages) - 1:
            time.sleep(SLEEP_BETWEEN)


if __name__ == "__main__":
    main()
