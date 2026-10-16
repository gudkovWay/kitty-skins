--!nonstrict
-- Служба Wallpaper Studio: единственный вход к бэкенду.
--
-- Бэкенд — установленный лаунчер ~/.local/bin/wallpaper-studio (обёртка над
-- python3 …/backend/studio.py). Три вызова, все отвечают JSON в stdout:
--   snapshot — только чтение, дёшево;
--   sync     — чтение поставщика + опциональное применение одобренного,
--              сам троттлит себя до 5 секунд;
--   request  — заявка в очередь (scan/analyze/generate/cancel/retry/
--              preferences/approve/reject/apply), дешёвая: она только
--              ставит задачу, тяжёлая работа в воркере.
--
-- Ключи состояния — только наши, чужих не трогаем:
--   studio.status   последний снимок + флаги занятости;
--   studio.request  заявка панели: {nonce, request}. Служба смотрит ключ
--                   через state.watch — заявка забирается мгновенно, а не в
--                   очередном цикле; цикл остаётся запасным путём.
--
-- Заявки — FIFO: ни одна не затирается. sync и request выполняются строго
-- по одному за раз (одна общая блокировка запуска), новые заявки копятся и
-- стартуют по мере освобождения. Результат каждой заявки — свежий снимок,
-- поэтому после неё сразу sync не нужен.
--
-- Нонсы: при старте службы lastNonce берётся из уже лежащей заявки, поэтому
-- перезапуск службы не переигрывает оплаченную команду. Панель, в свою
-- очередь, сеет nonce из существующего ключа.
--
-- Уведомления — только о НОВЫХ терминальных состояниях задач. При
-- старте службы фиксируется базовый снимок: всё, что уже лежало на диске
-- (вчерашние failed и needs_capture), спамом не считаем.

local KEY_STATUS = "studio.status"
local KEY_REQUEST = "studio.request"
local SERVICE = "q/wallpaper-studio:watch"

-- os.getenv в песочнице может не быть (соседние плагины пути не доверяют
-- окружению), поэтому локальный фолбэк на дом хозяина. Но подстановку
-- WALLPAPER_STUDIO_LAUNCHER делает сам shell: если getenv недоступен,
-- фолбэк молча указал бы на живой установленный бэкенд прямо в песочнице.
-- ${WALLPAPER_STUDIO_LAUNCHER:-…} раскрывается в окружении процесса,
-- переменные аргументы уходят через "$@" без всякого шелл-цитирования.
local HOME = (os.getenv and os.getenv("HOME")) or "/home/q"
local DEFAULT_LAUNCHER = HOME .. "/.local/bin/wallpaper-studio"

-- Единый транспорт к бэкенду. Полный ответ легко превышает ограниченный
-- буфер stdout runAsync (1 МиБ), поэтому бэкенд пишет его в уникальный
-- временный файл, а через bounded stdout наружу уходит только имя файла.
-- Колбэк читает файл целиком (readFileAsync, до 4 МиБ) и лишь потом объявляет
-- вызов завершённым: пока идёт чтение, очередь FIFO занята и следующий запуск
-- не стартует. Ошибку бэкенд по-прежнему кладёт JSON-ом в stdout (в файл),
-- поэтому parse сначала видит её, а stderr/код — только запасной путь.
local function backendArgv(args)
  local script = table.concat({
    "umask 077",
    'out=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/wallpaper-studio.XXXXXX") || exit 111',
    "printf '%s\\n' \"$out\"",
    "trap 'rm -f -- \"$out\"' HUP INT TERM",
    '"${WALLPAPER_STUDIO_LAUNCHER:-' .. DEFAULT_LAUNCHER .. '}" "$@" >"$out"',
    "code=$?",
    'exit "$code"',
  }, "\n")
  return { "/bin/sh", "-c", script, "wallpaper-studio", unpack(args) }
end

-- Временный файл создаём мы, и удалять можно только точный его путь.
-- Пустой/неожиданный вывод не чистим и чужой путь из ответа не трогаем.
local function ownedTemp(path)
  return type(path) == "string"
    and path:sub(1, 1) == "/"
    and path:find("wallpaper-studio.", 1, true) ~= nil
end

local function removeTemp(path)
  noctalia.runAsync({ "rm", "-f", "--", path })
end

local function readFailure(err)
  if err == "file too large" then
    return "ответ бэкенда больше предела чтения (4 МиБ)"
  end
  return "не удалось прочитать ответ бэкенда: " .. tostring(err)
end

