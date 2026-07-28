# oled-stats — деплой на Raspberry Pi 5

Сервис вывода системной статистики на 1.5" OLED **128×128** (драйвер SH1107, интерфейс I²C).
Экран: заголовок (имя хоста + uptime) и шесть строк — CPU (загрузка / температура),
RAM (занято / всего), SSD (свободно / всего + температура NVMe), SSID (+ значок силы
WiFi-сигнала справа от имени сети), IP, Fan (обороты).

Библиотека работы с экраном **`oleddisplay` устанавливается из отдельного репозитория** —
локальной копии её кода здесь нет.

- Репозиторий библиотеки: https://github.com/Jacksotnik/rpi5-sh1107-oled-128x128
- Рабочая копия репо (на Mac): `~/my_projs/rpi5-sh1107-oled-128x128`

## Содержимое `~/oled-stats/`

| Путь | Назначение |
|------|------------|
| `stats_oled.py` | приложение: сбор метрик + вёрстка экрана + цикл обновления; импортирует установленный `oleddisplay`. Самостоятельный код — живёт только здесь, правится прямо на малинке (с репозиторием не синхронизируется) |
| `requirements.txt` | зависимости venv: библиотека из git + `psutil` |
| `venv/` | виртуальное окружение (сюда установлен `oleddisplay`) |
| `README.md` | этот файл |

systemd-юнит `oled-stats.service` лежит **не здесь**, а в `/etc/systemd/system/` (см. ниже).

## Архитектура

- **Библиотека `oleddisplay`** — ставится в `venv` из GitHub-репозитория (`pip install git+…`).
  Единственный источник её кода — репозиторий; в этой папке копии нет.
- **Приложение `stats_oled.py`** — тонкий потребитель библиотеки (метрики RPi5 + цикл). Его
  канонический источник — `examples/stats.py` в репозитории; на малинке лежит деплой-копия.
- **Сервис `oled-stats.service`** — запускает приложение при загрузке системы и перезапускает
  при падении.

## Автозапуск при старте (systemd)

Юнит `/etc/systemd/system/oled-stats.service`:

