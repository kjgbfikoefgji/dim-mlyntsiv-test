# Локальний сервер для перегляду сайту.
#
#     py serve.py            # http://localhost:8931
#     py serve.py 9000       # інший порт
#
# Навіщо свій, а не `py -m http.server`. Стандартний не шле жодного
# Cache-Control — тільки Last-Modified. Без Cache-Control браузер рахує
# свіжість евристично й може віддати сторінку з кешу, навіть не спитавши
# сервер. 15.08 це тричі виглядало як «я перезавантажив, нічого не
# змінилось»: файл на диску був новий, сервер віддавав новий, а браузер
# показував старий. Помітно було тільки за `?v=2` у адресі.
#
# Тут кожна відповідь іде з no-store, тож звичайне перезавантаження завжди
# показує те, що на диску. Це сервер для розробки; на бойовому хості кеш,
# навпаки, потрібен.

import http.server
import pathlib
import socketserver
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8931


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    # Ще й вимикаємо 304: інакше браузер зі своїм If-Modified-Since отримає
    # «не змінилось» і спокійно покаже власну копію.
    def send_head(self):
        for h in ('If-Modified-Since', 'If-None-Match'):
            while h in self.headers:
                del self.headers[h]
        return super().send_head()

    def log_message(self, fmt, *args):
        # Тихо про звичайні 200, голосно про решту.
        if args and str(args[1]).startswith('2'):
            return
        super().log_message(fmt, *args)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    with Server(('127.0.0.1', PORT), Handler) as httpd:
        print(f'Дім Млинців → http://localhost:{PORT}/  (кеш вимкнено)')
        print(f'Папка: {ROOT}')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nЗупинено.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