-- done(res, nil) — ответ для parse: содержимое файла в res.stdout, код и
-- stderr бэкенда сохранены. done(nil, why) — сбой транспорта. Ровно один
-- вызов на запуск, включая отказ постановки runAsync/readFileAsync.
local function runBackend(args, timeout, done)
  local started = noctalia.runAsync(backendArgv(args), function(res)
    if type(res) ~= "table" then
      done(nil, "процесс не запустился")
      return
    end
    local path = noctalia.string.trim(res.stdout or "")
    if res.stdoutTruncated or not ownedTemp(path) then
      done(nil, "не удалось получить временный файл ответа")
      return
    end
    if res.timedOut then
      removeTemp(path)
      done(nil, "истёк таймаут вызова бэкенда")
      return
    end
    local accepted = noctalia.readFileAsync(path, function(data, err)
      removeTemp(path)
      if err ~= nil then
        done(nil, readFailure(err))
        return
      end
      done({ stdout = data, stderr = res.stderr, exitCode = res.exitCode }, nil)
    end)
    if accepted == false then
      -- Колбэк не сработает: не подвешиваем очередь, убираем свой файл.
      removeTemp(path)
      done(nil, "не удалось запустить чтение ответа")
    end
  end, timeout)
  if started == false then
    done(nil, "процесс не запустился")
  end
end

local POLL_MS = 5000     -- цикл службы; sync сам себя троттлит до 5 с
-- Худший случай вызова: library validate+use (2×120 с) поверх discovery —
-- покрываем 360 с на каждую из тяжёлых команд; циклы не наслаиваются.
local SYNC_MS = 360000
local REQUEST_MS = 360000

-- Терминальные состояния задач: о них сообщаем один раз.
local TERMINAL = {
  succeeded = true, needs_capture = true, failed = true,
  cancelled = true, interrupted = true,
}

local WORD = {
  queued = "в очереди", running = "идёт", succeeded = "готово",
  needs_capture = "нужен захват", failed = "ошибка",
  cancelled = "отменено", interrupted = "прервано",
}

local KINDRUS = {
  scan = "Обзор", analyze_all = "Разбор", analyze = "Разбор",
  generate = "Генерация", capture = "Захват кадров", import = "Импорт",
  preview = "Живое превью",
  wallpaper_apply = "Включение обоев", wallpaper_stop = "Выключение обоев",
}

local status = {
  snapshot = nil,
  at = 0,
  requestBusy = false,
  syncBusy = false,
  inflightNonce = nil,
  lastError = "",   -- провал явной заявки: чистит только следующая заявка/успех
  syncError = "",   -- провал периодического sync: чистит успешный sync
}

local notified = {}          -- id задач, о которых уже сообщали
local baselineDone = false
local lastNonce = nil        -- последний принятый от панели нонс
local queue = {}             -- FIFO заявок, ожидающих запуска
local running = nil          -- заявка в полёте (таблица {nonce, request})
local syncing = false
local drain                  -- вперёд: колбэки запуска зовут её раньше определения

local function publish()
  status.at = os.time()
  noctalia.state.set(KEY_STATUS, status)
end

-- Через notify-send argv-таблицей: никакого шелла и кавычек, всё честно
-- одним аргументом на значение.
local function notify(urgency, title, body)
  noctalia.runAsync({ "notify-send", "-a", "wallpaper-studio",
    "-u", urgency, "-t", "8000", tostring(title), tostring(body) })
end

-- Разбираем stdout snapshot/sync/request. Ошибка приходит JSON-ом В STDOUT
-- даже при ненулевом коде: сначала пробуем его, и только потом падаем на
-- stderr/код — иначе честное сообщение бэкенда теряется.
local function parse(res)
  if type(res) ~= "table" then
    return nil, "процесс не запустился"
  end
  local got = noctalia.json.decode(res.stdout or "")
  if type(got) == "table" and type(got.error) == "string" and got.error ~= "" then
    return nil, got.error
  end
  if res.exitCode ~= 0 then
    local why = noctalia.string.trim(res.stderr or "")
    if why == "" then why = "код " .. tostring(res.exitCode) end
    return nil, why
  end
  if type(got) ~= "table" then
    return nil, "бэкенд ответил не JSON"
  end
  return got, nil
end

-- Уведомляем о задачах, которые стали терминальными ПОСЛЕ базового снимка.
local function announceTerminal(snap)
  for _, job in ipairs(snap.jobs or {}) do
    if TERMINAL[job.state] and job.id and not notified[job.id] then
      notified[job.id] = true
      local what = KINDRUS[job.kind] or job.kind or "Задача"
      local detail = ""
      if job.state == "needs_capture" then
        detail = "нужны кадры захвата"
      elseif job.state == "failed" and type(job.error) == "string" and job.error ~= "" then
        detail = job.error
      end
      notify(job.state == "succeeded" and "normal" or "critical",
        what .. ": " .. (WORD[job.state] or job.state), detail ~= "" and detail or "статус — в панели")
    end
  end
end

-- wallpaper-set сохраняет кадр для палитры, но не отдаёт фоновый слой
-- внешнему плееру. Владение слоем следует за подтверждённым playback,
-- а не за заявкой в очереди. Сохраняем только наши захваченные выходы:
-- новый экземпляр службы сможет освободить их после hot reload.
local wallpaperOutputs = {}
do
  local prior = noctalia.state.get(KEY_STATUS)
  if type(prior) == "table" and type(prior.wallpaperOutputs) == "table" then
    for output, managed in pairs(prior.wallpaperOutputs) do
      if managed == true then wallpaperOutputs[output] = true end
    end
  end
end
status.wallpaperOutputs = wallpaperOutputs

