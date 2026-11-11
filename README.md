# Atlas File Gateway

Минимальный личный файловый шлюз: **Web UI + GET API**. Предназначен для обмена текстовыми и бинарными файлами с агентами. **На VPS пока не развернут.**

## Возможности
- Web UI: вход по паролю, вставка текста в редактор и сохранение как UTF-8 файла, загрузка с компьютера, список, скачивание, удаление.
- GET API: `/api/list`, `/api/read?name=...`, `/api/write?name=...&data=BASE64`, для больших файлов `/api/chunk` + `/api/finish`.
- Отдельные секреты для администратора и API, ограничение размера, проверка SHA-256 при сборке, имена файлов без путей и директорий.
- Все файлы лежат в `./storage/files`; административная панель не предоставляет shell-доступ.

## Быстрый локальный запуск (Debian с Docker Compose)
```bash
git clone https://github.com/dowjonezz/Atlas-File-Gateway.git
cd Atlas-File-Gateway
cp .env.example .env
python3 - <<'PY'
from pathlib import Path
import secrets
p = Path(".env")
s = p.read_text()
for key in ("ADMIN_PASSWORD", "API_TOKEN", "SESSION_SECRET"):
    s = s.replace("REPLACE_WITH_LONG_RANDOM_PASSWORD" if key == "ADMIN_PASSWORD" else "REPLACE_WITH_SEPARATE_LONG_RANDOM_TOKEN" if key == "API_TOKEN" else "REPLACE_WITH_ANOTHER_LONG_RANDOM_SECRET", secrets.token_urlsafe(36))
p.write_text(s)
p.chmod(0o600)
PY
mkdir -p storage
sudo chown -R 10001:10001 storage
docker compose up -d --build
curl -fsS http://127.0.0.1:18080/health
```
В `.env` заданные пароли читать только на VPS; **не размещать `.env` в GitHub и не передавать мне токены в чат**.

## Reverse proxy и TLS
На VPS с работающими 3x-ui и nginx **не устанавливать Nginx Proxy Manager**: он добавит лишние процессы, интерфейс управления и риск конфликта портов 80/443. Используйте уже установленный nginx, если он обслуживает сертификаты/трафик.

Нужен отдельный DNS-адрес, например `files.example.com`, и отдельный HTTPS server block. Существующие настройки 3x-ui **не менять**. Проверьте существующие `nginx -T`, `ss -lntp`, расположение сертификатов и механизм обновления. IP-сертификат 3x-ui не всегда подходит для нового домена; выдайте сертификат с нужным именем.

Пример блока nginx (заменить имя и пути сертификата):
```nginx
server {
    listen 443 ssl;
    server_name files.example.com;
    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;
    client_max_body_size 11m;
    access_log off; # GET query may contain sensitive Base64 and legacy tokens
    add_header Cache-Control "no-store" always;
    add_header X-Robots-Tag "noindex, nofollow" always;
    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```
Включить HTTPS redirect с порта 80 с учётом уже существующих настроек; проверять `nginx -t` до reload. Пример **не готов для копирования без проверки существующего nginx**.

## API
Для скриптов передавайте токен в **заголовке X-API-Key**; URL-параметр `token` поддержан только как экспериментальный режим для клиентов, которые не умеют устанавливать заголовки. GET-запись нестандартна: прокси и браузеры могут выполнять/повторять такие запросы. Не публикуйте URL с токеном или Base64-содержимым, не пересылайте их посторонним.

```bash
# token должен находиться в переменной окружения, не печатайте его в истории терминала
curl -fsS -H "X-API-Key: $ATLAS_API_TOKEN" "https://files.example.com/api/list"
curl -fsS -G -H "X-API-Key: $ATLAS_API_TOKEN" --data-urlencode "name=note.txt" "https://files.example.com/api/read"
curl -fsS -G -H "X-API-Key: $ATLAS_API_TOKEN" --data-urlencode "name=note.txt" --data-urlencode "data=SGVsbG8=" "https://files.example.com/api/write"
```

Для бинарных файлов используйте `/api/read?name=...&encoding=base64`. `/api/write` ограничен короткими Base64 строками (до 6000 символов). Для больших данных клиент делит байты на части, отправляет части как `/api/chunk?upload_id=<random>&index=0&total=N&data=<BASE64>`, затем вызывает `/api/finish?upload_id=...&total=N&name=...&sha256=<full-file-sha256>`. Все GET требуют токен.

## Границы безопасности и эксплуатации
- Публичный адрес сервера обязательно должен работать по **HTTPS**.
- Разные сильные секреты для UI, API и подписи cookie; UI cookie `HttpOnly; Secure; SameSite=Strict`.
- Режим query-token менее безопасен, чем заголовок; URL могут попадать в историю, логи и диагностику. Reverse proxy без access log — дополнительная защита, не абсолютная.
- **Пока это прототип, не размещайте чувствительные данные.** Нет ротации токенов из UI, квот на временные chunks, уборки незавершённых chunks, rate limiting, полноценного аудита и проверки антивирусом.
- Endpoint `/health` публичный; файлы отдаются только после авторизации.
- GET-запись и GET-части могут изменять данные, поэтому это не совместимо с ожиданиями некоторых браузерных/поисковых инструментов.
- **Доступ именно из ChatGPT пока не подтверждён**: проверка API через curl не доказывает, что этот чат может записывать файлы.

## Временные ссылки на чтение (10 минут)
В Web UI напротив файла нажмите **«Ссылка на 10 мин»**. Администраторская сессия нужна только для создания ссылки. Кнопка копирует публичный URL вида `https://host:8443/shared?name=...&expires=...&signature=...`.

Ссылка даёт доступ **только на чтение одного файла** без API_TOKEN и пароля. Действует 10 минут, автоматически перестаёт работать при изменении содержимого файла и не является одноразовой: до истечения срока её можно открыть несколько раз. Не распространяйте ссылку для конфиденциальных файлов. Пользователь, имеющий ссылку, может передать её дальше. Удаление файла или изменение SESSION_SECRET отзывает доступ.

**Важное ограничение:** ссылка сама по себе не гарантирует, что облачный веб-инструмент ChatGPT сможет открыть IP:8443 — это нужно проверить по факту. Для обновления на VPS из каталога проекта: `git pull --ff-only && docker compose up -d --build`, если исходники на VPS клонированы из Git и локальных изменений нет. При загрузке проекта копированием обновите исходники перед пересборкой.
