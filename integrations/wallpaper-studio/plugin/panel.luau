--!nonstrict
-- Панель Wallpaper Studio: три вкладки — «Обои», «Оформление», «Очередь».
--
-- Панель — тонкий клиент. Ничего не считается и не запускается здесь:
--   · состояние приходит из studio.status, который пишет служба;
--   · действия уходят через studio.request — панель кладёт таблицу
--     {nonce, request}, служба выполняет её сериализованно и обновляет
--     снимок. Ответы сами по себе сюда не пишутся, перерисовку делает
--     watch на studio.status.
--
-- Черновики полей переживают обновления службы: ui.input в этой сборке
-- неконтролируемый — value засеивает поле один раз, дальше буфер держит хост,
-- а мы лишь слушаем onChange. Поэтому перерисовка после каждого sync не
-- затирает набираемый текст.
--
-- Захват кадров автоматический: выбор обои в каталоге один раз просит
-- службу сделать capture, если записи ещё нет и задача не идёт. Никаких
-- ручных путей манифеста и никакого вызова моделей на выбор/поиск/импорт.
--
-- Дорогие кнопки честны: «Разобрать» и «Сгенерировать» зовут модели, это
-- написано прямо в подсказках.
--
-- Живое превью: явная кнопка просит службу открыть (или обновить) одно
-- плавающее окно рамки для выбранного кандидата — настоящее окно на
-- живом композиторе, никакого экрана-записи. Провал открытия не переигрывает
-- платную генерацию: это отдельная задача kind='preview'. Статичное
-- превью-макет остаётся и честно подписано. Клик по превью обоев или по кадру
-- захвата открывает полноэкранный просмотр (его показывает бэкенд). Сброс
-- данных обоев — иконка рядом с «Разобрать» с именным подтверждением; он
-- чистит черновики только этих обоев.

local KEY_STATUS = "studio.status"
local KEY_REQUEST = "studio.request"
local SERVICE = "q/wallpaper-studio:watch"

-- Общий скин рабочего стола: рамка 12px со всех сторон (24px внутренних
-- издержек панели), blossom-углы/рейки и шрифтовые константы. Пути
-- ui.image внутри рамки — относительно 'skin-assets/…'.
local skin = require("./desktop_skin.luau")

-- ──────────────────────────────────────────────────────────── словари

local TABS = { { id = "wallpapers", label = "Обои", glyph = "image" },
  { id = "style", label = "Оформление", glyph = "palette" },
  { id = "queue", label = "Очередь", glyph = "history" } }

local FILTERS = { { id = "all", label = "Все" },
  { id = "workshop", label = "Workshop" },
  { id = "import", label = "Мои файлы" } }

local ORIGIN = { workshop = "Workshop", ["import"] = "мой файл" }
local KINDR = { scene = "сцена", web = "веб", video = "видео", image = "картинка" }

local META = {
  missing = { "метаданных нет", "on_surface_variant" },
  recorded = { "разметка записана", "primary" },
  preview = { "только превью", "tertiary" },
  invalid = { "разметка неверна", "error" },
  stale = { "разметка устарела", "tertiary" },
}

local CAPST = {
  queued = { "кадры: в очереди", "on_surface_variant" },
  running = { "кадры: снимаются", "primary" },
  ready = { "кадры готовы", "primary" },
  failed = { "кадры: ошибка", "error" },
}

local VSTATE = {
  candidate = { "кандидат", "tertiary" },
  approved = { "одобрен", "primary" },
  rejected = { "отклонён", "error" },
}

local JOB = {
  queued = { "в очереди", "on_surface_variant" },
  running = { "идёт", "primary" },
  succeeded = { "готово", "primary" },
  needs_capture = { "нужен захват", "tertiary" },
  failed = { "ошибка", "error" },
  cancelled = { "отменено", "on_surface_variant" },
  interrupted = { "прервано", "error" },
}

local KIND = { scan = "Обзор", analyze_all = "Разбор нового",
  analyze = "Разбор", generate = "Генерация",
  capture = "Захват кадров", import = "Импорт",
  preview = "Живое превью",
  wallpaper_apply = "Включение обоев", wallpaper_stop = "Выключение обоев" }

local DETAIL = {
  { id = "minimal", label = "минимальная" },
  { id = "balanced", label = "сбалансированная" },
  { id = "ornate", label = "насыщенная" },
}
local THICK = {
  { id = "thin", label = "тонкая" },
  { id = "normal", label = "обычная" },
  { id = "bold", label = "жирная" },
}
local MOTION = {
  { id = "static", label = "статика" },
  { id = "candles", label = "пресет «свечи»" },
}

-- Режим оформления: общий художественный язык, отдельно от цели.
-- quality переносит глубину/иерархию/материалы, gothic явно сохраняет
-- готический визуальный язык.
local DESIGN_MODE = {
  { id = "quality", label = "Качество" },
  { id = "gothic", label = "Готика" },
}

-- Цель оформления — продукт, а не движок отрисовки: рамка окон или
-- браузерный интерфейс. Снимок присылает design_targets; если его ещё нет
-- (старый бэкенд), честно показываем только доступную рамку.
local TARGET_FALLBACK = {
  { id = "frame", label = "Рамка окон", available = true, reason = "" },
}

-- Машинные имена этапов службы → русские подписи. Неизвестный этап
-- показываем как есть, ничего не выдумывая.
local STAGE = {
  scanning = "обзор каталога", classifying = "классификация",
  analyzing = "разбор", capturing = "съёмка кадров",
  importing = "импорт", applying = "включение", stopping = "выключение",
  opening = "открытие окна", preview = "превью",
  ["art-direction"] = "арт-бриф", ["frame-layout"] = "разметка рамки",
  ["image-generation"] = "генерация изображения", generating = "генерация изображения",
  assembling = "сборка", publishing = "публикация",
}

-- ───────────────────────────────────────────────────────────── состояние

local status = noctalia.state.get(KEY_STATUS) or {}
local tab = "wallpapers"
local selContent = nil        -- content_id выбранной обои
local selVariant = nil        -- показанный вариант оформления
local selOutput = nil         -- выбранный монитор; nil → ALL (все подключённые)
local filterIdx = 1

-- nonce сеется из существующей заявки: перезапуск панели/службы не должен
-- переигрывать прежнюю (возможно, платную) команду.
local nonce = 0
do
  local prior = noctalia.state.get(KEY_REQUEST)
  local n = type(prior) == "table" and tonumber(prior.nonce) or nil
  if n ~= nil then
    nonce = math.floor(n)
  end
end

-- Черновики. Заметки — на каждую обоину своя, остальное — общее.
local search = ""
local notes = {}              -- [content_id] = текст
local likes, dislikes, commonNotes = "", "", ""
local visionModel = ""
local detailIdx, thickIdx, motionIdx = 2, 2, 1
local modeIdx = 1             -- quality по умолчанию; сеется из preferences.design_mode
local selTarget = "frame"     -- цель оформления: frame доступна, browser — нет
local autoApply = false
local outputIdx = 1
local techOpen = false        -- техническая секция оформления свёрнута
local evidenceOpen = false    -- технические строки доказательства свёрнуты
local usageOpen = false       -- подробный расход генераций свёрнут

-- Внутренние бюджеты с учётом рамки скина: панель 1040x780 снаружи,
-- frame резервирует 12px с каждой стороны — фиксированные ширины уменьшены
-- на 24px, чтобы правая колонка и сетка не вылезали за внутреннюю область.
local DETAILS_W = 316         -- было 340 (340 - 24)
local THUMB_W, THUMB_H = 184, 104
local importError = nil       -- провал диалога импорта (не отмена)
local prefsSeen = false       -- снимок уже засеял черновики настроек
local prefsDirty = false      -- черновики настроек ещё не сохранены

-- Основа оформления — по-обойные черновики четырёх полей из item.design_basis.
-- source/values/overrides считает служба, панель только показывает values,
-- держит правки и отправляет патч из изменённых полей.
local BASIS_FIELDS = { "style_mood", "materials_motifs",
  "palette_lighting", "composition" }
local BASIS_LABELS = {
  style_mood = "Стиль и настроение",
  materials_motifs = "Материалы и мотивы",
  palette_lighting = "Палитра и освещение",
  composition = "Композиция",
}
local basisDrafts = {}        -- [content_id][field] = текст черновика
local basisDirty = {}         -- [content_id][field] = true — ещё не сохранено
local basisPending = {}       -- [content_id] = {field=value} — отправленный патч, ждём подтверждения в overrides
local basisInputs = {}        -- [content_id][field] = {value, revision}; ключ не меняется при вводе
local notesRev = {}           -- [content_id] = номер ревизии поля заметок: сброс пересоздаёт поле

-- Причина решения по варианту (approve/reject). Черновик на каждый вариант
-- переживает опросы службы и повторный показ; чистится только когда служба
-- подтвердила запись отзыва (design_feedback или last_action).
local reasonDrafts = {}       -- [variant_id] = текст причины
local reasonRev = {}          -- [variant_id] = номер ревизии поля: сброс пересоздаёт ввод
local reasonPending = {}      -- [variant_id] = {verdict=approved|rejected, reason=текст} — отправлено, ждём подтверждения

-- Живое превью: явное окно открывает службу по кнопке ниже; статичный
-- макет остаётся до первого открытия.
local PREVIEW_H = 248         -- высота превью-макета (260 минус издержка рамки)

-- Сброс данных обоев: подтверждение в панели и одноразовое подтверждение
-- службы по reset_token.
local resetConfirmId = nil    -- content_id, для которого просим подтверждение
local lastResetToken = nil    -- уже применённый reset_token: второй раз не чистим

local render                  -- вперёд: обработчики зовут её раньше определения

-- ───────────────────────────────────────────────────────────── служебное

local function snap()
  return type(status.snapshot) == "table" and status.snapshot or nil
end

local function busy()
  return status.requestBusy == true or status.syncBusy == true
end

local function counts()
  local s = snap()
  local c = s and s.counts or nil
  local function num(name)
    return tonumber(type(c) == "table" and c[name]) or 0
  end
  return { queued = num("queued"), running = num("running"),
    candidates = num("candidates"), needs_capture = num("needs_capture") }
end

local function resetBusy()
  local c = counts()
  return busy() or c.queued > 0 or c.running > 0
end

local function send(request)
  nonce += 1
  noctalia.state.set(KEY_REQUEST, { nonce = nonce, request = request, at = os.time() })
  render()
end

local function poke(event)
  noctalia.runAsync("noctalia msg plugin " .. SERVICE .. " all " .. event)
end

local function itemId(it)
  return tostring((it and (it.id or it.content_id)) or "")
end

local function itemTitle(it)
  return tostring((it and (it.title or it.name)) or "") ~= ""
    and tostring(it.title or it.name) or itemId(it)
end

local function itemPreview(it)
  return it and (it.preview_path or it.preview or it.thumbnail or it.path or it.file) or nil
end

local function items()
  local s = snap()
  return (s and type(s.items) == "table") and s.items or {}
end

local function itemById(id)
  for _, it in ipairs(items()) do
    if itemId(it) == id then
      return it
    end
  end
  return nil
end

local function variantsOf(contentId)
  local s = snap()
  local out = {}
  for _, v in ipairs((s and s.variants) or {}) do
    if v.content_id == contentId then
      table.insert(out, v)
    end
  end
  return out
end

local function variantById(id)
  local s = snap()
  for _, v in ipairs((s and s.variants) or {}) do
    if v.id == id then
      return v
    end
  end
  return nil
end

-- ─────────────────────────────────────── цель оформления и отзывы владельца

