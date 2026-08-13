#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Q-finder — проверяющая часть (работает в GitHub Actions).

Каждый запуск:
  1. Забирает у Cloudflare Worker список активных отслеживаний
     (кто из пользователей какие услуги отслеживает).
  2. Если каталог услуг устарел (старше 7 дней) или пуст —
     заново собирает его со страниц /lv|/ru|/en/Booking/Services
     и отправляет Worker'у.
  3. Для каждой отслеживаемой услуги открывает страницу
     /lv/Booking/Institutions?ServiceCode=<код> в браузере и ищет
     зелёные (оплачиваемые государством) места.
  4. При появлении новых зелёных мест шлёт уведомление в Telegram
     каждому, кто отслеживает эту услугу, с кнопкой остановки.

Переменные окружения (секреты GitHub):
  TELEGRAM_BOT_TOKEN — токен бота
  WORKER_URL         — адрес Worker'а, например https://qfinder.xxx.workers.dev
  SYNC_SECRET        — общий пароль (тот же, что в секретах Worker'а)
"""

import asyncio
import time
import json
import os
import re
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

RIGA_TZ = ZoneInfo("Europe/Riga")


def riga_hour():
    return datetime.now(RIGA_TZ).hour


def in_quiet(hour, quiet):
    """quiet = [с, до] по времени Латвии, либо None (тихих часов нет)."""
    if not quiet:
        return False
    f, t = quiet
    if f == t:
        return False
    if f > t:  # интервал через полночь, например 23..8
        return hour >= f or hour < t
    return f <= hour < t

from playwright.async_api import async_playwright

# .strip() защищает от случайных пробелов/переносов строки,
# прилипших при копировании значений в секреты
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
WORKER_URL = os.environ.get("WORKER_URL", "").strip().rstrip("/")
SYNC_SECRET = os.environ.get("SYNC_SECRET", "").strip()

BASE = "https://eveselibaspunkts.lv"
STATE_FILE = Path("state.json")
CATALOG_MAX_AGE_DAYS = 7

# Одна и та же дата в одном и том же учреждении уведомляется
# не чаще, чем раз в NOTIFY_COOLDOWN_HOURS часов
NOTIFY_COOLDOWN_HOURS = 12


MSG = {
    "ru": {
        "found": "🟢 ПОЯВИЛИСЬ МЕСТА!\n\nУслуга: {svc}\n\n{items}\n\nСкорее записывайтесь:\n{url}",
        "stop_btn": "⏹ Остановить отслеживание этой услуги",
        "error": "⚠️ Q-finder: не получается проверить сайт (ошибка №{n} подряд). Загляните в GitHub Actions.",
    },
    "lv": {
        "found": "🟢 PARĀDĪJĀS VIETAS!\n\nPakalpojums: {svc}\n\n{items}\n\nPierakstieties:\n{url}",
        "stop_btn": "⏹ Apturēt šī pakalpojuma novērošanu",
        "error": "⚠️ Q-finder: neizdodas pārbaudīt vietni (kļūda Nr.{n} pēc kārtas).",
    },
    "en": {
        "found": "🟢 SLOTS APPEARED!\n\nService: {svc}\n\n{items}\n\nBook now:\n{url}",
        "stop_btn": "⏹ Stop watching this service",
        "error": "⚠️ Q-finder: can't check the site (error #{n} in a row).",
    },
}


def http_json(url, payload=None):
    """GET (payload=None) или POST JSON, ответ — распарсенный JSON."""
    headers = {
        # представляемся браузером: защита Cloudflare может резать
        # «голые» запросы python-urllib кодом 403
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) qfinder-bot",
        "Accept": "application/json",
    }
    if payload is None:
        req = urllib.request.Request(url, headers=headers)
    else:
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers
        )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # печатаем, КТО ответил ошибкой (без секретов в логе):
        # 'forbidden' = наш воркер не принял пароль,
        # HTML-страница = сработала защита Cloudflare
        body = b""
        try:
            body = e.read()[:300]
        except Exception:
            pass
        path = url.split("?")[0]
        print(f"[http] {e.code} от {path}; тело ответа: {body!r}")
        raise


def tg_send(chat_id, text, stop_code=None, lang="ru"):
    if not BOT_TOKEN:
        print("[warn] нет токена;", text[:100])
        return
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if stop_code:
        payload["reply_markup"] = json.dumps(
            {
                "inline_keyboard": [
                    [{"text": MSG[lang]["stop_btn"], "callback_data": f"stop:{stop_code}"}]
                ]
            }
        )
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except Exception as e:
        print(f"[warn] telegram: {e}")


def select_to_notify(svc_entry, greens, now_ts):
    """Решает, о каких зелёных местах уведомлять сейчас.

    Правило: пара «учреждение + дата» — не чаще раза в 12 часов.
    Если у учреждения появилась НОВАЯ дата — уведомляем сразу.
    Возвращает (список учреждений для уведомления, обновлённый svc_entry).
    """
    notified = dict(svc_entry.get("notified", {}))
    # чистим записи старше 30 дней, чтобы state.json не разрастался
    notified = {k: v for k, v in notified.items() if now_ts - v < 30 * 86400}

    to_notify = []
    for inst, info in greens.items():
        key = f"{inst}|{info['date']}"
        if now_ts - notified.get(key, 0) >= NOTIFY_COOLDOWN_HOURS * 3600:
            to_notify.append(inst)
            notified[key] = now_ts

    new_entry = {
        "insts": {i: greens[i]["date"] for i in greens},
        "notified": notified,
    }
    return to_notify, new_entry


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --- извлечение зелёных дат ---
# На странице учреждений у каждой клиники справа плашки-даты:
# синяя = платный визит, ЗЕЛЁНАЯ = визит, оплачиваемый государством.
# Ищем именно зелёные плашки с датой (например «17. aug.») и берём
# карточку клиники вокруг них. Рекламные баннеры отфильтровываем.

EXTRACT_JS = r"""
() => {
  const MONTH = /^(\d{1,2})\.?\s*(jan|feb|mar|apr|mai|jūn|jun|jūl|jul|aug|sep|okt|nov|dec)\.?$/i;
  const parseColor = (c) => {
    const m = (c || '').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?/);
    if (!m) return null;
    return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : parseFloat(m[4]) };
  };
  const isGreen = (c) => {
    const p = parseColor(c);
    return !!(p && p.a > 0.1 && p.g > 110 && p.g > p.r + 20 && p.g > p.b + 20);
  };
  const greens = [];
  const seen = new Set();
  // кандидаты: 1) плашки с классом govpaid (точный признак с сайта),
  //            2) любые маленькие зелёные плашки с датой
  const candidates = new Set(document.querySelectorAll('[class*="govpaid"]'));
  for (const el of document.querySelectorAll('body *')) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 12 || !MONTH.test(t)) continue;
    let cs;
    try { cs = getComputedStyle(el); } catch (e) { continue; }
    if (isGreen(cs.backgroundColor) || isGreen(cs.borderTopColor)) candidates.add(el);
  }
  for (const el of candidates) {
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 12) continue;

    // поднимаемся до карточки клиники (в ней есть ссылка-название)
    let node = el, card = null;
    for (let i = 0; i < 8 && node.parentElement; i++) {
      node = node.parentElement;
      const t = node.innerText || '';
      if (node.querySelector('a') && t.length > 25 && t.length < 600) { card = node; break; }
    }
    if (!card) continue;
    const cardText = (card.innerText || '').replace(/\s+/g, ' ');
    if (/atlaide|ar kodu|reklām|uzzināt vairāk/i.test(cardText)) continue; // реклама
    const link = card.querySelector('a');
    const inst = link ? (link.innerText || '').replace(/\s+/g, ' ').trim() : '';
    if (!inst || inst.length < 3) continue;
    if (seen.has(inst)) continue;
    seen.add(inst);
    let addr = '';
    const lines = (card.innerText || '').split('\n').map((s) => s.trim()).filter(Boolean);
    const idx = lines.findIndex((l) => l.startsWith(inst.slice(0, 12)));
    if (idx >= 0 && lines[idx + 1] && !MONTH.test(lines[idx + 1])) addr = lines[idx + 1];
    addr = addr.replace(/\d{1,2}\.\s*(jan|feb|mar|apr|mai|jūn|jun|jūl|jul|aug|sep|okt|nov|dec)\.?/gi, '').replace(/\s+/g, ' ').trim();
    greens.push({ inst, addr: addr.slice(0, 80), date: txt });
  }
  return { greens, bodyText: (document.body.innerText || '').slice(0, 30000) };
}
"""

COLLECT_SERVICES_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="ServiceCode="]')) {
    const m = (a.getAttribute('href') || '').match(/ServiceCode=(\d+)/);
    if (!m) continue;
    const code = m[1];
    const text = (a.innerText || '').replace(/\s+/g, ' ').trim();
    if (!text || text.length < 3) continue;
    const key = code + '|' + text;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ code, text });
  }
  return out;
}
"""


