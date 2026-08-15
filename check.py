#!/usr/bin/env python3
"""Перевірка сайту «Дім Млинців» — канал «змінив → подивився → виправив».

Піднімає папку на вільному порту, знімає сторінку в chromium і друкує те,
що не ловиться очима: таб-порядок, обрізаний контент, контраст, шрифти.

    py check.py              # знімки + усі перевірки
    py check.py --shots      # тільки знімки
    py check.py --audit      # тільки перевірки, без знімків

Встановлення один раз:
    py -m pip install playwright
    py -m playwright install chromium
"""
from __future__ import annotations

import argparse
import functools
import http.server
import socket
import socketserver
import sys
import threading
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent
SHOTS = ROOT / 'shots'

# Вікна, на яких ловились реальні дефекти: 360×640 — найтісніший андроїд,
# 390×844 — iPhone, 1280×720 — ноутбук, де герой теж не влазив.
VIEWPORTS = [
    ('mobile-small', 360, 640),
    ('mobile', 390, 844),
    ('tablet', 768, 1024),
    ('laptop', 1280, 720),
    ('desktop', 1440, 900),
    ('desktop-tall', 1680, 1150),   # тут заголовок вилазив на тарілку
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def serve() -> tuple[socketserver.TCPServer, int]:
    port = free_port()
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(ROOT),
    )
    handler.log_message = lambda *a, **k: None  # тиша в консолі
    srv = socketserver.TCPServer(('127.0.0.1', port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# --- перевірки, які не ловляться очима ------------------------------------

FOCUSED = """() => {
  const a = document.activeElement;
  if (!a || a === document.body) return null;
  const r = a.getBoundingClientRect();
  return {
    tag: a.tagName,
    label: (a.getAttribute('aria-label') || a.textContent || '').trim().slice(0, 30),
    hiddenBox: !!a.closest('[aria-hidden="true"]'),
    inert: !!a.closest('[inert]'),
    // Елемент нульового розміру або за межами вікна — фокус, якого не видно.
    offscreen: r.width === 0 || r.height === 0
      || r.bottom < 0 || r.top > innerHeight,
  };
}"""


def tab_order(page, steps: int = 30) -> list[dict]:
    """Обхід Tab. Всередині evaluate це зробити не можна — потрібна клавіатура."""
    page.evaluate('document.body.focus()')
    out = []
    for _ in range(steps):
        page.keyboard.press('Tab')
        item = page.evaluate(FOCUSED)
        if item is None:
            break
        out.append(item)
    return out


# hero.scrollHeight брати не можна: у нього входить декоративне коло
# .hero:before (650px, зміщене вниз), яке навмисно обрізається. Міряємо
# нижній край реального контенту — того, до чого користувач має дістатись.
#
# canScroll теж не можна питати у scrollHeight: при overflow:hidden він
# лишається великим, хоча прокрутити не можна. Тому реально пробуємо.
OVERFLOW = """() => {
  const before = scrollY;
  document.documentElement.style.scrollBehavior = 'auto';
  scrollTo(0, 1e5);
  const maxScroll = scrollY;
  scrollTo(0, before);
  document.documentElement.style.scrollBehavior = '';

  const content = ['.hero-copy', '.product', '.flavor-panel', '.hero-counter',
                   '.dish-prev', '.header'];
  let lowest = 0, lowestSel = '';
  for (const sel of content) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const bottom = el.getBoundingClientRect().bottom + scrollY;
    if (bottom > lowest) { lowest = bottom; lowestSel = sel; }
  }
  return {
    vh: innerHeight,
    contentBottom: Math.round(lowest),
    lowestSel,
    // Скільки контенту лишається за кадром після максимальної прокрутки.
    unreachable: Math.max(0, Math.round(lowest - (maxScroll + innerHeight))),
    maxScroll: Math.round(maxScroll),
    bodyOverflowY: getComputedStyle(document.body).overflowY,
    horizontal: document.documentElement.scrollWidth > innerWidth + 1,
  };
}"""

# Кольори тексту й рамки елементів беремо з браузера, а тло — з фактичного
# пікселя на знімку. Інакше градієнт доводиться вгадувати: для темного
# чорнила гірший випадок — темний кінець градієнта, для світлого — світлий.
SCROLL_INTO_VIEW = r"""(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = el.getBoundingClientRect();
  // Рамку затискаємо у вікно: клаптик за його межами Playwright зняти не може.
  const x = Math.max(0, Math.min(r.x, innerWidth - 1));
  const y = Math.max(0, Math.min(r.y, innerHeight - 1));
  const width = Math.min(r.width, innerWidth - x);
  const height = Math.min(r.height, innerHeight - y);
  if (width < 1 || height < 1) return null;
  return {x, y, width, height};
}"""

CONTRAST_TARGETS = r"""(which) => {
  const sets = {
    hero: [
      ['.eyebrow', '.hero .eyebrow'],
      ['h1', '.hero-copy h1'],
      ['абзац героя', '.hero-copy p'],
      ['години', '.hours strong'],
      ['лічильник', '.hero-counter'],
      ['бренд', '.hero .brand'],
      ['«Замовити»', '.order-link'],
      ['пункт меню', '.nav a:not(.active)'],
      // Картка страви — світле скло на герої. У темній схемі її текст
      // успадковувався від body й ставав світлим на світлому.
      ['назва в картці', '.flavor-panel h2'],
      ['опис у картці', '.flavor-panel p'],
      ['ціна в картці', '.price-row strong'],
      ['мітка «Сьогодні радимо»', '.tag'],
    ],
    // Поверхні сторінки — саме вони змінюються разом зі схемою.
    panel: [
      ['заголовок секції', '.menu-section h2'],
      ['опис секції', '.menu-section .section-head p'],
      ['назва страви', '.menu-card h3'],
      ['опис страви', '.menu-card p'],
      ['ціна', '.menu-card__price strong'],
      ['чипс вимкнений', '.chip:not(.is-on)'],
      ['чипс увімкнений', '.chip.is-on'],
      ['бренд у панелі', '.panel-topbar .brand'],
      // Ці три пройшли повз перший замір темної схеми рівно тому, що їх
      // тут не було: «НАШЕ МЕНЮ», значок «ДМ» і хрестик були темними
      // на темному, а числа мовчали.
      ['надзаголовок секції', '.menu-section .eyebrow'],
      ['значок бренду', '.panel-topbar .brand-mark'],
      ['кнопка закриття', '.panel-close'],
    ],
  };
  const targets = sets[which] || sets.hero;
  const out = [];
  for (const [name, sel] of targets) {
    const el = document.querySelector(sel);
    if (!el) { out.push({name, missing: true}); continue; }
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) { out.push({name, missing: true}); continue; }
    const px = parseFloat(cs.fontSize);
    out.push({
      name, sel,
      color: cs.color,
      px: Math.round(px),
      large: px >= 24 || (px >= 18.66 && parseInt(cs.fontWeight, 10) >= 700),
      box: {x: r.x, y: r.y, width: r.width, height: r.height},
    });
  }
  return out;
}"""

# Старий варіант лишається для довідки: тло з ланцюжка предків.
CONTRAST = r"""() => {
  const lum = ([r, g, b]) => {
    const f = c => { c /= 255; return c <= .03928 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4; };
    return .2126 * f(r) + .7152 * f(g) + .0722 * f(b);
  };
  const parse = s => (s.match(/[\d.]+/g) || []).map(Number);
  const over = (fg, bg) => {
    const a = fg.length > 3 ? fg[3] : 1;
    return [0, 1, 2].map(i => a * fg[i] + (1 - a) * bg[i]);
  };
  const bgOf = el => {
    for (let n = el; n; n = n.parentElement) {
      const cs = getComputedStyle(n);
      const c = parse(cs.backgroundColor);
      if (c.length >= 3 && (c.length < 4 || c[3] > .95)) return c.slice(0, 3);
      if (cs.backgroundImage !== 'none') return null; // градієнт — беремо з пікселя
    }
    return [255, 255, 255];
  };
  const targets = [
    ['.eyebrow', '.hero .eyebrow'],
    ['h1', '.hero-copy h1'],
    ['абзац героя', '.hero-copy p'],
    ['години', '.hours strong'],
    ['лічильник', '.hero-counter'],
    ['пункт меню', '.nav a:not(.active)'],
  ];
  const res = [];
  for (const [name, sel] of targets) {
    const el = document.querySelector(sel);
    if (!el) { res.push({ name, note: 'не знайдено' }); continue; }
    const cs = getComputedStyle(el);
    const fg = parse(cs.color);
    let bg = bgOf(el);
    if (!bg) bg = window.__heroBg || [229, 161, 35];
    const c = over(fg, bg);
    const L1 = lum(c), L2 = lum(bg);
    const ratio = (Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05);
    const px = parseFloat(cs.fontSize);
    const bold = parseInt(cs.fontWeight, 10) >= 700;
    const large = px >= 24 || (px >= 18.66 && bold);
    res.push({
      name, px: Math.round(px), ratio: +ratio.toFixed(2),
      need: large ? 3 : 4.5, pass: ratio >= (large ? 3 : 4.5),
    });
  }
  return res;
}"""

FONTS = """async () => {
  const faces = [...document.fonts].filter(f => f.status === 'loaded');
  const display = getComputedStyle(document.querySelector('.hero-copy h1')).fontFamily;
  const family = display.split(',')[0].replace(/["']/g, '').trim();
  const cyrillic = faces.some(f =>
    f.family === family && /U\\+4[0-9a-f]{2}/i.test(f.unicodeRange));
  return { family, cyrillic,
    loaded: faces.map(f => f.family + ' ' + f.weight).filter((v, i, a) => a.indexOf(v) === i) };
}"""

# Перетин саме прямокутників, а не проєкцій на вісь: два елементи можуть
# ділити вертикальну смугу й стояти поруч по горизонталі.
OVERLAP = """() => {
  const names = {
    '.hero .header': 'шапка', '.hero-copy': 'текст героя',
    '.product': 'тарілка', '.flavor-panel': 'картка страви',
    '.dish-nav': 'стрілки', '.hours': 'години',
  };
  const boxes = [];
  for (const [sel, name] of Object.entries(names)) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    boxes.push({name, el, r: el.getBoundingClientRect()});
  }
  const hits = [];
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++) {
      // Предок завжди накриває нащадка — це не накладання, а вкладеність.
      if (boxes[i].el.contains(boxes[j].el) || boxes[j].el.contains(boxes[i].el))
        continue;
      const a = boxes[i].r, b = boxes[j].r;
      const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      // 4px допуску: тіні й скруглення дають дотик, який оком не читається.
      if (w > 4 && h > 4)
        hits.push(`${boxes[i].name} × ${boxes[j].name} (${Math.round(w)}×${Math.round(h)}px)`);
    }
  return hits;
}"""

# Бокс блока завжди дорівнює його колонці, тож переповнення гліфами через
# getBoundingClientRect не видно — саме тому заголовок непомітно заліз на
# тарілку. Реальну ширину рядків дає Range по вмісту.
TEXT_OVERFLOW = """() => {
  const out = [];
  const targets = ['.hero-copy h1', '.hero-copy p', '.section-head h2',
                   '.menu-card h3', '.flavor-panel h2'];
  for (const sel of targets) {
    for (const el of document.querySelectorAll(sel)) {
      if (el.hidden || el.closest('[inert]')) continue;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const box = el.getBoundingClientRect();
      if (box.width === 0) continue;
      const rng = document.createRange();
      rng.selectNodeContents(el);
      const rects = [...rng.getClientRects()];
      if (!rects.length) continue;
      const over = Math.round(Math.max(...rects.map(r => r.right)) - box.right);
      if (over > 2) out.push({sel, over, text: el.textContent.trim().slice(0, 22)});
    }
  }
  return out;
}"""

TOUCH = """() => {
  const sel = 'a, button, [role=button], input, select';
  const small = [];
  for (const el of document.querySelectorAll(sel)) {
    if (el.closest('[inert]') || el.closest('[aria-hidden="true"]')) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.width < 44 || r.height < 44) {
      small.push({
        label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 24),
        w: Math.round(r.width), h: Math.round(r.height),
      });
    }
  }
  return small;
}"""


def _lum(rgb) -> float:
    def f(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a, b) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def contrast_report(page, problems: list[str], which: str = 'hero') -> None:
    """Контраст із фактичним тлом: найчастіший піксель у рамці елемента.

    Текст займає меншість пікселів свого блока, тож найчастіший колір — це
    тло. Так градієнт не доводиться вгадувати.
    """
    from io import BytesIO
    from PIL import Image

    for t in page.evaluate(CONTRAST_TARGETS, which):
        if t.get('missing'):
            print(f'      {t["name"]}: не знайдено або схований')
            continue
        # Ціль може лежати нижче видимої частини свого контейнера — відколи
        # страва в меню займає цілий екран, ціна опиняється за межами кадру,
        # і знімок клаптика падав із «Clipped area … outside the image».
        # Підводимо ціль у поле зору й перечитуємо рамку.
        box = page.evaluate(SCROLL_INTO_VIEW, t['sel'])
        if not box:
            print(f'      {t["name"]}: не вдалося підвести в кадр')
            continue
        raw = page.screenshot(clip=box)
        img = Image.open(BytesIO(raw)).convert('RGB')
        pixels = list(img.getdata())

        parts = [float(v) for v in t['color']
                 .replace('rgba', '').replace('rgb', '').strip('() ').split(',')]
        fg_raw, alpha = tuple(parts[:3]), (parts[3] if len(parts) > 3 else 1.0)

        # Пікселі самого тексту (і його згладжування) відкидаємо: інакше на
        # градієнті найчастішим кольором стає чорнило, і замір перевертається.
        far = [px for px in pixels
               if sum((px[i] - fg_raw[i]) ** 2 for i in range(3)) > 90 ** 2]
        if not far:
            print(f'      {t["name"]}: тло не відокремити від тексту')
            continue

        # Медіана, а не крайній піксель: рамка елемента ширша за самі літери
        # й зачіпає чужий контент — іконку годинника, фото орбів, водяний
        # знак. По 5-му перцентилю вони видавали себе за тло й перевертали
        # оцінку. Медіана описує поверхню, на якій текст справді лежить.
        far.sort(key=_lum)
        bg = far[len(far) // 2]

        fg = tuple(alpha * fg_raw[i] + (1 - alpha) * bg[i] for i in range(3))
        need = 3.0 if t['large'] else 4.5
        ratio = round(_ratio(fg, bg), 2)
        flag = 'ok ' if ratio >= need else 'ПРОБЛЕМА'
        print(f'      [{flag}] {t["name"]} {t["px"]}px — {ratio}:1 '
              f'(треба {need}:1, найгірше тло {bg})')
        if ratio < need:
            problems.append(f'контраст «{t["name"]}» {ratio}:1 при потрібних {need}:1')


def scheme_report(browser, url: str, problems: list[str], do_shots: bool) -> None:
    """Світла й темна схеми. Темну легко лишити неперевіреною — так уже
    сталося з помаранчевою й фіолетовою темами слайдера."""
    for scheme in ('light', 'dark'):
        ctx = browser.new_context(viewport={'width': 1440, 'height': 900},
                                  device_scale_factor=2, reduced_motion='reduce',
                                  color_scheme=scheme)
        page = ctx.new_page()
        page.goto(url + '#menu', wait_until='networkidle')
        page.wait_for_timeout(1000)
        print(f'  схема {scheme}:')
        surface = page.evaluate(
            "getComputedStyle(document.body).backgroundColor")
        print(f'      тло сторінки {surface}')
        contrast_report(page, problems, which='panel')
        if do_shots:
            page.screenshot(path=str(SHOTS / f'scheme-{scheme}.png'))

        # Герой теж треба міряти в обох схемах: він лишається кольоровим,
        # але світла картка на ньому встигла успадкувати світлий текст.
        page.evaluate('location.hash = "#home"')
        page.wait_for_timeout(800)
        contrast_report(page, problems, which='hero')
        if do_shots:
            page.screenshot(path=str(SHOTS / f'scheme-{scheme}-hero.png'))
        ctx.close()


def routing_report(page, url: str, problems: list[str]) -> None:
    """Розділ має бути адресою: пряме посилання, «назад», активний пункт."""
    print('  адресація:')

    def state():
        return page.evaluate("""() => {
          const open = [...document.querySelectorAll('.panel.is-open')]
            .map(p => p.id.replace('panel-',''));
          const active = [...document.querySelectorAll('.nav a.active')]
            .map(a => a.getAttribute('href'));
          return {open, active, hash: location.hash};
        }""")

    # 1. Пряме відкриття /#menu має одразу показати меню
    page.goto(url + '#menu', wait_until='networkidle')
    page.wait_for_timeout(900)
    st = state()
    ok = st['open'] == ['menu']
    print(f'      [{"ok " if ok else "ПРОБЛЕМА"}] пряме /#menu → відкрито {st["open"] or "нічого"}')
    if not ok:
        problems.append('пряме посилання /#menu не відкриває меню')

    ok = st['active'] == ['#menu']
    print(f'      [{"ok " if ok else "ПРОБЛЕМА"}] активний пункт: {st["active"] or "жодного"}')
    if not ok:
        problems.append(f'активний пункт навігації {st["active"]}, а не #menu')

    # 2. Перехід у інший розділ і кнопка «назад»
    page.evaluate('location.hash = "#contacts"')
    page.wait_for_timeout(700)
    page.go_back()
    page.wait_for_timeout(700)
    st = state()
    ok = st['open'] == ['menu']
    print(f'      [{"ok " if ok else "ПРОБЛЕМА"}] «назад» із контактів → {st["open"] or "нічого"}')
    if not ok:
        problems.append('кнопка «назад» не повертає в попередній розділ')

    # 3. Закриття повертає на головну, а не викидає з сайту
    page.evaluate('location.hash = "#home"')
    page.wait_for_timeout(700)
    st = state()
    ok = st['open'] == [] and st['active'] == ['#home']
    print(f'      [{"ok " if ok else "ПРОБЛЕМА"}] #home → панелей {len(st["open"])}, '
          f'активний {st["active"]}')
    if not ok:
        problems.append('повернення на #home не закриває панель або не оновлює навігацію')

    page.goto(url, wait_until='networkidle')
    page.wait_for_timeout(500)


def content_report(page, problems: list[str]) -> None:
    """Те, що ловиться лише очима, але має лишатись видимим у звіті."""
    r = page.evaluate("""() => {
      const mismatch = [...document.querySelectorAll('[data-photo="mismatch"]')]
        .map(el => (el.querySelector('h3') || {}).textContent
                || el.className.replace(/\\s+/g,' ').trim());
      const ld = document.querySelector('script[type="application/ld+json"]');
      let ldOk = false, ldType = null;
      if(ld){ try { const j = JSON.parse(ld.textContent); ldType = j['@type']; ldOk = true; } catch(e){} }
      return {
        mismatch,
        ldPresent: !!ld, ldValid: ldOk, ldType,
        og: !!document.querySelector('meta[property="og:image"]'),
        icon: !!document.querySelector('link[rel~="icon"]'),
        canonical: !!document.querySelector('link[rel="canonical"]'),
        noAlt: [...document.images].filter(i => !i.alt && i.alt !== '').length,
        h1: document.querySelectorAll('h1').length,
      };
    }""")
    print('  вміст:')
    if r['mismatch']:
        print(f'      [увага] {len(r["mismatch"])} фото суперечать підписам '
              f'(заглушки, потрібні знімки закладу):')
        for m in r['mismatch']:
            print(f'          · {m}')
    else:
        print('      [ok ] непозначених фото-невідповідностей немає')

    for label, ok, note in (
        ('JSON-LD', r['ldPresent'] and r['ldValid'], f'тип {r["ldType"]}'),
        ('og:image', r['og'], ''),
        ('favicon', r['icon'], ''),
        ('рівно один <h1>', r['h1'] == 1, f'знайдено {r["h1"]}'),
        ('усі <img> з alt', r['noAlt'] == 0, f'без alt: {r["noAlt"]}'),
    ):
        flag = 'ok ' if ok else 'ПРОБЛЕМА'
        print(f'      [{flag}] {label} {note}'.rstrip())
        if not ok:
            problems.append(f'{label}: {note or "відсутній"}')

    # canonical і абсолютні og:url потребують справжнього домену — поки його
    # немає, це не помилка, а незакритий пункт.
    if not r['canonical']:
        print('      [чекає] canonical і абсолютний og:url — потрібен домен')


def audit(page, label: str, problems: list[str]) -> None:
    o = page.evaluate(OVERFLOW)
    mark = 'ПРОБЛЕМА' if o['unreachable'] else 'ok '
    print(f'  [{mark}] контент до {o["contentBottom"]}px ({o["lowestSel"]}), '
          f'вікно {o["vh"]}px + прокрутка {o["maxScroll"]}px, '
          f'недосяжно {o["unreachable"]}px')
    if o['unreachable']:
        problems.append(
            f'{label}: {o["unreachable"]}px контенту недосяжні — '
            f'body overflow-y: {o["bodyOverflowY"]}')
    if o['horizontal']:
        problems.append(f'{label}: горизонтальна прокрутка')
        print('  [ПРОБЛЕМА] горизонтальна прокрутка')

    tab = tab_order(page)
    bad = [t for t in tab if t['hiddenBox'] or t['offscreen']]
    if bad:
        problems.append(f'{label}: {len(bad)} прихованих елементів у таб-порядку')
        print(f'  [ПРОБЛЕМА] {len(bad)} з {len(tab)} елементів таб-порядку невидимі:')
        for t in bad[:6]:
            why = 'aria-hidden' if t['hiddenBox'] else 'за екраном'
            print(f'      {t["tag"]}: {t["label"]}  ({why})')
    else:
        print(f'  [ok ] таб-порядок чистий ({len(tab)} елементів)')

    hits = page.evaluate(OVERLAP)
    if hits:
        problems.append(f'{label}: накладання блоків героя')
        print('  [ПРОБЛЕМА] блоки героя накладаються:')
        for hit in hits:
            print(f'      {hit}')
    else:
        print('  [ok ] блоки героя не накладаються')

    over = page.evaluate(TEXT_OVERFLOW)
    if over:
        problems.append(f'{label}: текст виходить за свій блок')
        print('  [ПРОБЛЕМА] текст виходить за межі блока:')
        for o in over:
            print(f'      {o["sel"]} «{o["text"]}» на {o["over"]}px')
    else:
        print('  [ok ] текст не виходить за свої блоки')

    small = page.evaluate(TOUCH)
    if small:
        problems.append(f'{label}: {len(small)} цілей менше 44px')
        print(f'  [ПРОБЛЕМА] цілі менші за 44×44:')
        for s in small[:6]:
            print(f'      «{s["label"]}» {s["w"]}×{s["h"]}')
    else:
        print('  [ok ] усі цілі ≥44×44')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--shots', action='store_true', help='тільки знімки')
    ap.add_argument('--audit', action='store_true', help='тільки перевірки')
    args = ap.parse_args()
    do_shots = not args.audit
    do_audit = not args.shots

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('Немає playwright:  py -m pip install playwright'
              '  &&  py -m playwright install chromium')
        return 2

    SHOTS.mkdir(exist_ok=True)
    srv, port = serve()
    url = f'http://127.0.0.1:{port}/index.html'
    problems: list[str] = []
    console: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for name, w, h in VIEWPORTS:
                print(f'\n=== {name} {w}×{h} ===')
                ctx = browser.new_context(
                    viewport={'width': w, 'height': h},
                    device_scale_factor=2,
                    reduced_motion='reduce',
                )
                page = ctx.new_page()
                page.on('console', lambda m, n=name:
                        console.append(f'{n}: {m.text}') if m.type == 'error' else None)
                page.on('requestfailed', lambda r, n=name:
                        console.append(f'{n}: не завантажилось {r.url[:80]}'))
                page.goto(url, wait_until='networkidle', timeout=60000)
                page.wait_for_timeout(1200)

                if do_shots:
                    page.screenshot(path=str(SHOTS / f'{name}.png'))
                if do_audit:
                    audit(page, name, problems)
                    if name == 'desktop':
                        # Усі три теми слайдера, а не тільки перша: помаранчева
                        # й фіолетова раніше не перевірялись узагалі.
                        for theme in (1, 2, 3):
                            if theme > 1:
                                page.evaluate(
                                    'document.getElementById("dishNext").click()')
                                page.wait_for_timeout(1500)
                            print(f'  тема {theme}:')
                            contrast_report(page, problems)
                        page.evaluate('document.getElementById("dishNext").click()')
                        page.wait_for_timeout(1500)
                        f = page.evaluate(FONTS)
                        routing_report(page, url, problems)
                        content_report(page, problems)
                        scheme_report(browser, url, problems, do_shots)
                        flag = 'ok ' if f['cyrillic'] else 'ПРОБЛЕМА'
                        print(f'      [{flag}] дисплейний шрифт {f["family"]}: '
                              f'кирилиця {"є" if f["cyrillic"] else "НЕ вантажиться"}')
                        if not f['cyrillic']:
                            problems.append(
                                f'{f["family"]} малює кирилицю системним serif')

                # Панелі, оверлей і слайди — саме там ловились дефекти.
                if do_shots and name in ('mobile', 'desktop'):
                    for pid in ('menu', 'about', 'contacts'):
                        page.evaluate(f'location.hash = "#{pid}"')
                        page.wait_for_timeout(800)
                        page.screenshot(path=str(SHOTS / f'{name}-{pid}.png'))
                        page.evaluate('location.hash = "#home"')
                        page.wait_for_timeout(600)
                    page.evaluate('document.getElementById("menuToggle").click()')
                    page.wait_for_timeout(700)
                    page.screenshot(path=str(SHOTS / f'{name}-nav.png'))
                    page.keyboard.press('Escape')
                    page.wait_for_timeout(500)
                    for i in (2, 3):
                        page.evaluate('document.getElementById("dishNext").click()')
                        page.wait_for_timeout(1400)
                        page.screenshot(path=str(SHOTS / f'{name}-dish{i}.png'))
                ctx.close()
            browser.close()
    finally:
        srv.shutdown()

    print('\n' + '=' * 60)
    if console:
        print('Помилки консолі / ресурсів:')
        for c in dict.fromkeys(console):
            print('  ' + c)
    else:
        print('Помилок консолі й ненавантажених ресурсів немає.')

    if problems:
        print(f'\nЗнайдено проблем: {len(problems)}')
        for p_ in dict.fromkeys(problems):
            print('  · ' + p_)
        return 1
    print('Перевірки пройдено.')
    if do_shots:
        print(f'\nЗнімки: {SHOTS}  — подивись на них сам, лічильники не все ловлять.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