-- Метаданные целей из снимка (design_targets). Пока бэкенд старой версии их
-- не присылает, честно показываем одну доступную рамку, без выдуманного
-- браузера.
local function designTargets()
  local s = snap()
  local list = s and s.design_targets
  if type(list) == "table" and #list > 0 then
    return list
  end
  return TARGET_FALLBACK
end

local function targetRecord(id)
  for _, t in ipairs(designTargets()) do
    if tostring(type(t) == "table" and t.id or "") == tostring(id) then
      return t
    end
  end
  return nil
end

local function targetLabel(id)
  local t = targetRecord(id)
  return t ~= nil and tostring(t.label or t.id) or tostring(id or "")
end

local function targetAvailable(id)
  local t = targetRecord(id)
  return type(t) == "table" and t.available == true
end

local function targetReason(id)
  local t = targetRecord(id)
  local r = type(t) == "table" and t.reason or nil
  return type(r) == "string" and r or ""
end

-- Записанные отзывы владельца: {variant_id, content_id, target, verdict,
-- reason, created_at}. Пока записи нет — пустой список, а не догадка.
local function feedbackList()
  local s = snap()
  return (s and type(s.design_feedback) == "table") and s.design_feedback or {}
end

local function feedbackFor(variantId)
  local out = {}
  for _, rec in ipairs(feedbackList()) do
    if tostring(rec.variant_id or "") == tostring(variantId) then
      table.insert(out, rec)
    end
  end
  return out
end

local VERDICT = {
  approved = { "одобрено", "primary" },
  rejected = { "отклонено", "error" },
}

-- Художественный бриф варианта из provenance.art_direction. Отсутствие —
-- честный факт для старых пакетов, а не пустой блок.
local function artDirectionFor(v)
  local prov = type(v) == "table" and v.provenance or nil
  local ad = type(prov) == "table" and prov.art_direction or nil
  return type(ad) == "table" and ad or nil
end

-- Строки брифа: общий тезис/материалы/перевод мотивов и рамка
-- (структура/иерархия/свет). Плюс id брифа и пути полной истории запроса.
local function briefLines(ad)
  local out = {}
  local common = type(ad.common) == "table" and ad.common or {}
  local frame = type(ad.frame) == "table" and ad.frame or {}
  local function text(key, label, value, color)
    if type(value) == "string" and value ~= "" then
      table.insert(out, { key = key, text = label .. ": " .. value,
        color = color or "on_surface" })
    end
  end
  text("thesis", "Тезис", common.thesis)
  if type(common.materials) == "table" then
    local parts = {}
    for _, m in ipairs(common.materials) do
      if type(m) == "string" and m ~= "" then
        table.insert(parts, m)
      end
    end
    if #parts > 0 then
      text("materials", "Материалы", table.concat(parts, ", "), "on_surface_variant")
    end
  end
  text("palette", "Палитра и свет", common.palette_lighting, "on_surface_variant")
  if type(common.motif_translation) == "table" then
    for i, mt in ipairs(common.motif_translation) do
      if type(mt) == "table" then
        local obs = type(mt.observation) == "string" and mt.observation or ""
        local tr = type(mt.translation) == "string" and mt.translation or ""
        if obs ~= "" or tr ~= "" then
          local line = "Перевод мотива: " .. obs .. " → " .. tr
          if mt.omit == true then
            line = line .. " (не переносить)"
          end
          table.insert(out, { key = "motif-" .. tostring(i), text = line,
            color = "on_surface_variant" })
        end
      end
    end
  end
  text("structure", "Структура", frame.structure, "on_surface_variant")
  text("hierarchy", "Иерархия", frame.hierarchy, "on_surface_variant")
  text("lighting", "Свет", frame.lighting, "on_surface_variant")
  local pieces = {}
  if type(ad.id) == "string" and ad.id ~= "" then
    table.insert(pieces, "id " .. ad.id)
  end
  local req = type(ad.request) == "table" and ad.request or {}
  if type(req.dir) == "string" and req.dir ~= "" then
    table.insert(pieces, "каталог " .. req.dir)
  end
  for _, k in ipairs({ "system_path", "prompt_path", "request_path", "response_path" }) do
    if type(req[k]) == "string" and req[k] ~= "" then
      table.insert(pieces, k .. " " .. req[k])
    end
  end
  if #pieces > 0 then
    table.insert(out, { key = "paths", text = "Запись брифа: "
      .. table.concat(pieces, " · "), color = "on_surface_variant" })
  end
  return out
end

-- Задача открытия живого превью именно этого варианта (kind='preview',
-- payload.variant_id). Свежие идут последними, поэтому последнее совпадение.
local function previewJobFor(variantId)
  if variantId == nil then
    return nil
  end
  local s = snap()
  local found = nil
  for _, job in ipairs((s and s.jobs) or {}) do
    local payload = type(job.payload) == "table" and job.payload or {}
    local vid = payload.variant_id
    if vid == nil then
      vid = job.variant_id
    end
    if job.kind == "preview" and tostring(vid or "") == tostring(variantId) then
      found = job
    end
  end
  return found
end

-- Показ варианта: живое окно — короткий статус, иначе прежний статичный
-- макет на всю ширину панели. Никаких изображений живого окна здесь нет.
local function previewNode(v)
  local rp = type(v) == "table" and v.render_preview or nil
  if type(rp) == "table" and rp.kind == "live" then
    return ui.column({ key = "preview", gap = 2 }, {
      ui.row({ key = "liverow", gap = 6, align = "center" }, {
        ui.glyph({ name = "terminal", size = 16, color = "primary" }),
        ui.label({ key = "lbl", fontSize = 11, text = "живое окно открыто",
          color = "primary" }),
      }),
      ui.label({ key = "hint", fontSize = 9,
        text = "плавающее окно рамки " .. tostring(rp.window_title or "")
          .. " — кадр переключает плагин",
        color = "on_surface_variant" }),
    })
  end
  if type(v.preview_path) == "string" and v.preview_path ~= "" then
    return ui.column({ key = "preview", gap = 2 }, {
      ui.image({ key = "img", path = v.preview_path, height = PREVIEW_H,
        radius = 6, fit = "contain" }),
      ui.label({ key = "lbl", fontSize = 9,
        text = "статичное превью-макет — живая рамка выглядит иначе",
        color = "on_surface_variant" }),
    })
  end
  return nil
end

local function mappedVariant(contentId)
  local s = snap()
  local m = s and s.mappings or nil
  local vid = type(m) == "table" and m[contentId] or nil
  return vid and variantById(vid) or nil
end

-- Применённое сейчас — только из last_applied бэкенда: одобренная замена
-- (mapping) и реально применённый вариант — разные вещи.
local function appliedVariantId(contentId)
  local s = snap()
  local la = s and s.last_applied or nil
  if type(la) ~= "table" then
    return nil
  end
  if la.content_id ~= nil and la.content_id ~= contentId then
    return nil
  end
  local vid = la.variant_id
  return vid and tostring(vid) or nil
end

-- Подключённые выходы — от композитора (noctalia.outputs, как в WEngine),
-- не из снимка: на свежей установке снимок ещё пуст, а мониторы уже есть.
local function connectedOutputs()
  local seen, out = {}, {}
  local list = noctalia.outputs()
  if type(list) == "table" then
    for _, output in ipairs(list) do
      local name = type(output) == "table" and output.name or output
      local str = tostring(name or "")
      if str ~= "" and not seen[str] then
        seen[str] = true
        table.insert(out, str)
      end
    end
  end
  return out
end

-- Выходы для рамок окон: «авто» (пустой preference output) плюс
-- реально подключённые выходы.
local function outputOptions()
  local out = { "авто" }
  for _, name in ipairs(connectedOutputs()) do
    table.insert(out, name)
  end
  return out
end

-- Мониторы для плеера: literal ALL (все подключённые) первым, затем
-- реально подключённые выходы. Выбор конкретного выхода сохраняется, пока
-- он ещё подключён (chosenOutput); сброс/отключение → ALL.
local function monitorOptions()
  local out = { "ALL" }
  for _, name in ipairs(connectedOutputs()) do
    table.insert(out, name)
  end
  return out
end

-- Что сейчас играет на выбранном мониторе (или везде: не выбран или ALL).
local function playingIds(output)
  local s = snap()
  local pb = s and s.playback or nil
  local cur = type(pb) == "table" and pb.current or nil
  local out = {}
  if type(cur) == "table" then
    for name, rec in pairs(cur) do
      if rec ~= nil and type(rec) == "table"
        and (output == nil or output == "ALL" or name == output)
        and rec.status == "playing" and rec.content_id ~= nil then
        out[rec.content_id] = true
      end
    end
  end
  return out
end

local function playbackEnabled()
  local s = snap()
  local pb = s and s.playback or nil
  return type(pb) == "table" and pb.enabled == true or false
end

local function selId(list, id)
  for i, entry in ipairs(list) do
    if entry.id == id then
      return i
    end
  end
  return 1
end

-- Целевой выход плеера: literal ALL по умолчанию; конкретное имя — только
-- если выбор ещё подключён (иначе откат на ALL).
local function chosenOutput()
  if selOutput ~= nil then
    for _, name in ipairs(connectedOutputs()) do
      if name == selOutput then
        return selOutput
      end
    end
  end
  return #monitorOptions() > 1 and "ALL" or nil
end

-- ─────────────────────────────────────────────── захват по выбору каталога

-- Идёт ли уже задача захвата для этой обои.
local function captureJobActive(contentId)
  local s = snap()
  for _, job in ipairs((s and s.jobs) or {}) do
    if job.kind == "capture" and job.content_id == contentId
      and (job.state == "queued" or job.state == "running") then
      return true
    end
  end
  return false
end

-- Выбор обои просит кадры ровно один раз: запись есть или задача идёт —
-- ничего не отправляем. Никаких моделей здесь не участвует.
local function ensureCapture(it)
  if it == nil then
    return
  end
  if it.capture ~= nil then
    return
  end
  if captureJobActive(itemId(it)) then
    return
  end
  send({ action = "capture", content_id = itemId(it) })
end

-- ──────────────────────────────────────────────────────── импорт из файла

-- Диалог zenity: argv-таблицей, без шелла и кавычек. Код 1 (и пустой
-- ответ) — отмена, тихо; прочие ненулевые коды — настоящая ошибка, её
-- показываем в панели, а не глотаем.
local function importDialog()
  noctalia.runAsync({ "zenity", "--file-selection", "--title=Импорт обоев",
    "--file-filter=Видео и GIF | *.mp4 *.MP4 *.gif *.GIF",
    "--file-filter=Все файлы | *" }, function(res)
    local path = noctalia.string.trim((type(res) == "table" and res.stdout) or "")
    if type(res) ~= "table" then
      return
    end
    if res.exitCode == 0 and path ~= "" then
      importError = nil
      send({ action = "import", path = path })
      return
    end
    if res.exitCode == 1 or path == "" then
      importError = nil            -- отмена — нормальный исход
      return
    end
    importError = "диалог импорта не открылся: код "
      .. tostring(res.exitCode)
    render()
  end, 600000)
end

-- ────────────────────────────────────────────────────────── предпочтения

-- Эффективное значение поля основы для выбранной обои: values из снимка
-- (source, поверх которого служба наложила сохранённые overrides).
local function basisValue(contentId, f)
  local it = itemById(contentId)
  local db = it and it.design_basis or nil
  local values = type(db) == "table" and type(db.values) == "table"
    and db.values or nil
  local v = values and values[f] or nil
  return type(v) == "string" and v or ""
end

local function basisDirtyAny(contentId)
  local d = basisDirty[contentId]
  if type(d) ~= "table" then
    return false
  end
  for _, f in ipairs(BASIS_FIELDS) do
    if d[f] then
      return true
    end
  end
  return false