def parse_institutions(body):
    """Из JSON-ответа /Booking/ListInstitutions достаёт клиники
    с ближайшей датой гос. визита (GovernmentPaidTime)."""
    greens = {}
    for it in body.get("list") or []:
        name = (it.get("displayName") or "").strip()
        if not name:
            continue
        dates = [
            (s.get("date") or "")[:10]
            for s in it.get("nearestTimeSlots") or []
            if s.get("type") == "GovernmentPaidTime" and s.get("date")
        ]
        if dates:
            d = min(dates)
            try:
                d = datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")
            except ValueError:
                pass
            greens[name] = {"date": d, "addr": (it.get("address") or "").strip()}
    return greens


async def open_page(browser, url, responses=None):
    page = await browser.new_page(locale="lv-LV", viewport={"width": 1400, "height": 1000})
    if responses is not None:
        page.on("response", lambda r: responses.append(r))
    await page.goto(url, wait_until="networkidle", timeout=90000)
    await page.wait_for_timeout(4000)
    for label in ["Dodu atļauju", "Piekrītu", "Apstiprināt", "Atļaut", "Accept", "Piekrist"]:
        try:
            await page.get_by_role("button", name=label).first.click(timeout=1200)
            await page.wait_for_timeout(800)
            break
        except Exception:
            pass
    for _ in range(15):
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(400)
    await page.wait_for_timeout(2000)
    return page