local function syncWallpaperSurfaces(snap)
  local playback = snap.playback
  if type(playback) ~= "table" or type(playback.enabled) ~= "boolean"
    or type(playback.current) ~= "table" then
    return -- неполный снимок не доказывает остановку плеера
  end
  local playing = {}
  if playback.enabled then
    for output, record in pairs(playback.current) do
      if type(record) == "table" and record.status == "playing"
        and type(record.pid) == "number" and record.pid > 0 then
        playing[output] = true
        if not wallpaperOutputs[output] then
          noctalia.setWallpaperEnabled(output, false)
          wallpaperOutputs[output] = true
        end
      end
    end
  end
  for output in pairs(wallpaperOutputs) do
    if not playing[output] then
      noctalia.setWallpaperEnabled(output, true)
      wallpaperOutputs[output] = nil
    end
  end
end

local function accept(snap, keepAction)
  if type(snap) ~= "table" then
    return
  end
  -- Периодический sync отвечает без last_action: это не команда хозяина,
  -- и её фидбек затирать нельзя — переносим последнее явное действие.
  if keepAction and snap.last_action == nil
    and type(status.snapshot) == "table" then
    snap.last_action = status.snapshot.last_action
  end
  status.snapshot = snap
  syncWallpaperSurfaces(snap)
  if baselineDone then
    announceTerminal(snap)
  else
    -- Базовый снимок: всё уже терминальное помечаем увиденным молча.
    for _, job in ipairs(snap.jobs or {}) do
      if TERMINAL[job.state] and job.id then
        notified[job.id] = true
      end
    end
    baselineDone = true
  end
end

local function syncNow()
  if syncing or running then
    return
  end
  syncing = true
  status.syncBusy = true
  publish()
  runBackend({ "sync" }, SYNC_MS, function(res, err)
    syncing = false
    status.syncBusy = false
    local snap, why
    if res then
      snap, why = parse(res)
    else
      why = err
    end
    if snap then
      -- Наблюдение (sync) не чистит lastError проваленной заявки и не
      -- трогает её фидбек; свой syncError оно обновляет.
      status.syncError = ""
      accept(snap, true)
    else
      status.syncError = "sync: " .. why
    end
    publish()
    drain()
  end)
end

local function launch(req)
  running = req
  status.requestBusy = true
  status.inflightNonce = req.nonce
  publish()
  runBackend({ "request", noctalia.json.encode(req.request) }, REQUEST_MS, function(res, err)
    running = nil
    status.requestBusy = false
    status.inflightNonce = nil
    local snap, why
    if res then
      snap, why = parse(res)
    else
      why = err
    end
    if snap then
      -- Явная команда: успех чистит оба признака провала, свежий снимок
      -- несёт свой last_action.
      status.lastError = ""
      status.syncError = ""
      accept(snap)
    else
      status.lastError = "заявка: " .. why
    end
    publish()
    drain()
  end)
end

-- Запуск следующей работы, пока свободно: FIFO-заявки имеют приоритет,
-- sync подстраивается.
drain = function()
  if running or syncing then
    return
  end
  local nextReq = table.remove(queue, 1)
  if nextReq then
    launch(nextReq)
    return
  end
end

-- Новая заявка: nonce сменился — ставим в очередь FIFO, ничего не затираем.
local function takeRequest()
  local req = noctalia.state.get(KEY_REQUEST)
  if type(req) ~= "table" or req.nonce == nil or req.nonce == status.inflightNonce then
    return
  end
  for _, q in ipairs(queue) do
    if q.nonce == req.nonce then
      return                       -- уже в очереди
    end
  end
  if lastNonce ~= nil and req.nonce == lastNonce then
    return
  end
  lastNonce = req.nonce
  table.insert(queue, req)
  drain()
end

function update()
  takeRequest()
  syncNow()
end

function onIpc(event)
  if event == "refresh" then
    syncNow()
  elseif event == "rerequest" then
    takeRequest()
  elseif event == "status" then
    -- Диагностика по уже собранному состоянию: без вызова бэкенда, без путей
    -- и без полезной нагрузки — только то, что служба и так знает.
    local snap = status.snapshot
    local items = (type(snap) == "table" and type(snap.items) == "table") and #snap.items or nil
    noctalia.log(string.format(
      "wallpaper-studio: snapshot=%s items=%s syncBusy=%s requestBusy=%s syncError=%q lastError=%q",
      tostring(snap ~= nil), tostring(items),
      tostring(status.syncBusy), tostring(status.requestBusy),
      status.syncError, status.lastError))
  end
end


-- База нонсов из существующего состояния: перезапуск службы не должен
-- переигрывать последнюю (возможно, платную) заявку панели.
do
  local prior = noctalia.state.get(KEY_REQUEST)
  lastNonce = (type(prior) == "table" and prior.nonce ~= nil) and prior.nonce or nil
end
-- Мгновенный забор заявки, без ожидания цикла.
noctalia.state.watch(KEY_REQUEST, function(_value)
  takeRequest()
end)

noctalia.setUpdateInterval(POLL_MS)
publish()
syncNow()
