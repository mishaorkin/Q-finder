#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-наблюдатель для eveselibaspunkts.lv.

Проверяет страницу записи на доплерографию артерий и вен ног
(ServiceCode=180) и присылает уведомление в Telegram, когда появляются
"зелёные" места — визиты, оплачиваемые государством (valsts apmaksāts).

Запускается по расписанию через GitHub Actions (см. .github/workflows/check.yml).
Настройки берутся из переменных окружения:
  TELEGRAM_BOT_TOKEN — токен бота от @BotFather
  TELEGRAM_CHAT_ID   — ваш chat id (узнать у @userinfobot)
  SERVICE_CODE       — код услуги (по умолчанию 180)
"""

import asyncio
import json
import os
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

SERVICE_CODE = os.environ.get("SERVICE_CODE", "180")
URL = f"https://eveselibaspunkts.lv/lv/Booking/Institutions?ServiceCode={SERVICE_CODE}"
STATE_FILE = Path("state.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Тексты, по которым отсеиваем служебные элементы страницы (баннеры, меню и т.п.)
JUNK_MARKERS = [
    "sīkdat",          # cookie-баннер
    "javascript",
    "privāt",          # политика приватности
    "lietošanas notei",
    "medicloud",
    "facebook",
    "seko mums",
]

# Признак того, что мест нет
NO_SLOTS_MARKERS = ["nav brīvu", "nav pieejam"]


def tg_send(text: str) -> None:
    """Отправка сообщения в Telegram."""
    if not BOT_TOKEN or not CHAT_ID:
        print("[warn] Telegram не настроен, сообщение в консоль:")
        print(text)
        return
    data = urllib.parse.urlencode(
        {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# JavaScript, который выполняется внутри страницы: ищет все элементы
# зелёного цвета (фон, текст, рамка или класс green/success) и возвращает
# текст ближайшей информативной карточки вокруг каждого из них.
EXTRACT_JS = r"""
() => {
  const parseColor = (c) => {
    const m = (c || '').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?/);
    if (!m) return null;
    return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : parseFloat(m[4]) };
  };
  const isGreen = (c) => {
    const p = parseColor(c);
    return !!(p && p.a > 0.1 && p.g > 90 && p.g > p.r + 25 && p.g > p.b + 25);
  };
  const cards = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('body *')) {
    let cs;
    try { cs = getComputedStyle(el); } catch (e) { continue; }
    const green =
      isGreen(cs.backgroundColor) || isGreen(cs.color) ||
      isGreen(cs.borderTopColor) || isGreen(cs.fill);
    const cls = typeof el.className === 'string' ? el.className : '';
    const greenClass = /green|success/i.test(cls);
    if (!green && !greenClass) continue;
    // поднимаемся вверх до контейнера с осмысленным текстом (карточка учреждения)
    let node = el;
    for (let i = 0; i < 8 && node.parentElement; i++) {
      if ((node.innerText || '').trim().length > 60) break;
      node = node.parentElement;
    }
    const txt = (node.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 400);
    if (!txt || seen.has(txt)) continue;
    seen.add(txt);
    cards.push({ text: txt, cls: cls.slice(0, 120) });
  }
  return { cards, bodyText: (document.body.innerText || '').slice(0, 30000) };
}
"""


async def fetch_page():
    """Открывает страницу в браузере, возвращает (данные, xhr-ответы)."""
    responses = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(
            locale="lv-LV",
            viewport={"width": 1400, "height": 1000},
        )
        page.on("response", lambda r: responses.append(r))
        await page.goto(URL, wait_until="networkidle", timeout=90000)
        await page.wait_for_timeout(6000)

        # закрыть cookie-баннер, если он есть
        for label in ["Piekrītu", "Apstiprināt", "Atļaut", "Accept", "Piekrist"]:
            try:
                await page.get_by_role("button", name=label).first.click(timeout=1500)
                await page.wait_for_timeout(1000)
                break
            except Exception:
                pass

        # прокрутка, чтобы подгрузился весь список учреждений
        for _ in range(15):
            await page.mouse.wheel(0, 2500)
            await page.wait_for_timeout(500)
        await page.wait_for_timeout(3000)

        data = await page.evaluate(EXTRACT_JS)
        await page.screenshot(path="page.png", full_page=True)

        # сохраняем JSON-ответы сервера — пригодятся для точной настройки
        xhr = []
        for r in responses:
            try:
                ct = (r.headers or {}).get("content-type", "")
                if "json" in ct and "eveselibaspunkts" in r.url:
                    body = await r.text()
                    if len(body) < 2_000_000:
                        xhr.append({"url": r.url, "body": json.loads(body)})
            except Exception:
                pass

        await browser.close()
    return data, xhr


def is_junk(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in JUNK_MARKERS)


def in_quiet_hours() -> bool:
    """Тихие часы (по местному времени): QUIET_FROM..QUIET_TO — не проверяем."""
    try:
        q_from = int(os.environ.get("QUIET_FROM", "-1"))
        q_to = int(os.environ.get("QUIET_TO", "-1"))
    except ValueError:
        return False
    if q_from < 0 or q_to < 0:
        return False
    hour = datetime.now().hour
    if q_from > q_to:  # интервал через полночь, например 23..8
        return hour >= q_from or hour < q_to
    return q_from <= hour < q_to


def main() -> int:
    if in_quiet_hours():
        print("Тихие часы — проверка пропущена")
        return 0

    state = load_state()

    try:
        data, xhr = asyncio.run(fetch_page())
    except Exception:
        err = traceback.format_exc(limit=3)
        print(err)
        n = int(state.get("consecutive_errors", 0)) + 1
        state["consecutive_errors"] = n
        save_state(state)
        # шлём тревогу при первой ошибке и затем при каждой 30-й подряд
        # (при частых проверках это примерно раз в 5 часов), чтобы не заспамить
        if n == 1 or n % 30 == 0:
            tg_send(
                "⚠️ Бот-доплерография: не удалось проверить страницу "
                f"(ошибка №{n} подряд). Если это повторяется — проверьте "
                f"логи в GitHub Actions.\n{URL}"
            )
        return 0

    # отладочные файлы (сохраняются как артефакты GitHub Actions)
    Path("page_text.txt").write_text(data.get("bodyText", ""), encoding="utf-8")
    Path("cards.json").write_text(
        json.dumps(data.get("cards", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    Path("xhr.json").write_text(
        json.dumps(xhr, ensure_ascii=False, indent=2)[:5_000_000], encoding="utf-8"
    )

    body_text = data.get("bodyText", "")
    if len(body_text) < 300:
        raise SystemExit("Страница загрузилась пустой — смотрите артефакты")

    # кандидаты: зелёные карточки без служебного мусора и без «нет мест»
    greens = []
    for card in data.get("cards", []):
        text = card["text"]
        low = text.lower()
        if is_junk(text):
            continue
        if any(m in low for m in NO_SLOTS_MARKERS):
            continue
        greens.append(text)

    prev = set(state.get("greens", []))
    cur = set(greens)
    new_items = [t for t in greens if t not in prev]
    first_run = "greens" not in state

    if first_run:
        if greens:
            msg = (
                "✅ Бот запущен и работает. Уже сейчас на странице видны "
                "зелёные (гос.) места:\n\n"
                + "\n\n".join("🟢 " + t[:250] for t in greens[:8])
                + f"\n\nЗапись: {URL}"
            )
        else:
            msg = (
                "✅ Бот запущен и работает. Слежу за появлением "
                "оплачиваемых государством мест на доплерографию "
                "артерий и вен ног (5 проверок в день). Пока зелёных "
                "мест не видно — как появятся, сразу напишу."
            )
        tg_send(msg)
    elif new_items:
        msg = (
            "🟢 ПОЯВИЛИСЬ МЕСТА! Похоже, на доплерографию ног открылись "
            "оплачиваемые государством визиты:\n\n"
            + "\n\n".join("🟢 " + t[:250] for t in new_items[:8])
            + f"\n\nСкорее записывайтесь: {URL}"
        )
        tg_send(msg)
    else:
        print(f"Новых зелёных мест нет (всего зелёных карточек: {len(greens)})")

    # last_run намеренно не пишем в state.json, чтобы файл менялся
    # (и коммитился в GitHub-версии) только при реальных изменениях
    state.update(
        {
            "greens": sorted(cur),
            "consecutive_errors": 0,
        }
    )
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