end

-- Подтверждение сохранения: сверяем отправленный патч с overrides из
-- снимка — именно с сохранёнными переопределениями, а не с эффективными
-- values, иначе совпадение значений ошибочно гасило бы «грязность».
-- Поле гасим, только если черновик с тех пор не менялся; правки,
-- сделанные, пока сохранение летело, остаются черновиком.
local function ackBasis()
  local s = snap()
  local list = (s and type(s.items) == "table") and s.items or nil
  if list == nil then
    return
  end
  for contentId, pending in pairs(basisPending) do
    local overrides = nil
    for _, it in ipairs(list) do
      if itemId(it) == contentId then
        local db = it.design_basis
        overrides = type(db) == "table" and type(db.overrides) == "table"
          and db.overrides or {}
        break
      end
    end
    if overrides ~= nil then
      for f, v in pairs(pending) do
        if overrides[f] == v then
          local d = basisDrafts[contentId]
          if d ~= nil and d[f] == v then
            d[f] = nil
            if basisDirty[contentId] ~= nil then
              basisDirty[contentId][f] = nil
            end
          end
          pending[f] = nil
        end
      end
      if next(pending) == nil then
        basisPending[contentId] = nil
      end
    end
  end
end

-- Черновик очищает только новая запись после отправки решения.
-- Исторический отзыв с тем же текстом не подтверждает новый запрос.
-- Совпадение по variant_id+verdict+reason:
-- отзыв не стирает набранный текст.
local function ackFeedback()
  local function acknowledged(vid, pending)
    for index, rec in ipairs(feedbackList()) do
      if index > pending.after
        and tostring(rec.variant_id or "") == vid
        and tostring(rec.verdict or "") == pending.verdict
        and tostring(rec.reason or "") == pending.reason then
        return true
      end
    end
    return false
  end
  for vid, pending in pairs(reasonPending) do
    if acknowledged(vid, pending) then
      -- Черновик гасим, только если он не менялся, пока летел ответ:
      -- набранное после отправки не должно теряться.
      if reasonDrafts[vid] == pending.reason then
        reasonDrafts[vid] = nil
      end
      reasonPending[vid] = nil
      reasonRev[vid] = (reasonRev[vid] or 0) + 1
    end
  end
end

-- Подтверждение сброса: служба успешно сбросила данные этих обоев и прислала
-- reset_token. Чистим черновики ТОЛЬКО этих обоев — основу, заметки и снятое
-- выделение варианта; общие предпочтения и черновики других обоев не трогаем.
-- Ревизия поля заметок и полей основы сдвигается, чтобы неконтролируемый
-- ui.input засеялся заново (иначе буфер хозяина сохранил бы старый текст).
local function clearContentDrafts(contentId)
  basisDrafts[contentId] = nil
  basisDirty[contentId] = nil
  basisPending[contentId] = nil
  notes[contentId] = nil
  notesRev[contentId] = (notesRev[contentId] or 0) + 1
  local inputs = basisInputs[contentId]
  if type(inputs) == "table" then
    for _, f in ipairs(BASIS_FIELDS) do
      local inp = inputs[f]
      if type(inp) == "table" then
        inp.value = ""
        inp.revision = (tonumber(inp.revision) or 0) + 1
      end
    end
  end
  -- Показанный вариант этих обоев больше не существует — снять показ.
  if selVariant ~= nil then
    local v = variantById(selVariant)
    if v == nil or tostring(v.content_id) == tostring(contentId) then
      selVariant = nil
    end
  end
  -- Черновики причин вариантов этих обоев тоже больше не к чему привязать.
  local staleReason = {}
  for vid in pairs(reasonDrafts) do
    local v = variantById(vid)
    if v == nil or tostring(v.content_id) == tostring(contentId) then
      table.insert(staleReason, vid)
    end
  end
  for _, vid in ipairs(staleReason) do
    reasonDrafts[vid] = nil
    reasonPending[vid] = nil
    reasonRev[vid] = (reasonRev[vid] or 0) + 1
  end
  if resetConfirmId == contentId then
    resetConfirmId = nil
  end
end

-- Одноразовое подтверждение сброса из last_action: последнее явное действие
-- reset c неудачей не чистит. Токен гасим, чтобы переживающий sync не сыграл
-- очистку второй раз.
local function consumeReset()
  local s = snap()
  local la = s and s.last_action or nil
  if type(la) ~= "table" or la.action ~= "reset" or la.ok ~= true then
    return
  end
  if la.content_id == nil or la.reset_token == nil then
    return
  end
  if lastResetToken == la.reset_token then
    return
  end
  lastResetToken = la.reset_token
  clearContentDrafts(tostring(la.content_id))
end

-- Общие предпочтения и основа выбранной обои сохраняются одной заявкой:
-- глобальные значения как раньше, плюс content_id и патч только из
-- изменённых полей основы. Значения для патча фиксируем в момент
-- отправки (basisPending) — трактовка ответа идёт по ним, а не по тому,
-- что успели набрать, пока заявка летела.
local function requestPrefs()
  local values = {
    likes = likes,
    dislikes = dislikes,
    notes = commonNotes,
    vision_model = visionModel,
    output = outputIdx > 1 and outputOptions()[outputIdx] or "",
    auto_apply = autoApply == true,
    design_mode = DESIGN_MODE[modeIdx].id,
    kitty = {
      detail = DETAIL[detailIdx].id,
      thickness = THICK[thickIdx].id,
      motion = MOTION[motionIdx].id,
    },
  }
  local request = { action = "preferences", values = values }
  if selContent ~= nil and basisDirtyAny(selContent) then
    local patch = {}
    local drafts = basisDrafts[selContent] or {}
    local dirty = basisDirty[selContent] or {}
    for _, f in ipairs(BASIS_FIELDS) do
      if dirty[f] then
        patch[f] = type(drafts[f]) == "string" and drafts[f] or ""
      end
    end
    if next(patch) ~= nil then
      request.content_id = selContent
      request.design_basis = patch
      basisPending[selContent] = patch
    end
  end
  send(request)
end

-- ─────────────────────────────────────────────────────── куски разметки

-- Подсветка задаётся ЯВНО в обоих состояниях: незаданное свойство хост
-- оставляет прежним, и «прозрачный» фон без значения не снимается.
local PLAIN = "surface_variant/0"
local PICKED = "primary/0.14"

local function chip(key, pair)
  return ui.label({ key = key, text = "· " .. pair[1], fontSize = 11,
    color = pair[2] })
end

-- Поля не бывают узкими: подпись сверху, ввод на всю ширину (flexGrow и у
-- колонки, и у ввода — иначе колонка не растягивается в строке).
local function field(key, label, value, placeholder, onDraft)
  return ui.column({ key = key, gap = 2, flexGrow = 1 }, {
    ui.label({ key = key .. "-label", text = label, fontSize = 11,
      color = "on_surface_variant" }),
    ui.input({ key = key .. "-input", value = value, placeholder = placeholder,
      flexGrow = 1,
      onChange = function(text)
        if onDraft then onDraft(text or "") end
        render()
      end }),
  })
end