async def load_full_services_list(page):
    """Нажимает «Ielādēt vēl / Загрузить ещё / Load more», пока список не кончится."""
    import re as _re
    pattern = _re.compile(r"Ielādēt vēl|Загрузить ещё|Загрузить еще|Load more", _re.I)
    for _ in range(60):
        try:
            btn = page.get_by_text(pattern).first
            await btn.click(timeout=1500)
            await page.wait_for_timeout(700)
        except Exception:
            break


def services_from_xhr(body):
    """Пробует достать услуги (код + название) из любого JSON-ответа сайта."""
    out = []
    items = body.get("list") if isinstance(body, dict) else body
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        # клиники отсекаем по их характерным полям
        if "institutionCode" in it or "nearestTimeSlots" in it or it.get("address"):
            continue
        code = it.get("serviceCode") or it.get("code") or it.get("sourceIdentifier")
        name = it.get("displayName") or it.get("name") or it.get("title")
        if code and name:
            out.append({"code": str(code), "text": str(name).strip()})
    return out


async def scrape_catalog(browser):
    """Собирает каталог услуг на трёх языках, объединяя по коду."""
    merged = {}
    for lang in ["lv", "ru", "en"]:
        try:
            responses = []
            page = await open_page(browser, f"{BASE}/{lang}/Booking/Services", responses)
            await load_full_services_list(page)
            items = await page.evaluate(COLLECT_SERVICES_JS)
            # дополняем тем, что сайт сам получил от сервера (надёжнее вёрстки)
            for r in responses:
                if "eveselibaspunkts" not in r.url:
                    continue
                try:
                    ct = (r.headers or {}).get("content-type", "")
                    if "json" in ct:
                        items.extend(services_from_xhr(json.loads(await r.text())))
                except Exception:
                    pass
            await page.close()
            print(f"[catalog] {lang}: {len(items)} записей")
            for it in items:
                # число специалистов — в отдельное поле, из названия убираем
                spec_m = re.search(
                    r"(\d+)\s*(speciālist|специалист|specialist)", it["text"], re.I
                )
                name = re.sub(
                    r"\s*\d+\s*(speciālist\w*|специалист\w*|specialist\w*)\s*",
                    " ",
                    it["text"],
                    flags=re.IGNORECASE,
                ).strip()
                if not name:
                    continue
                entry = merged.setdefault(it["code"], {"code": it["code"]})
                entry.setdefault(lang, name)
                if spec_m:
                    entry.setdefault("spec", int(spec_m.group(1)))
        except Exception as e:
            print(f"[catalog] {lang}: ошибка {e}")
    services = [v for v in merged.values() if v.get("lv") or v.get("ru") or v.get("en")]
    return services


async def check_service(browser, code):
    url = f"{BASE}/lv/Booking/Institutions?ServiceCode={code}"
    responses = []
    page = await open_page(browser, url, responses)

    # Основной путь: читаем JSON, который сайт сам запросил у сервера, —
    # там для каждой клиники явно указан тип GovernmentPaidTime (гос. визит)
    greens = None
    for r in responses:
        if "ListInstitutions" not in r.url:
            continue
        try:
            body = json.loads(await r.text())
            greens = parse_institutions(body)
            print(f"[check {code}] данные из API: {len(body.get('list') or [])} клиник")
        except Exception as e:
            print(f"[check {code}] API не разобрался: {e}")

    data = await page.evaluate(EXTRACT_JS)
    try:
        await page.screenshot(path=f"page_{code}.png", full_page=True)
    except Exception:
        pass
    await page.close()

    if greens is None:
        # Запасной путь: зелёные плашки-даты в вёрстке
        if len(data.get("bodyText", "")) < 300:
            raise RuntimeError(f"страница услуги {code} загрузилась пустой")
        greens = {}
        for g in data.get("greens", []):
            if g["inst"] not in greens:
                greens[g["inst"]] = {"date": g["date"], "addr": g.get("addr", "")}
    return greens, url