```ini
[Unit]
Description=OLED system stats display (SH1107 128x128, I2C)
After=multi-user.target

[Service]
Type=simple
User=admin
WorkingDirectory=/home/admin/oled-stats
ExecStart=/home/admin/oled-stats/venv/bin/python /home/admin/oled-stats/stats_oled.py --interval 5 --rotate 3 --contrast 72 --night-contrast 16
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Построчно:

- `After=multi-user.target` — стартует после инициализации многопользовательского режима
  (базовые сервисы уже подняты).
- `Type=simple` — процесс не форкается, systemd считает его запущенным сразу после `ExecStart`.
- `User=admin` — работает от пользователя `admin`; он состоит в группе `i2c`, поэтому имеет
  доступ к `/dev/i2c-1` без root.
- `WorkingDirectory=/home/admin/oled-stats` — рабочий каталог процесса.
- `ExecStart=…/venv/bin/python …/stats_oled.py --interval 5 --rotate 3 --contrast 72 --night-contrast 16`
  — запуск приложения интерпретатором **из venv** (там установлен `oleddisplay`): обновление раз в
  5 секунд, поворот `3` (270°), дневной контраст (яркость) `72` из диапазона 0..255. Дефолт панели
  SH1107 в luma — `127`, поэтому для заметного затемнения `--contrast` надо ставить ощутимо ниже 127.
  `--night-contrast 16` — ночью **00:00–06:00** (окно настраивается через `--night-start`/`--night-end`,
  по местному времени малинки) яркость опускается до `16`, ради экономии и замедления выгорания OLED;
  требует заданного `--contrast` (значение, к которому вернуться днём).
- Экран **гаснет при штатной остановке/выключении**: приложение ловит SIGTERM (его шлёт systemd при
  `stop`/`poweroff`) и корректно очищает панель. Иначе последний кадр «висел» бы, пока не снимут
  питание с модуля.
- `Restart=on-failure` + `RestartSec=3` — при аварийном завершении перезапуск через 3 секунды.
- `WantedBy=multi-user.target` — при `enable` создаётся симлинк в `multi-user.target.wants/`,
  благодаря чему сервис поднимается на каждой загрузке.

Сейчас сервис **enabled** (автозапуск включён). Управление:

```bash
sudo systemctl status oled-stats     # состояние
sudo systemctl restart oled-stats    # перезапустить
sudo systemctl stop oled-stats       # остановить
sudo systemctl start oled-stats      # запустить
sudo systemctl disable oled-stats    # выключить автозапуск
sudo journalctl -u oled-stats -f     # смотреть логи в реальном времени
```

Если правишь сам юнит (например, меняешь `--interval` или добавляешь `--rotate`):

```bash
sudo nano /etc/systemd/system/oled-stats.service
sudo systemctl daemon-reload          # перечитать юнит
sudo systemctl restart oled-stats
```

## Сборка и деплой после изменений

Код библиотеки живёт в репозитории. Порядок действий зависит от того, что менялось.

### A. Изменения в библиотеке `oleddisplay`

Правится в рабочей копии репо **на Mac** (`~/my_projs/rpi5-sh1107-oled-128x128`).

1. Сделать личный аккаунт gh активным (нужен для push в личный репозиторий):
   ```bash
   gh auth switch --hostname github.com --user Jacksotnik
   ```
2. Отредактировать код и прогнать тесты локально:
   ```bash
   cd ~/my_projs/rpi5-sh1107-oled-128x128
   ./.venv/bin/python -m unittest discover -s tests
   ```
3. Закоммитить и запушить:
   ```bash
   git add -A && git commit -m "…"
   git push
   ```
4. **На малинке** переустановить библиотеку из свежего HEAD и перезапустить сервис:
   ```bash
   ssh rpi
   ~/oled-stats/venv/bin/pip install --upgrade --force-reinstall --no-deps --no-cache-dir \
       "git+https://github.com/Jacksotnik/rpi5-sh1107-oled-128x128.git"
   sudo systemctl restart oled-stats
   sudo journalctl -u oled-stats -n 20 --no-pager   # убедиться, что без ошибок
   ```

> ⚠️ Версия пакета обычно не меняется (`0.1.0`), поэтому простой `pip install -r requirements.txt`
> **не подтянет** новый код — pip решит, что уже установлено. Для обновления обязательны флаги
> `--force-reinstall --no-cache-dir`.

### B. Изменения в приложении `stats_oled.py`

`stats_oled.py` — самостоятельный код: он живёт **только на малинке** и с репозиторием не связан.
Репозиторий — это лишь библиотека `oleddisplay`; `examples/stats.py` в нём — независимый пример,
а **не** источник этого приложения. Поэтому правим файл прямо на малинке и перезапускаем сервис:

```bash
ssh rpi
nano ~/oled-stats/stats_oled.py                                        # правки
~/oled-stats/venv/bin/python -m py_compile ~/oled-stats/stats_oled.py   # быстрая проверка синтаксиса
sudo systemctl restart oled-stats
sudo journalctl -u oled-stats -n 20 --no-pager                         # убедиться, что без ошибок
```

Перед крупной правкой удобно снять рядом резервную копию: `cp stats_oled.py stats_oled.py.bak`.

### Пересоздание venv с нуля

```bash
cd ~/oled-stats
python3 -m venv --system-site-packages venv
./venv/bin/pip install -r requirements.txt
```

## Важные предостережения

- **Не обращаться к дисплею вторым процессом**, пока работает сервис: при выходе `luma` шлёт
  панели display-off (`0xAE`), экран гаснет, а сервис его заново не включает. Для ручных
  экспериментов сперва `sudo systemctl stop oled-stats`.
- **Push в личный репозиторий** идёт от активного аккаунта gh — для этого репо нужен `Jacksotnik`
  (`gh auth switch --hostname github.com --user Jacksotnik`). На чтение (`git fetch`/`clone`)
  публичный репозиторий доступен и без переключения.
- Особенность железа (ложные NAK по младшему биту) и её программный обход описаны в README
  самого репозитория библиотеки.