local function selectRow(key, label, options, index, onChange)
  return ui.column({ key = key, gap = 2, flexGrow = 1 }, {
    ui.label({ key = key .. "-label", text = label, fontSize = 11,
      color = "on_surface_variant" }),
    ui.select({ key = key .. "-select", options = options, flexGrow = 1,
      -- selectedIndex тоже нулевой: наружу отдаём index-1, от колбэка
      -- принимаем нулевой и переводим обратно (замер WEngine:718-734).
      selectedIndex = math.max(0, math.min(index - 1, #options - 1)),
      onChange = function(i)
        local idx = math.floor(tonumber(i) or -1) + 1
        if idx >= 1 and idx <= #options then
          onChange(idx)
        end
      end }),
  })
end

local function toggleRow(key, label, checked, onChange)
  return ui.row({ key = key, gap = 8, align = "center" }, {
    ui.toggle({ key = key .. "-toggle", checked = checked,
      -- колбэк ui.toggle приходит БЕЗ значения (WEngine:765-770): новое
      -- состояние — инверсия текущего локального, иначе флаг всегда false.
      onChange = function()
        onChange(not checked)
      end }),
    ui.label({ text = label, fontSize = 11, color = "on_surface",
      flexGrow = 1 }),
  })
end

-- ──────────────────────────────────────────────────────────────── шапка

local function header()
  local c = counts()
  local parts = {}
  if c.queued > 0 then table.insert(parts, "в очереди " .. c.queued) end
  if c.running > 0 then table.insert(parts, "идёт " .. c.running) end
  if c.candidates > 0 then table.insert(parts, "кандидаты " .. c.candidates) end
  if c.needs_capture > 0 then table.insert(parts, "нужен захват " .. c.needs_capture) end
  local s = snap()
  local err = ""
  for _, candidate in ipairs({ status.lastError, status.syncError,
    (s and type(s.sync_error) == "string") and s.sync_error or "" }) do
    if candidate ~= "" then
      err = candidate
      break
    end
  end
  return ui.row({ key = "head", gap = 8, align = "center" }, {
    ui.image({ key = "emblem", path = "skin-assets/emblem.png", width = 24,
      height = 24, radius = 6, fit = "cover" }),
    ui.label({ text = "Wallpaper Studio", fontSize = 15,
      fontFamily = skin.FONT, fontWeight = "semibold" }),
    ui.spacer({ flexGrow = 1 }),
    ui.label({ key = "player", text = playbackEnabled() and "плеер включён"
      or "плеер выключен", fontSize = 11, color = "on_surface_variant" }),
    ui.label({ key = "count", text = busy() and "…" or table.concat(parts, " · "),
      fontSize = 12, color = "on_surface_variant" }),
    ui.button({
      key = "refresh", glyph = "refresh", controlSize = "sm", variant = "ghost",
      tooltip = "свежий снимок", enabled = not busy(),
      onClick = function() poke("refresh") end,
    }),
    ui.button({
      key = "close", glyph = "close", controlSize = "sm", variant = "ghost",
      onClick = function() panel.close() end,
    }),
  }), err
end

local function tabbar()
  local c = counts()
  local row = {}
  for _, t in ipairs(TABS) do
    local badge = t.id == "queue" and (c.queued + c.running) or 0
    table.insert(row, ui.button({
      key = "tab-" .. t.id,
      text = t.label .. (badge > 0 and (" " .. badge) or ""),
      glyph = t.glyph,
      controlSize = "sm",
      variant = tab == t.id and "primary" or "ghost",
      onClick = function()
        tab = t.id
        render()
      end,
    }))
  end
  return ui.row({ key = "tabs", gap = 6, align = "center" }, row)
end

-- ─────────────────────────────────────── расход генераций: честный блок

-- Контракт бэкенда status.generation_usage:
--   {attempts, reported_attempts, unknown_attempts,
--    tokens:{input,output,total}, latest, recent}
-- recent/latest: {id, status, provider, model, started_at, finished_at,
--   text_tokens:{input,output,total}, image_usage (объект или null),
--   unknown_fields}. Агрегаты — подытог ТОЛЬКО известных попыток; null
--   означает «неизвестно», а не 0. Никаких процентов, долларов и долей
--   квоты здесь не выводится.

local function usageNum(v)
  return tonumber(v)
end

local function tokensLine(prefix, t)
  if type(t) ~= "table" then
    return prefix .. ": неизвестно"
  end
  local i, o, tot = usageNum(t.input), usageNum(t.output), usageNum(t.total)
  if i == nil and o == nil and tot == nil then
    return prefix .. ": неизвестно"
  end
  local function fmt(n)
    return n ~= nil and tostring(math.floor(n)) or "?"
  end
  return prefix .. ": ввод " .. fmt(i) .. " · вывод " .. fmt(o)
    .. " · всего " .. fmt(tot)
end

-- Расход изображений — отдельная квота, не текстовые токены: показываем
-- факт наличия и реальные поля объекта, ничего не вычисляя.
local function imageUsageShort(rec)
  if rec == nil or rec.image_usage == nil then
    return nil
  end
  if type(rec.image_usage) ~= "table" then
    return "расход изображений: " .. tostring(rec.image_usage)
  end
  local parts = {}
  for k, v in pairs(rec.image_usage) do
    table.insert(parts, tostring(k) .. "=" .. tostring(v))
  end
  table.sort(parts)
  return "расход изображений: " .. table.concat(parts, ", ")
end

local function attemptLine(prefix, rec)
  if type(rec) ~= "table" then
    return nil
  end
  local st = type(rec.status) == "string" and rec.status or "?"
  local model = type(rec.model) == "string" and rec.model ~= "" and rec.model or "модель неизвестна"
  local line = prefix .. ": " .. st .. " · " .. model
  if type(rec.provider) == "string" and rec.provider ~= "" then
    line = line .. " · " .. rec.provider
  end
  if type(rec.started_at) == "string" and rec.started_at ~= "" then
    line = line .. " · начата " .. rec.started_at
  end
  return line
end

local function usageCounterText()
  local u = type(status.generation_usage) == "table"
    and status.generation_usage or nil
  if u == nil then
    return "расход генераций: данных от службы ещё нет"
  end
  local attempts = usageNum(u.attempts) or 0
  local unknown = usageNum(u.unknown_attempts) or 0
  local parts = { "попыток генерации: " .. tostring(attempts) }
  if unknown > 0 then
    table.insert(parts, "с неизвестным расходом: " .. tostring(unknown))
  end
  local t = type(u.tokens) == "table" and u.tokens or nil
  if t ~= nil and (usageNum(t.input) ~= nil or usageNum(t.output) ~= nil
    or usageNum(t.total) ~= nil) then
    -- Подытог известных попыток: показывает текстовые токены; расход
    -- изображений считается отдельно и в эту сумму не входит.
    table.insert(parts, "текстовые токены (известные попытки): "
      .. tostring(math.floor(usageNum(t.total)
        or ((usageNum(t.input) or 0) + (usageNum(t.output) or 0)))))
  else
    table.insert(parts, "текстовые токены: неизвестно")
  end
  local img = imageUsageShort(u.latest)
  if img == nil and type(u.recent) == "table" then
    for _, r in ipairs(u.recent) do
      img = imageUsageShort(r)
      if img ~= nil then
        break
      end
    end
  end
  table.insert(parts, img or "расход изображений: не фиксировался")
  return table.concat(parts, " · ")
end

local function usageDetailLines(u)
  local out = {}
  local function line(key, text, color)
    table.insert(out, ui.label({ key = key, text = text, fontSize = 10,
      maxLines = 3, color = color or "on_surface_variant" }))
  end
  line("sum", tokensLine("текстовые токены — подытог известных попыток", u.tokens))
  line("rep", "попыток всего: " .. tostring(usageNum(u.attempts) or 0)
    .. " · с отчётом провайдера: " .. tostring(usageNum(u.reported_attempts) or 0)
    .. " · с неизвестным расходом: " .. tostring(usageNum(u.unknown_attempts) or 0))
  local latest = attemptLine("последняя попытка", u.latest)
  if latest ~= nil then
    line("latest", latest, "on_surface")
    local img = imageUsageShort(u.latest)
    if img ~= nil then
      line("latest-img", img)
    end
    if type(u.latest) == "table" and type(u.latest.text_tokens) == "table" then
      line("latest-tok", tokensLine("  текстовые токены последней", u.latest.text_tokens))
    end
    if type(u.latest) == "table" and type(u.latest.unknown_fields) == "table"
      and next(u.latest.unknown_fields) ~= nil then
      line("latest-unk", "нераспознанные поля последней: "
        .. tostring(noctalia.json.encode and noctalia.json.encode(u.latest.unknown_fields)
          or "(есть нераспознанные поля)"), "tertiary")
    end
  else
    line("latest-none", "последней попытки нет")
  end
  local recent = type(u.recent) == "table" and u.recent or {}
  if #recent > 0 then
    line("recent-head", "недавние попытки:", "on_surface")
    for n, r in ipairs(recent) do
      local ln = attemptLine("  #" .. tostring(n), r)
      if ln ~= nil then
        line("recent-" .. n, ln)
        local img = imageUsageShort(r)
        if img ~= nil then
          line("recent-img-" .. n, "  " .. img)
        end
      end
    end
  end
  line("note", "агрегаты — только по известным попыткам; «неизвестно» не равно нулю; стоимость и проценты квоты служба не сообщает",
    "on_surface_variant")
  return out
end

-- Компактный счётчик виден всегда, на всех трёх вкладках; раскрытие —
-- подробная техническая секция.
local function usageFooter()
  local u = type(status.generation_usage) == "table"
    and status.generation_usage or nil
  local kids = {
    ui.glyph({ name = "activity", size = 14, color = "on_surface_variant" }),
    ui.label({ key = "ucounter", text = usageCounterText(), fontSize = 11,
      color = "on_surface_variant", flexGrow = 1, maxLines = 2 }),
  }
  if u ~= nil then
    table.insert(kids, ui.button({
      key = "uopen",
      glyph = usageOpen and "chevron-up" or "chevron-down",
      controlSize = "sm", variant = "ghost",
      tooltip = "подробный расход генераций",
      onClick = function()
        usageOpen = not usageOpen
        render()
      end }))
  end
  local rows = { ui.row({ key = "ufoot", gap = 8, align = "center" }, kids) }
  if usageOpen and u ~= nil then
    table.insert(rows, ui.scroll({ key = "udetail", height = 150, gap = 2,
      padding = 8, radius = 8, fill = PLAIN }, usageDetailLines(u)))
  end
  return ui.column({ key = "usage", gap = 4, fill = "surface/0.35",
    radius = 8, padding = 6 }, rows)
end

-- ────────────────────────────────────────── вкладка «Обои»: сетка каталога

local function filtered()
  local q = noctalia.string.trim(string.lower(search))
  local want = FILTERS[filterIdx].id
  local out = {}
  for _, it in ipairs(items()) do
    local origin = it.origin == "import" and "import" or "workshop"
    if want == "all" or want == origin then
      if q == "" or string.find(string.lower(itemTitle(it)), q, 1, true)
        or string.find(string.lower(itemId(it)), q, 1, true) then
        table.insert(out, it)
      end
    end
  end
  return out
end

-- Постоянная панель управления над каталогом: видна и при пустом каталоге.
local function libraryControls()
  local children = {
    ui.row({ key = "searchrow", gap = 6, align = "center" }, {
      ui.input({ key = "search", value = search, flexGrow = 1,
        placeholder = "поиск по обоям…", controlSize = "sm",
        onSubmit = function(text) search = text or ""; render() end,
        onChange = function(text)
          search = text or ""
          render()                   -- фильтр сетки сразу, не по Enter
        end }),
      ui.button({ key = "import", text = "Импорт файла…", glyph = "upload",
        controlSize = "sm", variant = "outline", enabled = not busy(),
        tooltip = "выбрать MP4 или GIF — копия ляжет в управляемую библиотеку, исходник не трогаем",
        onClick = function() importDialog() end }),
      ui.button({ key = "scan", text = "Сканировать", glyph = "search",
        controlSize = "sm", variant = "ghost", enabled = not busy(),
        tooltip = "обзор каталога обоев — дёшево, без моделей",
        onClick = function() send({ action = "scan" }) end }),
      ui.button({ key = "analyze-all", text = "Разобрать новое", glyph = "brain",
        controlSize = "sm", variant = "ghost", enabled = not busy(),
        tooltip = "разбор только ещё не разобранных обоев — явный платный вызов модели, сам не запускается",
        onClick = function() send({ action = "analyze_all" }) end }),
    }),
    ui.row({ key = "filters", gap = 12, align = "center" }, {
      selectRow("filter", "показывать", {
        FILTERS[1].label, FILTERS[2].label, FILTERS[3].label }, filterIdx,
        function(i)
          filterIdx = i
          render()
        end),
      selectRow("monitor", "монитор", (function()
        if #connectedOutputs() == 0 then
          return { "нет выходов" }
        end
        return monitorOptions()
      end)(), (function()
        if #connectedOutputs() == 0 or selOutput == nil then
          return 1
        end
        for i, name in ipairs(monitorOptions()) do
          if name == selOutput then
            return i
          end
        end
        return 1
      end)(), function(i)
        local outs = monitorOptions()
        local pick = outs[i]
        -- i == 1 — literal ALL: сброс на «все подключённые».
        selOutput = pick ~= nil and pick ~= "ALL" and pick or nil
        render()
      end),
    }),
  }
  if importError ~= nil then
    table.insert(children, 2, ui.label({ key = "importerr", text = importError,
      fontSize = 11, color = "error", maxLines = 2 }))
  end
  return ui.column({ key = "controls", gap = 6 }, children)
end

-- Сетка 3 колонки: строки по три ячейки, вставкой, чтобы дырка из nil
-- посреди конструктора не ломала ipairs хоста.
local function wallpaperGrid()
  local list = filtered()
  if #list == 0 then
    return ui.column({ key = "empty", gap = 8, flexGrow = 1,
      align = "center", justify = "center" }, {
      ui.glyph({ name = "image", size = 28, color = "on_surface_variant" }),
      ui.label({ text = search ~= "" and "ничего не нашлось" or "каталог пуст",
        color = "on_surface_variant" }),
      ui.label({ key = "hint", fontSize = 11,
        text = "кнопка «Импорт файла…» добавляет MP4/GIF; «Сканировать» перечитывает каталог",
        color = "on_surface_variant" }),
    })
  end
  local playing = playingIds(chosenOutput())
  local rows = {}
  local row = nil
  for n, it in ipairs(list) do
    if (n - 1) % 3 == 0 then
      row = {}
      table.insert(rows, ui.row({
        key = "gridrow-" .. tostring(math.floor((n - 1) / 3)),
        gap = 8, align = "start" }, row))
    end
    local id = itemId(it)
    local chosen = id == selContent
    local prev = itemPreview(it)
    local thumb = type(prev) == "string" and prev ~= ""
      and ui.image({ key = "img", path = prev, width = THUMB_W, height = THUMB_H,
        radius = 6, fit = "cover",
        -- Превью обоев кликабельно: бэкенд сам разберётся, что открывать —
        -- картинку, GIF или видео (для сцен/веба — доступную галерею кадров).
        onClick = function()
          send({ action = "view", target = "wallpaper", content_id = id })
        end })
      or ui.column({ key = "noimg", width = THUMB_W, height = THUMB_H,
        align = "center", justify = "center",
        fill = "surface_variant/0.6", radius = 6 }, {
        ui.label({ text = "превью нет", fontSize = 10,
          color = "on_surface_variant" }),
      })
    table.insert(row, ui.column({
      key = "w-" .. id .. (chosen and "-on" or "-off"),
      gap = 2, padding = 6, radius = 8, flexGrow = 1,
      fill = chosen and PICKED or PLAIN,
    }, {
      thumb,
      ui.label({ key = "title", text = itemTitle(it), fontSize = 11,
        maxLines = 1, color = "on_surface" }),
      ui.row({ key = "marks", gap = 4, align = "center" }, (function()
        local marks = { ui.button({ key = "pick", text = chosen and "выбрано" or "Выбрать",
          glyph = chosen and "check" or "chevron-right",
          controlSize = "sm", variant = chosen and "primary" or "ghost",
          enabled = not chosen,
          onClick = function()
            selContent = id
            selVariant = nil
            ensureCapture(itemById(id))
            render()
          end }) }
        -- Вставкой, без nil-дырок посреди конструктора: пропущенная метка
        -- не должна смещать следующие.
        if playing[id] then
          table.insert(marks, ui.label({ key = "live", text = "играет",
            fontSize = 10, color = "primary" }))
        end
        if it.origin == "import" then
          table.insert(marks, ui.label({ key = "orig", text = "мой файл",
            fontSize = 10, color = "tertiary" }))
        end
        return marks
      end)()),
    }))
  end
  return ui.scroll({ key = "grid", gap = 8, padding = 4, flexGrow = 3 }, rows)
end

-- Детали выбранной обои: кадры, метаданные, применение.
-- Кадр кликабелен: открывает полноэкранную галерею захвата на этом кадре
-- (index нулевой, как ждёт заявка). Стрелки и выход — на стороне бэкенда.
local function frameCells(cap, contentId)
  local frames = (type(cap) == "table" and type(cap.frames) == "table")
    and cap.frames or {}
  if #frames == 0 then
    return ui.label({ key = "noframes", fontSize = 11,
      text = "кадров ещё нет — выберите обои, и служба снимет их сама",
      color = "on_surface_variant" })
  end
  local rows = {}
  local row = nil
  for n, f in ipairs(frames) do
    if (n - 1) % 3 == 0 then
      row = {}
      table.insert(rows, ui.row({
        key = "frow-" .. tostring(math.floor((n - 1) / 3)),
        gap = 6, align = "start" }, row))
    end
    local timeS = tonumber(f.time_s)
    local frameIdx = n - 1            -- наружу индекс с нуля
    table.insert(row, ui.column({
      key = "f-" .. tostring(n), gap = 1, flexGrow = 1 }, {
      ui.image({ key = "img", path = tostring(f.file or ""), width = 100,
        height = 60, radius = 4, fit = "contain",
        onClick = function()
          send({ action = "view", target = "capture",
            content_id = contentId, index = frameIdx })
        end }),
      ui.label({ key = "t", text = timeS ~= nil
        and (string.format("%.1f", timeS) .. " с") or "?", fontSize = 9,
        color = "on_surface_variant" }),
    }))
  end
  return ui.column({ key = "frames", gap = 4 }, {
    ui.column({ key = "fgrid", gap = 4 }, rows),
    ui.label({ key = "fhint", fontSize = 9,
      text = "клик по кадру откроет галерею на весь экран",
      color = "on_surface_variant" }),
  })
end

-- Метаданные разбора: точные поля конверта анализатора
-- {schema_version, content_sha256, profile, created_at, model, evidence,
-- visual} и его visual {summary, tags, objects, materials, style, vibe,
-- temperature, colors[{hex,role,weight}], composition,
-- motion{level,description}, confidence, uncertainties}. Никаких
-- выдуманных алиасов. Технические строки доказательства — отдельным
-- блоком по явному раскрытию.
local ROLE = { dominant = "доминирующий", secondary = "вторичный",
  accent = "акцентный" }
local MOTLVL = { static = "статика", low = "слабое", medium = "среднее",
  high = "сильное", unknown = "неизвестно" }

local function listLine(v, key)
  local vals = v[key]
  if type(vals) ~= "table" or #vals == 0 then
    return nil
  end
  local parts = {}
  for _, s in ipairs(vals) do
    if type(s) == "string" and s ~= "" then
      table.insert(parts, s)
    end
  end
  return #parts > 0 and table.concat(parts, ", ") or nil
end

local function colorsLine(v)
  local colors = v.colors
  if type(colors) ~= "table" or #colors == 0 then
    return nil
  end
  local parts = {}
  for _, c in ipairs(colors) do
    if type(c) == "table" and type(c.hex) == "string" then
      local piece = c.hex
      if type(c.role) == "string" and c.role ~= "" then
        piece = piece .. " (" .. (ROLE[c.role] or c.role)
          .. (type(c.weight) == "number"
            and string.format(", %d%%", math.floor(c.weight * 100 + 0.5)) or "")
          .. ")"
      end
      table.insert(parts, piece)
    end
  end
  return #parts > 0 and table.concat(parts, " · ") or nil
end

local function motionLine(v)
  local m = v.motion
  if type(m) ~= "table" then
    return nil
  end
  local lvl = type(m.level) == "string" and (MOTLVL[m.level] or m.level) or nil
  local desc = type(m.description) == "string" and m.description ~= ""
    and m.description or nil
  if lvl ~= nil and desc ~= nil then
    return lvl .. " — " .. desc
  end
  return lvl or desc
end

local function visualLines(v)
  if type(v) ~= "table" then
    return {}
  end
  local out = {}
  local function line(key, label, value)
    if value ~= nil and value ~= "" then
      table.insert(out, { key = key, text = label .. ": " .. tostring(value) })
    end
  end
  line("summary", "описание", type(v.summary) == "string" and v.summary or nil)
  line("objects", "объекты", listLine(v, "objects"))
  line("materials", "материалы", listLine(v, "materials"))
  line("style", "стиль", listLine(v, "style"))
  line("vibe", "настроение", listLine(v, "vibe"))
  line("tags", "теги", listLine(v, "tags"))
  line("temperature", "температура",
    type(v.temperature) == "string" and v.temperature ~= "" and v.temperature or nil)
  line("colors", "палитра", colorsLine(v))
  line("composition", "композиция",
    type(v.composition) == "string" and v.composition or nil)
  line("motion", "движение", motionLine(v))
  if type(v.confidence) == "number" then
    line("confidence", "уверенность",
      string.format("%d%%", math.floor(v.confidence * 100 + 0.5)))
  end
  local unc = listLine(v, "uncertainties")
  if unc ~= nil then
    table.insert(out, { key = "uncertainties", text = "неуверенность: " .. unc,
      color = "tertiary" })
  end
  return out
end

-- Технические строки доказательства: вид, моменты кадров с хешами,
-- наблюдаемость движения между кадрами.
local function evidenceLines(analysis)
  local ev = analysis.evidence
  if type(ev) ~= "table" then
    return {}
  end
  local out = {}
  local function line(key, text)
    table.insert(out, { key = key, text = text, color = "on_surface_variant" })
  end
  line("kind", "доказательство: " .. tostring(ev.kind or "?"))
  line("observable", "движение между кадрами: "
    .. (ev.motion_observable == true and "наблюдается" or "не наблюдается"))
  local frames = type(ev.frames) == "table" and ev.frames or {}
  for n, f in ipairs(frames) do
    local t = tonumber(f.time_s)
    local piece = string.format("кадр %d: %s с", n,
      t ~= nil and string.format("%.1f", t) or "?")
    if type(f.sha256) == "string" and #f.sha256 >= 8 then
      piece = piece .. " · sha256 " .. string.sub(f.sha256, 1, 8) .. "…"
    end
    line("frame-" .. n, piece)
  end
  return out
end

local function analysisLines(analysis)
  if type(analysis) ~= "table" then
    return {}
  end
  local out = visualLines(analysis.visual)
  local function line(key, label, value)
    if value ~= nil and value ~= "" then
      table.insert(out, { key = key, text = label .. ": " .. tostring(value) })
    end
  end
  line("model", "модель", type(analysis.model) == "string" and analysis.model or nil)
  line("created", "разобрано", analysis.created_at)
  line("profile", "профиль разбора", analysis.profile)
  return out
end

local function detailsPane()
  local it = selContent and itemById(selContent) or nil
  if it == nil then
    return ui.column({ key = "details", width = DETAILS_W, gap = 8, fill = PLAIN,
      radius = 8, padding = 10, align = "center", justify = "center" }, {
      ui.glyph({ name = "image", size = 24, color = "on_surface_variant" }),
      ui.label({ text = "выберите обои в сетке", color = "on_surface_variant" }),
    })
  end
  local id = itemId(it)
  local meta = META[(it.analysis_status or "missing")] or META.missing
  local capst = nil
  if it.capture ~= nil then
    capst = CAPST.ready
  elseif captureJobActive(id) then
    capst = CAPST.running
  end
  local playing = playingIds(nil)[id] == true
  local out = chosenOutput()
  local kindName = KINDR[it.kind] or tostring(it.kind or "?")

  local chips = {
    ui.label({ key = "kind", text = kindName, fontSize = 11,
      color = "on_surface_variant" }),
    ui.label({ key = "origin", text = ORIGIN[it.origin] or "Workshop",
      fontSize = 11, color = "on_surface_variant" }),
    chip("meta", meta),
  }
  -- Вставкой: пропущенный бейдж не оставляет nil-дырку посреди детей.
  if capst ~= nil then
    table.insert(chips, chip("cap", capst))
  end
  if playing then
    table.insert(chips, ui.label({ key = "live", text = "· играет", fontSize = 11,
      color = "primary" }))
  end

  local children = {
    ui.label({ key = "title", text = itemTitle(it), fontSize = 14,
      fontFamily = skin.FONT, fontWeight = "medium", flexGrow = 1, maxLines = 2 }),
    ui.row({ key = "chips", gap = 6, align = "center" }, chips),
  }

  table.insert(children, ui.column({ key = "framesblock", gap = 4 }, {
    ui.label({ key = "head", text = "Кадры захвата", fontSize = 12,
      fontWeight = "medium" }),
    frameCells(it.capture, id),
  }))

  local metaLines = analysisLines(it.analysis)
  local metaChildren = {
    ui.label({ key = "head", text = "Метаданные разбора", fontSize = 12,
      fontWeight = "medium" }),
    ui.row({ key = "analyze-action", gap = 6, align = "center" }, {
      ui.button({ key = "an", text = "Разобрать эти обои", glyph = "brain",
        controlSize = "sm", variant = "outline", flexGrow = 1,
        enabled = not busy(),
        tooltip = "разбор метаданных выбранных обоев — вызов vision-модели; кадры захватятся сами",
        onClick = function() send({ action = "analyze", content_id = id }) end }),
      ui.button({ key = "reset", glyph = "trash", controlSize = "sm",
        variant = "ghost", enabled = not resetBusy(),
        tooltip = "сбросить разбор, правки основы и варианты рамки этих обоев",
        onClick = function()
          resetConfirmId = resetConfirmId == id and nil or id
          render()
        end }),
    }),
  }
  if #metaLines == 0 then
    table.insert(metaChildren, ui.label({ key = "none", fontSize = 11,
      text = "метаданных пока нет — кнопка выше запускает платный разбор этих обоев",
      color = "on_surface_variant" }))
  else
    for n, ln in ipairs(metaLines) do
      table.insert(metaChildren, ui.label({ key = "ml-" .. n,
        text = ln.text, fontSize = 11, maxLines = 2,
        color = ln.color or "on_surface" }))
    end
  end
  if #metaLines > 0 then
    -- Техническое доказательство — по явному раскрытию: панель и так тесная.
    table.insert(metaChildren, ui.button({ key = "evbtn",
      text = evidenceOpen and "Скрыть технические данные"
        or "Технические данные доказательства",
      glyph = evidenceOpen and "chevron-up" or "chevron-down",
      controlSize = "sm", variant = "ghost",
      onClick = function()
        evidenceOpen = not evidenceOpen
        render()
      end }))
    if evidenceOpen then
      for n, ln in ipairs(evidenceLines(it.analysis)) do
        table.insert(metaChildren, ui.label({ key = "ev-" .. n,
          text = ln.text, fontSize = 10, maxLines = 2, color = ln.color }))
      end
    end
  end
  table.insert(children, ui.column({ key = "metablock", gap = 3 }, metaChildren))

  -- Именное подтверждение сброса: сначала называем обои и что именно
  -- исчезнет, только потом шлём reset с confirm=true.
  if resetConfirmId == id then
    table.insert(children, ui.column({ key = "resetconfirm", gap = 4,
      fill = "error/0.12", radius = 8, padding = 8 }, {
      ui.label({ key = "head", fontSize = 12, fontWeight = "medium",
        color = "error", maxLines = 2,
        text = "Сбросить данные обоев «" .. itemTitle(it) .. "»?" }),
      ui.label({ key = "warn", fontSize = 10, color = "on_surface",
        maxLines = 8,
        text = "Удалятся разбор, правки основы и все варианты рамки этих обоев. Если их рамка сейчас активна, она отключится. Файл обоев, снятые кадры, общие предпочтения и данные других обоев сохранятся. Перед сбросом создаётся резервная копия." }),
      ui.row({ key = "row", gap = 6, align = "center" }, {
        ui.button({ key = "do", text = "Сбросить эти обои", glyph = "trash",
          controlSize = "sm", variant = "destructive", enabled = not resetBusy(),
          onClick = function()
            if resetBusy() then return end
            resetConfirmId = nil
            send({ action = "reset", content_id = id, confirm = true })
          end }),
        ui.button({ key = "no", text = "Отмена", controlSize = "sm",
          variant = "ghost",
          onClick = function()
            resetConfirmId = nil
            render()
          end }),
      }),
    }))
  end

  local actionKids = {
    ui.button({ key = "apply", text = "Включить", glyph = "play",
      controlSize = "sm", variant = "primary",
      enabled = out ~= nil and not busy(),
      tooltip = out == "ALL"
        and "поставить эти обои на все подключённые мониторы"
        or out ~= nil
        and ("поставить эти обои на " .. out)
        or "нет известных выходов — снимок ещё не пришёл",
      onClick = function()
        -- Один запрос, один слот состояния: ALL несёт массив целей,
        -- конкретный монитор — своё имя.
        if out == "ALL" then
          send({ action = "wallpaper_apply", content_id = id,
            output = "ALL", outputs = connectedOutputs() })
        else
          send({ action = "wallpaper_apply", content_id = id, output = out })
        end
      end }),
    ui.button({ key = "stop", text = "Выключить", glyph = "stop",
      controlSize = "sm", variant = "ghost",
      enabled = out ~= nil and not busy(),
      tooltip = out == "ALL" and "выключить на всех подключённых мониторах"
        or out ~= nil and ("освободить " .. out) or "нет известных выходов",
      onClick = function()
        if out == "ALL" then
          send({ action = "wallpaper_stop",
            output = "ALL", outputs = connectedOutputs() })
        else
          send({ action = "wallpaper_stop", output = out })
        end
      end }),
  }
  table.insert(children, ui.row({ key = "actions", gap = 6, align = "center" },
    actionKids))
  table.insert(children, ui.label({ key = "cost", fontSize = 10,
    text = "«Включить/Выключить» управляют плеером на выбранном мониторе (ALL — на всех подключённых); «Разобрать эти обои» — платный вызов модели; сброс (иконка рядом) удаляет только данные этих обоев",
    color = "on_surface_variant" }))

  return ui.scroll({ key = "details", width = DETAILS_W, gap = 8, fill = PLAIN,
    radius = 8, padding = 10 }, children)
end

local function wallpapersTab()
  return ui.column({ key = "wallpapers", gap = 8, flexGrow = 1 }, {
    libraryControls(),
    ui.row({ key = "lib", gap = 8, flexGrow = 1, align = "start" }, {
      wallpaperGrid(),
      detailsPane(),
    }),
  })
end

-- ─────────────────────────────── вкладка «Оформление»: профиль, рамка окон

-- Профиль выбранного контента: если показанный вариант ссылается на свой
-- profile_id — берём его; иначе последний по created_at (ничья — по id,
-- чтобы выбор был детерминированным) профиль этой обои.
local function profileFor(contentId, variantProfileId)
  local s = snap()
  local profiles = (s and type(s.design_profiles) == "table") and s.design_profiles or {}
  if variantProfileId ~= nil then
    local p = profiles[tostring(variantProfileId)]
    if type(p) == "table" then
      return p
    end
  end
  local best = nil
  local bestKey = nil
  for id, rec in pairs(profiles) do
    if type(rec) == "table" and tostring(rec.content_id) == contentId then
      local prov = type(rec.provenance) == "table" and rec.provenance or {}
      local key = tostring(prov.created_at or "") .. "\1" .. tostring(id)
      if best == nil or key > bestKey then
        best = rec
        bestKey = key
      end
    end
  end
  return best
end

-- Провенанс — реальные поля записи: {model, created_at, analysis_profile,
-- evidence_kind, wallpaper{title,…}}. Никаких source/at.
local function provenanceText(prov)
  local parts = {}
  if type(prov.model) == "string" and prov.model ~= "" then
    table.insert(parts, "модель " .. prov.model)
  end
  if type(prov.analysis_profile) == "string" and prov.analysis_profile ~= "" then
    table.insert(parts, "профиль разбора " .. prov.analysis_profile)
  end
  if type(prov.evidence_kind) == "string" and prov.evidence_kind ~= "" then
    table.insert(parts, "доказательство " .. prov.evidence_kind)
  end
  local wp = type(prov.wallpaper) == "table" and prov.wallpaper or {}
  if type(wp.title) == "string" and wp.title ~= "" then
    table.insert(parts, "обои «" .. wp.title .. "»")
  end
  if type(prov.created_at) == "string" and prov.created_at ~= "" then
    table.insert(parts, prov.created_at)
  end
  return #parts > 0 and table.concat(parts, " · ") or nil
end

local function profileBlock(it, shownVariant)
  -- Профиль показанного варианта, если он ссылается на общий профиль;
  -- иначе детерминированный последний профиль этой обои.
  local variantPid = nil
  if type(shownVariant) == "table"
    and type(shownVariant.provenance) == "table" then
    variantPid = shownVariant.provenance.profile_id
  end
  local prof = profileFor(itemId(it), variantPid)
  local children = {
    ui.label({ key = "head", text = "Общий профиль оформления", fontSize = 12,
      fontWeight = "medium" }),
  }
  if type(prof) ~= "table" then
    table.insert(children, ui.label({ key = "none", fontSize = 11,
      text = "профиля ещё нет — он появится после разбора или генерации",
      color = "on_surface_variant" }))
  else
    local prov = type(prof.provenance) == "table" and prof.provenance or {}
    local provText = provenanceText(prov)
    table.insert(children, ui.label({ key = "pid", fontSize = 11,
      text = "профиль " .. tostring(prof.id or "?"), color = "on_surface" }))
    table.insert(children, ui.label({ key = "pprov", fontSize = 10,
      text = provText ~= nil and ("происхождение: " .. provText)
        or "происхождение не записано",
      color = "on_surface_variant" }))
  end
  return ui.column({ key = "profile", gap = 3 }, children)
end

local function variantRows(contentId)
  local variants = variantsOf(contentId)
  if #variants == 0 then
    return ui.label({ key = "novar", text = "вариантов пока нет",
      fontSize = 11, color = "on_surface_variant" })
  end
  local mapped = mappedVariant(contentId)
  local rows = {}
  for _, v in ipairs(variants) do
    local st = VSTATE[v.state or "candidate"] or VSTATE.candidate
    local shown = v.id == selVariant
    local buttons = {
      ui.button({ key = "show", text = shown and "…" or "показать",
        controlSize = "sm", variant = "ghost",
        onClick = function()
          -- Явный toggle: `x and nil or v.id` из nil истинность не
          -- восстанавливает и снять показ не может.
          if shown then
            selVariant = nil
          else
            selVariant = v.id
          end
          render()
        end }),
    }
    local kids = {
      ui.row({ key = "vrow", gap = 8, align = "center" }, {
        ui.label({ key = "name", text = tostring(v.name or v.id), fontSize = 12,
          flexGrow = 1, maxLines = 1 }),
        chip("st", st),
        mapped and mapped.id == v.id
          and ui.label({ key = "live", text = "одобренная замена", fontSize = 10,
            color = "secondary" })
          or appliedVariantId(contentId) == v.id
            and ui.label({ key = "live", text = "последняя применённая", fontSize = 10,
              color = "primary" })
            or nil,
      }),
    }
    -- Превью показанного варианта — ДО кнопок одобрения/применения. Живое
    -- окно показывается коротким статусом; до открытия остаётся статичный
    -- макет. Окно открывает только явная кнопка — раскрытие ничего не
    -- запускает.
    if shown then
      local pnode = previewNode(v)
      if pnode ~= nil then
        table.insert(kids, pnode)
      end
      local pj = previewJobFor(v.id)
      local live = type(v.render_preview) == "table"
        and v.render_preview.kind == "live"
      local opening = type(pj) == "table"
        and (pj.state == "queued" or pj.state == "running")
      if type(pj) == "table" then
        local txt, col = "живое превью: в очереди", "on_surface_variant"
        if pj.state == "running" then
          txt, col = "живое превью: открываем окно…", "primary"
        elseif live then
          txt, col = "живое превью: окно открыто", "primary"
        elseif pj.state == "succeeded" then
          txt = "прошлое окно закрыто — можно открыть заново"
        elseif pj.state == "cancelled" or pj.state == "interrupted" then
          txt = "открытие превью остановлено"
        elseif pj.state == "failed" then
          txt = "живое превью: ошибка"
          if type(pj.error) == "string" and pj.error ~= "" then
            txt = txt .. " — " .. pj.error
          end
          col = "error"
        end
        table.insert(kids, ui.label({ key = "pjob", text = txt, fontSize = 10,
          maxLines = 2, color = col }))
      end
      if not opening and not live then
        table.insert(kids, ui.button({ key = "popen",
          text = "Открыть живое превью",
          glyph = "external-link", controlSize = "sm", variant = "ghost",
          enabled = not busy(),
          tooltip = "открыть (или обновить) одно плавающее окно рамки с этим кандидатом; без записи и вызова модели",
          onClick = function()
            send({ action = "preview", variant_id = v.id })
            panel.close()
          end }))
      end
      if type(pnode) ~= "table" and type(pj) ~= "table" then
        table.insert(kids, ui.label({ key = "pnone", fontSize = 10,
          text = "живого окна ещё нет — кнопка выше откроет его",
          color = "on_surface_variant" }))
      end
      -- Художественный бриф выбранного варианта: общий тезис/материалы/
      -- перевод мотивов и рамка. Нет записи — говорим прямо, старые пакеты
      -- её не сохраняли.
      local ad = artDirectionFor(v)
      if ad ~= nil then
        local bkids = { ui.label({ key = "adhead", text = "Художественный бриф",
          fontSize = 11, fontWeight = "medium" }) }
        for n, ln in ipairs(briefLines(ad)) do
          table.insert(bkids, ui.label({ key = "ad-" .. tostring(n),
            text = ln.text, fontSize = 10, maxLines = 3, color = ln.color }))
        end
        table.insert(kids, ui.column({ key = "adbrief", gap = 2, fill = PLAIN,
          radius = 6, padding = 6 }, bkids))
      else
        table.insert(kids, ui.label({ key = "adnone", fontSize = 10,
          maxLines = 2, color = "on_surface_variant",
          text = "бриф этого варианта не записан — старые пакеты его не сохраняли" }))
      end
    end
    -- Причина решения: собственный черновик на вариант. Буфер ui.input
    -- переживает опросы службы и повторный показ; чистится только после
    -- подтверждения записи отзыва (design_feedback/last_action).
    local rev = reasonRev[v.id] or 0
    table.insert(kids, ui.column({ key = "reason", gap = 2 }, {
      ui.label({ key = "rlabel", text = "причина решения (до 2000 знаков)",
        fontSize = 10, color = "on_surface_variant" }),
      ui.input({ key = "reason-" .. v.id .. "-" .. tostring(rev),
        value = reasonDrafts[v.id] or "",
        placeholder = "почему одобряю или отклоняю — необязательно",
        controlSize = "sm", flexGrow = 1,
        onChange = function(text)
          reasonDrafts[v.id] = text or ""
          render()
        end }),
    }))
    local saved = feedbackFor(v.id)
    if #saved > 0 then
      local rec = saved[#saved]
      local vd = VERDICT[rec.verdict]
        or { tostring(rec.verdict or "?"), "on_surface_variant" }
      local rtext = type(rec.reason) == "string" and rec.reason or ""
      table.insert(kids, ui.label({ key = "rsaved", fontSize = 10, maxLines = 3,
        text = "записан отзыв: " .. vd[1]
          .. (rtext ~= "" and (" — " .. rtext) or ""),
        color = vd[2] }))
    end
    if v.state ~= "approved" then
      table.insert(buttons, ui.button({ key = "ok", text = "Одобрить",
        controlSize = "sm", variant = "ghost",
        onClick = function()
          local reason = reasonDrafts[v.id] or ""
          reasonPending[v.id] = { verdict = "approved", reason = reason,
            after = #feedbackList() }
          send({ action = "approve", variant_id = v.id, reason = reason })
        end }))
    end
    if v.state ~= "rejected" then
      table.insert(buttons, ui.button({ key = "no", text = "Отклонить",
        controlSize = "sm", variant = "ghost",
        onClick = function()
          local reason = reasonDrafts[v.id] or ""
          reasonPending[v.id] = { verdict = "rejected", reason = reason,
            after = #feedbackList() }
          send({ action = "reject", variant_id = v.id, reason = reason })
        end }))
    end
    table.insert(buttons, ui.button({ key = "apply", text = "Применить",
      controlSize = "sm", variant = "outline",
      enabled = v.state == "approved",
      tooltip = v.state == "approved" and "переключить рамку на этот вариант"
        or "применять можно только одобренные",
      onClick = function() send({ action = "apply", variant_id = v.id }) end }))
    table.insert(kids, ui.row({ key = "vact", gap = 6, align = "center" }, buttons))
    table.insert(kids, ui.label({ key = "motion", text = "цель: "
      .. targetLabel(v.target or "frame")
      .. " · движение: "
      .. (v.motion == "candles" and "пресет «свечи»" or "статика")
      .. " · провенанс записан в варианте",
      fontSize = 10, color = "on_surface_variant" }))
    table.insert(rows, ui.column({
      key = "v-" .. v.id .. (shown and "-on" or "-off"),
      gap = 2, padding = 6, radius = 6,
      fill = shown and PICKED or PLAIN,
    }, kids))
  end
  return ui.column({ key = "variants", gap = 4 }, rows)
end

-- Секция «Основа оформления этих обоев»: четыре поля из item.design_basis.
-- Грязное поле держит постоянный ключ (буфер ui.input не пересеивается),
-- чистое пересеивается только при фактической смене значения — поэтому
-- опросы службы, смена вкладок и повторный разбор не затирают ни правки,
-- ни набранный текст.
local function basisSection(it)
  local id = itemId(it)
  local db = type(it.design_basis) == "table" and it.design_basis or nil
  local kids = {
    ui.label({ key = "head", text = "Основа оформления этих обоев",
      fontSize = 12, fontWeight = "medium" }),
  }
  if db == nil or db.available ~= true then
    table.insert(kids, ui.label({ key = "none", fontSize = 11,
      text = "основы пока нет — она появится после «Разобрать эти обои»; поля можно заполнить и вручную",
      color = "on_surface_variant" }))
  else
    table.insert(kids, ui.label({ key = "src", fontSize = 10,
      text = "заполнено из сохранённого разбора этих обоев; правка становится переопределением и переживёт повторный разбор",
      color = "on_surface_variant" }))
  end
  basisInputs[id] = basisInputs[id] or {}
  for _, f in ipairs(BASIS_FIELDS) do
    local dirty = basisDirty[id] ~= nil and basisDirty[id][f] == true
    local value = dirty and (basisDrafts[id] and basisDrafts[id][f] or "")
      or basisValue(id, f)
    local input = basisInputs[id][f]
    if input == nil then
      input = { value = value, revision = 0 }
      basisInputs[id][f] = input
    elseif not dirty and input.value ~= value then
      input.value = value
      input.revision += 1
    end
    local key = "basis-" .. id .. "-" .. f .. "-" .. tostring(input.revision)
    table.insert(kids, field(key, BASIS_LABELS[f], value,
      "заполнится из разбора…", function(text)
        basisDrafts[id] = basisDrafts[id] or {}
        basisDirty[id] = basisDirty[id] or {}
        basisDrafts[id][f] = text
        basisDirty[id][f] = true
        input.value = text
      end))
  end
  return ui.column({ key = "basis", gap = 6 }, kids)
end

local function styleTab()
  local it = selContent and itemById(selContent) or nil
  if it == nil then
    return ui.column({ key = "nostyle", gap = 8, flexGrow = 1,
      align = "center", justify = "center" }, {
      ui.glyph({ name = "palette", size = 28, color = "on_surface_variant" }),
      ui.label({ text = "сначала выберите обои на вкладке «Обои»",
        color = "on_surface_variant" }),
      ui.label({ key = "hint", fontSize = 11,
        text = "оформление делается под конкретные обои", color = "on_surface_variant" }),
    })
  end

  local id = itemId(it)
  local noteText = notes[id] or ""
  local seed = prefsSeen and "s" or "0"

  -- Профиль: показанного варианта, если он ссылается на общий профиль,
  -- иначе детерминированный последний профиль этой обои.
  local shownVariant = selVariant and variantById(selVariant) or nil

  -- Общие предпочтения: черновики переживают перерисовки, сохранение —
  -- одной кнопкой одним вложенным патчем. Отдельно от основы обоев.
  local common = ui.column({ key = "common", gap = 10 }, {
    profileBlock(it, shownVariant),
    ui.label({ key = "prefhead", text = "Твои предпочтения", fontSize = 12,
      fontWeight = "medium" }),
    field("likes-" .. seed, "нравится (общее)", likes,
      "материалы и мотивы, которые нравятся", function(text)
        likes = text
        prefsDirty = true
      end),
    field("dislikes-" .. seed, "не нравится (общее)", dislikes,
      "чего избегать", function(text)
        dislikes = text
        prefsDirty = true
      end),
    field("common-notes-" .. seed, "общие заметки к оформлению", commonNotes,
      "общие требования для всех приложений", function(text)
        commonNotes = text or ""
        prefsDirty = true
      end),
    field("notes-" .. id .. "-" .. tostring(notesRev[id] or 0),
      "заметки к этой обои", noteText,
      "что поправить в следующем варианте…", function(text)
        notes[id] = text or ""
      end),
  })

  -- Техническая секция свёрнута: vision-модель, выход и авто-применение
  -- нужны редко, а место в панели дорого.
  local tech = nil
  if techOpen then
    tech = ui.column({ key = "tech", gap = 6, fill = PLAIN, radius = 8,
      padding = 8 }, {
      ui.label({ key = "head", text = "Технические настройки", fontSize = 12,
        fontWeight = "medium" }),
      field("vision-" .. seed, "модель разбора", visionModel,
        "селектор vision-модели", function(text)
          visionModel = text
          prefsDirty = true
        end),
      selectRow("output", "выход для рамок",
        outputOptions(), outputIdx,
        function(i)
          outputIdx = i
          prefsDirty = true
          render()
        end),
      toggleRow("auto", "автоматически применять одобренные рамки", autoApply,
        function()
          autoApply = not autoApply
          prefsDirty = true
          render()
        end),
    })
  end

  local kitty = ui.column({ key = "kitty", gap = 6 }, {
    ui.label({ key = "head", text = "Рамка окон: под обои", fontSize = 12,
      fontWeight = "medium" }),
    ui.row({ key = "kittyrow", gap = 12, align = "start" }, {
      selectRow("detail", "детализация",
        { DETAIL[1].label, DETAIL[2].label, DETAIL[3].label }, detailIdx,
        function(i)
          detailIdx = i
          prefsDirty = true
          render()
        end),
      selectRow("thick", "толщина",
        { THICK[1].label, THICK[2].label, THICK[3].label }, thickIdx,
        function(i)
          thickIdx = i
          prefsDirty = true
          render()
        end),
      selectRow("motion", "движение",
        { MOTION[1].label, MOTION[2].label }, motionIdx,
        function(i)
          motionIdx = i
          prefsDirty = true
          render()
        end),
    }),
  })

  -- Цель и режим оформления. Цель — продукт (рамка окон сейчас, браузерный
  -- интерфейс пока недоступен), режим — общий художественный язык. Это
  -- отдельно от preferences.kitty: там настройки отрисовки, их не трогаем.
  local targetOptions = {}
  local targetIdx = 1
  for i, t in ipairs(designTargets()) do
    table.insert(targetOptions, tostring(t.label or t.id))
    if tostring(t.id) == tostring(selTarget) then
      targetIdx = i
    end
  end
  local targetKids = {
    ui.label({ key = "head", text = "Цель и режим оформления", fontSize = 12,
      fontWeight = "medium" }),
    ui.row({ key = "trow", gap = 12, align = "start" }, {
      selectRow("target", "цель оформления", targetOptions, targetIdx,
        function(i)
          local t = designTargets()[i]
          selTarget = t ~= nil and tostring(t.id) or "frame"
          render()
        end),
      selectRow("mode", "режим",
        { DESIGN_MODE[1].label, DESIGN_MODE[2].label }, modeIdx,
        function(i)
          modeIdx = i
          prefsDirty = true
          render()
        end),
    }),
  }
  if not targetAvailable(selTarget) then
    table.insert(targetKids, ui.label({ key = "treason", fontSize = 10,
      maxLines = 2, color = "tertiary",
      text = "Цель «" .. targetLabel(selTarget) .. "» недоступна: "
        .. (targetReason(selTarget) ~= "" and targetReason(selTarget)
          or "причина не сообщена") }))
  end
  local targetBlock = ui.column({ key = "design", gap = 6, fill = PLAIN,
    radius = 8, padding = 8 }, targetKids)

  local unsaved = prefsDirty or basisDirtyAny(id)
  local ready = prefsSeen and not unsaved
  -- Дети собираются вставкой: tech свёрнута, и nil-дырка посреди
  -- конструктора сделала бы длину таблицы неопределённой.
  local kids = {
    basisSection(it),
    common,
    ui.row({ key = "techtoggle", gap = 8, align = "center" }, {
      ui.button({ key = "techbtn", text = techOpen and "Скрыть технические"
        or "Технические настройки", glyph = techOpen and "chevron-up" or "chevron-down",
        controlSize = "sm", variant = "ghost",
        onClick = function()
          techOpen = not techOpen
          render()
        end }),
    }),
  }
  if tech ~= nil then
    table.insert(kids, tech)
  end
  table.insert(kids, targetBlock)
  table.insert(kids, kitty)
  table.insert(kids, ui.row({ key = "prefsave", gap = 8, align = "center" }, (function()
    local saveKids = {
      ui.button({ key = "save", text = "Сохранить профиль", glyph = "check",
        controlSize = "sm", variant = "outline", enabled = not busy(),
        tooltip = "черновики уходят в профиль одной заявкой, вложенной формой",
        onClick = function() requestPrefs() end }),
    }
    if unsaved then
      table.insert(saveKids, ui.label({ key = "dirty", text = "есть несохранённые правки",
        fontSize = 10, color = "tertiary" }))
    end
    return saveKids
  end)()))
  table.insert(kids, ui.column({ key = "gen", gap = 6 }, {
      ui.row({ key = "genrow", gap = 8, align = "center" }, {
        ui.button({ key = "gen", text = "Сгенерировать вариант", glyph = "sparkles",
          controlSize = "sm", variant = "primary",
          enabled = not busy() and ready and targetAvailable(selTarget),
          tooltip = unsaved and "Сначала сохраните профиль"
            or (not targetAvailable(selTarget))
              and ("Цель «" .. targetLabel(selTarget) .. "» недоступна: "
                .. (targetReason(selTarget) ~= "" and targetReason(selTarget)
                  or "генерация не подключена"))
            or "вызов провайдера картинок — платное действие; кадры при нужде захватятся сами",
          onClick = function()
            -- Защита в самом действии: отрисованный enabled мог устареть.
            if busy() or prefsDirty or basisDirtyAny(id)
              or not targetAvailable(selTarget) then
              return
            end
            send({ action = "generate", content_id = id,
              notes = notes[id] or "", target = selTarget })
          end }),
        ui.button({ key = "another", text = "Ещё вариант", glyph = "copy",
          controlSize = "sm", variant = "ghost",
          enabled = not busy() and ready and targetAvailable(selTarget),
          tooltip = unsaved and "Сначала сохраните профиль"
            or (not targetAvailable(selTarget))
              and ("Цель «" .. targetLabel(selTarget) .. "» недоступна: "
                .. (targetReason(selTarget) ~= "" and targetReason(selTarget)
                  or "генерация не подключена"))
            or "новая генерация без заметок — платное действие",
          onClick = function()
            if busy() or prefsDirty or basisDirtyAny(id)
              or not targetAvailable(selTarget) then
              return
            end
            send({ action = "generate", content_id = id, notes = "",
              target = selTarget })
          end }),
      }),
      ui.label({ key = "cost", fontSize = 10,
        text = "генерация и «ещё вариант» — новые обращения к провайдеру картинок; цель «браузер» станет доступна позже; отмена — на вкладке «Очередь»",
        color = "on_surface_variant" }),
    }))
  table.insert(kids, ui.column({ key = "variantsblock", gap = 4 }, {
    ui.label({ key = "varhead", text = "Варианты", fontSize = 12,
      fontWeight = "medium" }),
    variantRows(id),
  }))
  return ui.scroll({ key = "style", gap = 10, flexGrow = 1 }, kids)
end

-- ───────────────────────────────────────────────────── вкладка «Очередь»

-- Итог пакетной задачи: реальные счётчики и по-элементные исходы из
-- job.result (results:[{id,title,status,error?}], scan_errors).
local function resultLines(job)
  local res = job.result
  if type(res) ~= "table" then
    return {}
  end
  local out = {}
  local parts = {}
  for _, name in ipairs({ "total", "analyzed", "cache_hit", "preview", "needs_capture", "failed", "skipped" }) do
    local n = tonumber(res[name]) or 0
    if n > 0 then
      table.insert(parts, name .. " " .. n)
    end
  end
  if #parts > 0 then
    table.insert(out, { text = "итог: " .. table.concat(parts, " · "),
      color = "on_surface_variant" })
  end
  for _, r in ipairs((type(res.results) == "table" and res.results) or {}) do
    if type(r) == "table" then
      local line = tostring(r.title or r.id or "?") .. " — " .. tostring(r.status or "?")
      if type(r.error) == "string" and r.error ~= "" then
        line = line .. " · " .. r.error
      end
      table.insert(out, { text = line,
        color = r.status == "needs_capture" and "tertiary"
          or r.status == "error" and "error" or "on_surface_variant" })
    end
  end
  for _, e in ipairs((type(res.scan_errors) == "table" and res.scan_errors) or {}) do
    if type(e) == "table" then
      table.insert(out, { text = "ошибка обзора: " .. tostring(e.path or e.id or "?")
        .. " · " .. tostring(e.message or ""), color = "error" })
    end
  end
  return out
end

local function queueTab()
  local s = snap()
  local jobs = (s and type(s.jobs) == "table") and s.jobs or {}
  if #jobs == 0 then
    return ui.column({ key = "nojobs", gap = 8, flexGrow = 1,
      align = "center", justify = "center" }, {
      ui.glyph({ name = "history", size = 28, color = "on_surface_variant" }),
      ui.label({ text = "задач нет", color = "on_surface_variant" }),
      ui.label({ key = "hint", fontSize = 11,
        text = "обзор, захват и импорт — дёшево; разбор и генерация — платные вызовы моделей",
        color = "on_surface_variant" }),
    })
  end
  local rows = {}
  for i = #jobs, 1, -1 do           -- свежие сверху
    local job = jobs[i]
    local st = JOB[job.state or "queued"] or JOB.queued
    local active = job.state == "queued" or job.state == "running"
    local retryable = job.state == "failed" or job.state == "interrupted"
      or job.state == "cancelled" or job.state == "needs_capture"
    local payload = type(job.payload) == "table" and job.payload or {}
    local title = (KIND[job.kind] or job.kind or "задача")
      .. (job.content_id and (" · " .. tostring(job.content_id)) or "")
      .. (payload.output and (" · " .. tostring(payload.output)) or "")
      .. (job.kind == "import" and type(payload.path) == "string"
        and (" · " .. payload.path) or "")
    local stage = tostring(job.stage or "")
    local line = stage ~= "" and ("этап: " .. (STAGE[stage] or stage)) or ""
    if type(job.error) == "string" and job.error ~= "" then
      line = line ~= "" and (line .. " · " .. job.error) or job.error
    end
    local cells = {
      ui.label({ key = "t", text = title, fontSize = 12, flexGrow = 1, maxLines = 1 }),
      chip("st", st),
    }
    -- Дети собираются вставкой: дырка из nil посреди конструктора делает
    -- длину таблицы неопределённой, и хост по ipairs потерял бы кнопку.
    if active then
      table.insert(cells, ui.button({ key = "cancel", text = "Отменить", glyph = "x",
        controlSize = "sm", variant = "ghost",
        onClick = function() send({ action = "cancel", job_id = job.id }) end }))
    end
    if retryable then
      table.insert(cells, ui.button({ key = "retry", text = "Повторить", glyph = "refresh",
        controlSize = "sm", variant = "ghost",
        tooltip = "новая попытка",
        onClick = function() send({ action = "retry", job_id = job.id }) end }))
    end
    local body = {
      ui.row({ key = "jr", gap = 8, align = "center" }, cells),
    }
    if line ~= "" then
      table.insert(body, ui.label({ key = "l", text = line, fontSize = 10,
        maxLines = 2, color = job.state == "failed" and "error" or "on_surface_variant" }))
    end
    if type(job.progress) == "string" and job.progress ~= "" then
      table.insert(body, ui.label({ key = "p", text = job.progress, fontSize = 10,
        maxLines = 1, color = "on_surface_variant" }))
    end
    for n, rl in ipairs(resultLines(job)) do
      table.insert(body, ui.label({ key = "res-" .. n, text = rl.text,
        fontSize = 10, maxLines = 2, color = rl.color }))
    end
    table.insert(rows, ui.column({
      key = "j-" .. tostring(job.id) .. "-" .. job.state,
      gap = 2, padding = 6, radius = 6,
      fill = active and PICKED or PLAIN,
    }, body))
  end
  return ui.scroll({ key = "jobs", gap = 4, flexGrow = 1 }, rows)
end

-- ───────────────────────────────────────────────────────────── отрисовка

render = function()
  local head, err = header()
  local s = snap()
  local la = s and s.last_action or nil
  local feedback = nil
  if type(la) == "table" and type(la.message) == "string" and la.message ~= "" then
    feedback = ui.label({ key = "action", text = (la.ok == true and "Готово: " or "Ошибка: ")
      .. la.message, fontSize = 11, maxLines = 2,
      color = la.ok == true and "primary" or "error" })
  end
  local bodyTab = tab == "queue" and queueTab()
    or tab == "style" and styleTab()
    or wallpapersTab()
  local children = { tabbar(), bodyTab, usageFooter() }
  if feedback then table.insert(children, feedback) end
  if err ~= "" then
    table.insert(children, ui.label({ key = "err", text = err, fontSize = 11,
      color = "error", maxLines = 2 }))
  end
  local kids = { head, table.unpack(children) }
  local body = ui.column({ key = "root", flexGrow = 1, gap = 8,
    paddingH = 6, paddingV = 6 }, kids)

  -- Рамка скина: 12px со всех сторон (суммарно 24px внутренних издержек),
  -- blossom-углы и рейки рисует общий модуль desktop_skin.
  panel.render(skin.frame(body, "studio-frame"))
end

-- Черновики настроек засеиваем из снимка один раз: поле ui.input
-- пересеивать нельзя — собьёшь набираемый текст.
local function seedPrefs()
  if prefsSeen then
    return
  end
  local s = snap()
  local p = (s and type(s.preferences) == "table") and s.preferences or nil
  if p == nil then
    return
  end
  local kitty = type(p.kitty) == "table" and p.kitty or {}
  detailIdx = selId(DETAIL, kitty.detail or "balanced")
  thickIdx = selId(THICK, kitty.thickness or "normal")
  motionIdx = selId(MOTION, kitty.motion or "static")
  modeIdx = selId(DESIGN_MODE, type(p.design_mode) == "string" and p.design_mode
    or "quality")
  likes = tostring(p.likes or "")
  dislikes = tostring(p.dislikes or "")
  commonNotes = tostring(p.notes or "")
  visionModel = tostring(p.vision_model or "")
  autoApply = p.auto_apply == true
  local outs = outputOptions()
  local want = tostring(p.output or "")
  outputIdx = 1
  for i, name in ipairs(outs) do
    if name == want and want ~= "" then
      outputIdx = i
    end
  end
  prefsSeen = true
end

function onOpen(_context)
  status = noctalia.state.get(KEY_STATUS) or {}
  ackBasis()
  ackFeedback()
  consumeReset()
  seedPrefs()
  render()
  poke("refresh")
end

function onClose()
  -- Черновики живут в панельных переменных; очередь и снимки — у службы.
  tab = "wallpapers"
end

noctalia.state.watch(KEY_STATUS, function(value)
  status = value or {}
  ackBasis()
  ackFeedback()
  consumeReset()
  seedPrefs()
  local s = snap()
  if prefsSeen and s and type(s.preferences) == "table" then
    local want = {
      likes = likes,
      dislikes = dislikes,
      notes = commonNotes,
      vision_model = visionModel,
      output = outputIdx > 1 and outputOptions()[outputIdx] or "",
      auto_apply = autoApply == true,
      design_mode = DESIGN_MODE[modeIdx].id,
      kitty = {
        detail = DETAIL[detailIdx].id,
        thickness = THICK[thickIdx].id,
        motion = MOTION[motionIdx].id,
      },
    }
    local matches = true
    for key, wanted in pairs(want) do
      if key == "kitty" then
        for kk, kv in pairs(wanted) do
          if type(s.preferences.kitty) ~= "table"
            or s.preferences.kitty[kk] ~= kv then
            matches = false
            break
          end
        end
      elseif s.preferences[key] ~= wanted then
        matches = false
        break
      end
    end
    prefsDirty = not matches
  end
  render()
end)
