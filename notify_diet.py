import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# 2026-08: 사이트 개편으로 옛 경로(listByWeek.do)가 404. 새 경로를 우선 시도하고,
# 실패하면(네트워크 오류/404/테이블 구조 변경) 옛 경로로 폴백한다.
DIET_URL = "https://www.uc.ac.kr/kr/CMS/DietMenuMgr/list.do"
CAMPUS_PARAMS = {"mCode": "MN187", "searchDietCategory": "4"}  # 동부식당

LEGACY_DIET_URL = "https://www.uc.ac.kr/www/CMS/DietMenuMgr/listByWeek.do"
LEGACY_CAMPUS_PARAMS = {"mCode": "MN207", "searchDietCategory": "4"}  # 동부식당

DIET_SOURCES = [(DIET_URL, CAMPUS_PARAMS), (LEGACY_DIET_URL, LEGACY_CAMPUS_PARAMS)]

KST = timezone(timedelta(hours=9))


class DateNotListed(Exception):
    """Today's date isn't in the site's currently displayed week yet.

    Seen in practice right after the week rolls over (e.g. Monday morning):
    the site can lag before it starts showing the new week's table, so this
    is treated as retryable rather than assumed to mean "no menu today"."""


def _fetch_table(url, params):
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return soup.select_one("#cafeteria-menu table.tbl-type01") or soup.select_one("#cafeteria-menu table.tbl")


def _extract_header_date(th):
    """New site splits the date across .mon ("2026.08") and .date ("31") spans
    instead of one full "2026-08-31" string, so reassemble it when needed."""
    date_el = th.select_one(".date")
    date_text = date_el.get_text(strip=True) if date_el else th.get_text(strip=True)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        return date_text
    mon_el = th.select_one(".mon")
    if mon_el:
        year, month = mon_el.get_text(strip=True).split(".")
        return f"{year}-{month}-{date_text.zfill(2)}"
    return date_text


def fetch_today_menu():
    today = datetime.now(KST).date()

    table = None
    last_error = None
    for url, params in DIET_SOURCES:
        try:
            table = _fetch_table(url, params)
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"{url} 요청 실패, 다음 URL 시도: {e}")
            continue
        if table is None:
            last_error = RuntimeError(f"{url}: 식단표 테이블을 찾을 수 없습니다 (사이트 구조가 변경되었을 수 있습니다)")
            print(f"{last_error}, 다음 URL 시도")
            continue
        break
    if table is None:
        raise last_error

    header_cells = table.select("thead th")[1:]  # skip "구분" column
    dates = [_extract_header_date(th) for th in header_cells]

    if str(today) not in dates:
        raise DateNotListed(f"{today}가 이번주 식단표({dates})에 없습니다")

    col_index = dates.index(str(today))

    lunch_row = None
    for row in table.select("tbody tr"):
        header = row.select_one("th")
        if header and header.get_text(strip=True) in ("점심", "중식"):
            lunch_row = row
            break
    if lunch_row is None:
        raise RuntimeError("점심 메뉴 행을 찾을 수 없습니다")

    cells = lunch_row.select("td")
    cell = cells[col_index]
    for br in cell.find_all("br"):
        br.replace_with("\n")
    menu_text = cell.get_text().replace("\r", "").strip()
    return menu_text, today


FOOD_COURT_MARKER = "<푸드코트>"


def build_slack_message(menu_text, today):
    """Returns None when today's row is genuinely empty/holiday (site has the
    date listed but no menu) - caller should skip sending in that case."""
    if not menu_text.strip() or "공휴일" in menu_text:
        return None

    main_menu_text = menu_text.split(FOOD_COURT_MARKER)[0]

    day_kr = ["월", "화", "수", "목", "금", "토", "일"][today.weekday()]
    date_str = f"{today.strftime('%Y-%m-%d')} ({day_kr})"
    items = "\n".join(f"- {line}" for line in main_menu_text.split("\n") if line.strip())
    return f"*{date_str} 오늘의 식단 (동부식당 점심)*\n{items}"


def _post_to_slack_once(text, webhook_url):
    resp = requests.post(webhook_url, json={"text": text}, timeout=30)
    resp.raise_for_status()


SLACK_QUICK_ATTEMPTS = 3
SLACK_QUICK_BACKOFF_SECONDS = 20
SLACK_EXTENDED_ROUNDS = 2  # 1 initial quick round + 1 extended-wait round
SLACK_EXTENDED_WAIT_SECONDS = 1800  # 30 min


def send_to_slack(text, webhook_url):
    last_error = None
    for round_num in range(1, SLACK_EXTENDED_ROUNDS + 1):
        for attempt in range(1, SLACK_QUICK_ATTEMPTS + 1):
            try:
                _post_to_slack_once(text, webhook_url)
                return
            except requests.exceptions.RequestException as e:
                last_error = e
                print(f"Slack 전송 시도 {attempt}/{SLACK_QUICK_ATTEMPTS} (라운드 {round_num}) 실패: {e}")
                if attempt < SLACK_QUICK_ATTEMPTS:
                    time.sleep(SLACK_QUICK_BACKOFF_SECONDS)
        if round_num < SLACK_EXTENDED_ROUNDS:
            print(f"{SLACK_EXTENDED_WAIT_SECONDS}초 대기 후 재시도합니다")
            time.sleep(SLACK_EXTENDED_WAIT_SECONDS)
    raise last_error


MAX_ATTEMPTS = 4
RETRY_WAIT_SECONDS = 300  # 5 min - gives the site time to roll over to the new week


def main():
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("SLACK_WEBHOOK_URL 환경변수가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            menu_text, today = fetch_today_menu()
            break
        except DateNotListed as e:
            print(f"시도 {attempt}/{MAX_ATTEMPTS} 실패: {e}")
            if attempt == MAX_ATTEMPTS:
                send_to_slack(
                    f"[식단봇] 오늘 날짜가 식단표 사이트에 아직 반영되지 않았습니다 ({e}). "
                    "사이트를 직접 확인해주세요.",
                    webhook_url,
                )
                return
            time.sleep(RETRY_WAIT_SECONDS)

    message = build_slack_message(menu_text, today)
    if message is None:
        print(f"{today}: 공휴일/휴무로 식단 없음 - 알림 생략")
        return
    print(message)
    send_to_slack(message, webhook_url)


if __name__ == "__main__":
    main()