async def run():
    # 1. список отслеживаний
    info = http_json(f"{WORKER_URL}/watches?key={urllib.parse.quote(SYNC_SECRET)}")
    watches = info.get("watches", [])
    catalog_ts = info.get("catalog_ts", 0) / 1000.0
    age_days = (datetime.now().timestamp() - catalog_ts) / 86400 if catalog_ts else 1e9
    print(f"отслеживаний: {len(watches)}, каталогу дней: {age_days:.1f}")

    state = load_state()
    svc_state = state.setdefault("svc", {})
    pending = state.setdefault("pending", {})
    hour = riga_hour()

    # Утренняя доставка: отправляем то, что накопилось за тихие часы,
    # тем, у кого тихие часы уже закончились
    chat_quiet = {}
    for w in watches:
        chat_quiet[w["chat"]] = w.get("quiet", [23, 8])
    for chat_key in list(pending.keys()):
        try:
            chat = int(chat_key)
        except ValueError:
            del pending[chat_key]
            continue
        quiet = chat_quiet.get(chat)  # нет отслеживаний → None → шлём сразу
        if in_quiet(hour, quiet):
            continue
        for m in pending[chat_key][:30]:
            tg_send(chat, m["text"], stop_code=m.get("code"), lang=m.get("lang", "ru"))
        del pending[chat_key]
        print(f"[pending] доставлено накопленное для {chat}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        # 2. каталог услуг
        if age_days > CATALOG_MAX_AGE_DAYS:
            services = await scrape_catalog(browser)
            if len(services) >= 10:
                res = http_json(
                    f"{WORKER_URL}/catalog?key={urllib.parse.quote(SYNC_SECRET)}",
                    {"services": services},
                )
                print(f"[catalog] отправлен: {res}")
                Path("services.json").write_text(
                    json.dumps(services, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            else:
                print("[catalog] собрано подозрительно мало, не отправляю")

        # 3. проверка каждой отслеживаемой услуги
        codes = sorted({w["code"] for w in watches})
        for code in codes:
            try:
                greens, url = await check_service(browser, code)
            except Exception as e:
                print(f"[check {code}] ошибка: {e}")
                continue

            new_insts, svc_state[code] = select_to_notify(
                svc_state.get(code, {}), greens, time.time()
            )
            print(f"[check {code}] зелёных клиник: {len(greens)}, к уведомлению: {len(new_insts)}")

            if new_insts:
                items_text = "\n".join(
                    "🟢 {inst} — {date}{addr}".format(
                        inst=i,
                        date=greens[i]["date"],
                        addr=f" ({greens[i]['addr']})" if greens[i]["addr"] else "",
                    )
                    for i in new_insts[:10]
                )
                for w in watches:
                    if w["code"] != code:
                        continue
                    lang = w.get("lang") or "ru"
                    if lang not in MSG:
                        lang = "ru"
                    text = MSG[lang]["found"].format(
                        svc=w.get("name") or code, items=items_text, url=url
                    )
                    if in_quiet(hour, w.get("quiet", [23, 8])):
                        # тихие часы пользователя: копим, доставим утром
                        box = pending.setdefault(str(w["chat"]), [])
                        if len(box) < 30:
                            box.append({"text": text, "code": code, "lang": lang})
                        print(f"[quiet] отложено для {w['chat']}")
                    else:
                        tg_send(w["chat"], text, stop_code=code, lang=lang)

        await browser.close()

    state["consecutive_errors"] = 0
    save_state(state)


def main():
    if not WORKER_URL or not SYNC_SECRET:
        print("ОШИБКА: не заданы WORKER_URL / SYNC_SECRET")
        return 1
    try:
        asyncio.run(run())
        return 0
    except Exception:
        print(traceback.format_exc(limit=5))
        state = load_state()
        n = int(state.get("consecutive_errors", 0)) + 1
        state["consecutive_errors"] = n
        save_state(state)
        # предупреждаем владельцев отслеживаний, но не спамим
        if n == 1 or n % 30 == 0:
            try:
                info = http_json(
                    f"{WORKER_URL}/watches?key={urllib.parse.quote(SYNC_SECRET)}"
                )
                chats = {(w["chat"], w.get("lang") or "ru") for w in info.get("watches", [])}
                for chat, lang in chats:
                    if lang not in MSG:
                        lang = "ru"
                    tg_send(chat, MSG[lang]["error"].format(n=n))
            except Exception:
                pass
        return 0  # не валим workflow — красный крест и так виден


if __name__ == "__main__":
    sys.exit(main())
