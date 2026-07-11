# QMPlay2 — исправление записи LL-HLS потоков

Форк QMPlay2 с исправлениями для надёжной записи живых LL-HLS (Low-Latency HLS)
потоков через встроенный Downloader. До правок запись либо не стартовала, либо
обрывалась ошибкой сразу после начала.

Репозиторий **самодостаточный**: все зависимости (включая содержимое подмодулей
QmVk, Vulkan-Headers, QtWinExtras) закоммичены напрямую, поэтому обычного
`git clone` достаточно — делать `git submodule update` не нужно.

---

## Симптомы (до исправлений)

1. Нажатие **Start** в Downloader для LL-HLS потока ничего не запускало — либо
   мгновенная ошибка, либо «тишина».
2. При воспроизведении поток открывался, но при записи падал с ошибкой записи
   в файл (`WRITE_FAIL`).
3. После сбоя запись «умирала» навсегда — повторный Start не помогал.

---

## Что исправлено

### Правка 1 — `src/qmplay2/StreamMuxer.cpp` (DTS fallback)

Проблема: для LL-HLS пакетов часто отсутствует DTS, и в muxer передавалось
`AV_NOPTS_VALUE`. `av_interleaved_write_frame` возвращал `EINVAL`, запись
завершалась с ошибкой.

```cpp
// До:
if (packet.hasDts())
    pkt.dts = packet.getDts();
// (иначе pkt.dts оставался AV_NOPTS_VALUE)
// После:
if (packet.hasDts())
    pkt.dts = packet.getDts();
else
    pkt.dts = packet.getPts(); // LL-HLS часто не имеет DTS
```

### Правка 2 — `src/modules/Extensions/Downloader.cpp` (пропуск битых пакетов)

Проблема: при ошибке записи одного пакета цикл устанавливал `err = true` и
завершал запись целиком.

```cpp
// До:
if (!m_recMuxer->write(packet)) {
    err = true;
    break;
}
// После:
if (!m_recMuxer->write(packet)) {
    // пропускаем проблемный пакет, продолжаем запись остальных
    continue;
}
```

### Правка 3 — `src/modules/Extensions/Downloader.cpp` (рабочий retry)

Проблема: цикл повтора не работал — `IOController::reset()` не сбрасывал флаг
`br`, из-за чего новый демультиплексор сразу удалялся. Добавлен вызов
`demuxer.resetAbort()` перед каждой попыткой.

```cpp
// Перед каждой попыткой повтора:
demuxer.resetAbort();
```

### Правка 4 — `src/qmplay2/Functions.cpp` (Referer/Origin + multiple_requests)

Универсальное исправление: ряд edge-CDN блокируют воспроизведение/запись,
если в запросе нет заголовков `Referer`/`Origin`, соответствующих источнику
потока. Если заголовок `Referer` не задан, он выводится из хоста самого URL.
Также включается `multiple_requests=1` (многократные запросы сегментов в одной
сессии — критично для LL-HLS).

```cpp
// Some LL-HLS/CDN edge servers block playback unless a Referer/Origin header
// matching the stream origin is present. When none is set, derive it from the URL.
if (!rawHeaders.toLower().contains("referer:") && url.contains("://"))
{
    const QUrl u(url);
    if (u.scheme().startsWith("http"))
    {
        const QString origin = u.scheme() + "://" + u.host();
        rawHeaders += QByteArray("Referer: " + origin + "/\r\nOrigin: " + origin + "\r\n");
    }
}
// ...
av_dict_set(&options, "multiple_requests", "1", 0);
```

### Правка 5 — `src/modules/FFmpeg/FFDemux.cpp` (retry открытия)

Проблема: одиночная попытка `FormatContext::open` для живого потока часто
падает из-за временной недоступности edge. Добавлено до 4 попыток с паузой
1.5 c.

```cpp
const int maxTries = 4;
for (int attempt = 0; attempt < maxTries; ++attempt)
{
    FormatContext *fmtCtx = new FormatContext(...);
    // ... append, open ...
    if (fmtCtx->open(effectiveUrl, param)) { streams_info.append(...); return; }
    // ... erase, delete ...
    if (attempt + 1 < maxTries)
        QThread::msleep(1500);
}
```

### Правка 6 — `src/modules/FFmpeg/FormatContext.cpp` (reconnect/timeout)

Увеличен лимит переподключения, включены `multiple_requests` и явный таймаут
для сетевых потоков, добавлен отладочный лог при неудаче открытия.

```cpp
av_dict_set(&options, "reconnect_delay_max", "30", 0); // было 7
// ...
av_dict_set(&options, "multiple_requests", "1", 0);
av_dict_set(&options, "timeout", "30000000", 0);
```

### Правка 7 — `src/gui/CMakeLists.txt` (qt.conf для Windows)

Проблема: `qt.conf` генерировался только для macOS, из-за чего собранный под
Windows портативный билд не находил Qt-плагины. Добавлена генерация и
установка `qt.conf` в блоке `if(WIN32)`.

Дополнительно в `DemuxerThr.cpp` и `FormatContext.cpp` добавлено
диагностическое логирование в `%TEMP%/QMPlay2_*.log` (без влияния на функционал).

---

## Сборка под Windows (MinGW-w64)

Требования: MSYS2 с тулчейном `mingw64`, `cmake`, `ninja`, компиляторы
`gcc`/`g++`. FFmpeg и libass подтягиваются скриптами QMPlay2 автоматически при
конфигурации (нужен доступ в сеть).

```powershell
# 1. Клонировать
git clone https://github.com/Bestie123/QMPlay2_LLHLS.git
cd QMPlay2_LLHLS

# 2. Сконфигурировать (из MSYS2 / PowerShell с PATH на mingw64\bin)
cmake -S . -B build -G Ninja `
  -DCMAKE_BUILD_TYPE=Release `
  -DUSE_VULKAN=OFF -DUSE_PORTAUDIO=OFF -DUSE_QML=OFF `
  -DCMAKE_INSTALL_PREFIX="$PWD/install"

# 3. Собрать
ninja -C build
```

Сборка FFmpeg на первом шаге занимает значительное время (компилируется из
исходников).

---

## Сборка портативного дистрибутива (`scripts/deploy.py`)

После успешной сборки запустите скрипт, который копирует готовый каталог
`build/src/gui/` (исполняемый файл, модули, `lang/`, Qt-плагины, `qt.conf`) и
доустанавливает системные/Qt DLL через `windeployqt`:

```powershell
python scripts/deploy.py
```

Результат — каталог `QMPlay2_portable/` рядом с репозиторием, готовый к
запуску на чистой Windows (без установки Qt). Скрипт берёт только нужные DLL
(~120 шт., а не всю директорию), поэтому размер дистрибутива минимален.

---

## Проверка (как убедиться, что запись работает)

1. Откройте LL-HLS поток в QMPlay2 (воспроизведение должно стартовать).
2. В Downloader нажмите **Start** — запись должна начаться без ошибок.
3. Файл растёт в каталоге, указанном в настройках `OutputFilePath`.
4. При проблемах смотрите логи в `%TEMP%/QMPlay2_rec.log` и
   `%TEMP%/QMPlay2_open.log`.
