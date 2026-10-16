--!nonstrict
-- Панель выбора Noctalia-скина (q/wallpaper-studio:skins).
--
-- Тонкий клиент поверх общего сервиса q/wallpaper-studio:watch: состояние
-- приходит из studio.status, действия уходят через studio.request с тем же
-- nonce/FIFO, что и у основной панели. Своего сервиса и бэкенда здесь нет.
--
-- Контракт снимка (согласован с SkinStudioQueue/presentation helper,
-- 2026-10-04). На верхнем уровне snapshot.noctalia и у
-- snapshot.items[i].noctalia одинаково:
--   { schema_version, content_id, available, reason,
--     supported_families:[str],
--     families:[{id,label,available,reason,topologies:[str]}],
--     mappings:{content_id→variant_id}, active_variant_id,
--     last_applied:{variant_id,content_id,family,at}|null,
--     seed_error:str|null,
--     surface_catalog:{schema_version,surfaces:[{id,name,kind,plugin_id,
--       entry_id,type,persistent,runtime_support}],
--       nonvisual:[{id,name,kind,plugin_id,entry_id}]}|null,
--     inventory_digest:str|null, image_calls_per_variant:int|null,
--     generation_available:bool, generation_unavailable_reason:str,
--     jobs:{active:[{batch_id,content_id,count,ordinals,state,stage,
--       payload:{target,batch_id,ordinal,count,family,
--                parent_variant_id?,refinement_scope?,surface_id?}}],
--           queued,running,failed,cancelled} }
-- variants:[{variant_id,name,family,topology,colors,density,preview_path,
--   preview_kind,state,builtin,source,created_at,available,
--   unavailable_reason,installed,active,parent_variant_id,batch_id,revision,
--   presentation:{schema_version,complete,coverage_current,legacy,
--     scene:{surface_id,name,kind,preview_path,sha256,preview_kind,
--            provenance,runtime_support}|null, surfaces:[то же],
--     nonvisual:[...], inventory_digest, apply_ready, unavailable_reason,
--     runtime_verified:false}}]
-- Отсутствие snapshot.noctalia → все направления недоступны, честная
-- надпись; никаких выдуманных ярлыков и процентов. runtime_verified всегда
-- false (реальной authority нет) — витрина пишет «runtime unverified».
-- Семейство (family) НЕ означает применимость целого пакета: поддержка
-- семейства и покрытие/готовность пакета показываются раздельно.
--
-- Действия — только существующие заявки:
--   {action="generate", target="noctalia", content_id, count=1..4, family=?,
--    refinement_scope="whole"|"surface", surface_id=?|null,
--    inventory_digest, image_calls, parent_variant_id=?, notes=""}
--     g — серия пакетов (whole), r — уточнение whole/surface.
--     image_calls — вызовов на пакет из живой сметы (whole 1+N, surface 2);
--     digest и число замораживаются при открытии модала и уходят как есть.
--   {action="apply"|"approve", variant_id, reason?}             a — подтвердить
--   {action="cancel", job_id|batch_id, ...}                     отмена задач
--   Навигация — ТОЛЬКО фокус: ни одна строка не применяет и не тратит
--   моделей сама. Каждый расход — явное подтверждение Enter.
--
-- Карта действий (source-observed паттерны tmus/wallpaper-studio; статусы —
-- source-observed до рантайм-проверки владельцем):
--   h/l, ←/→       вариант в ленте, автоповтор
--   j/k, ↓/↑       сцена → компоненты выбранного варианта, автоповтор
--   Tab            витрина всего скина ↔ список обоев (тумглер)
--   Ctrl+j/k       секции Skins → Jobs → Refine (только browse; о/л)
--   /              режим поиска (текст ведём сами; Esc — выход из режима)
--   Enter          крупный выбранный кадр с подписями (обои — выбор)
--   a              применить полный пакет или явный recipe-only legacy
--   g              серия пакетов 1..4 (точная смета; j/k — семейство)
--   r              выбор рамки уточнения: весь пакет или компонент
--   c              отмена сфокусированной активной задачи
--   Esc            раскрутка: модал → детали → поиск → список → панель
-- Все буквы имеют физические двойники ЙЦУКЕН: j/k/h/l = о/л/р/д,
-- a/g/r/c = ф/п/к/с; Tab/слэш-позиция (в RU — «.») раскладке не меняются.
--
-- Родного ui.input здесь нет сознательно: захваченные клавиши до
-- сфокусированного поля не доходят (грабля tmus 18.09), поэтому текст
-- поиска и заметок панель ведёт сама в onKey.

local KEY_STATUS = "studio.status"
local KEY_REQUEST = "studio.request"
local SERVICE = "q/wallpaper-studio:watch"

local skin = require("./desktop_skin.luau")

-- ─────────────────────────────────────────────────────────── словари

local FAMR = {
  ["stagecraft"] = "Сценография",
  ["cut-stone"] = "Резной камень",
  ["wayfinding-field"] = "Маршруты",
  ["adaptive-workbench"] = "Верстак",
  ["kumihimo"] = "Кумихимо",
}

-- Порядок выбора семейства для серии. Показ всех сохраняется;
-- генерация доступна только там, где нативная поддержка реально объявлена
-- (familyAvailable), и семейство НЕ означает применимость целого пакета.
local FAMILY_IDS = { "stagecraft", "cut-stone", "wayfinding-field",
  "adaptive-workbench", "kumihimo" }

-- Метки runtime_support компонентов пакета.
local RUNTIME_LABEL = {
  implemented = "реализовано нативно",
  preserved = "сохранено как есть",
  ["concept-only"] = "LLM-концепт",
}

-- Метки типа компонента (kind из каталога поверхности).
local KIND_LABEL = {
  scene = "сцена",
  bar = "бар",
  ["bar-widget"] = "виджет бара",
  ["desktop-widget"] = "виджет рабочего стола",
  ["lockscreen-widget"] = "виджет экрана блокировки",
  panel = "панель",
  ["plugin-widget"] = "виджет плагина",
  ["plugin-desktop-widget"] = "виджет плагина на столе",
}

-- Метки невизуальных записей (без изображений).
local NONVISUAL_LABEL = {
  service = "служба",
  shortcut = "ярлык",
  ["launcher-provider"] = "провайдер запуска",
}

-- Витрина: сцена + до 128 поверхностей. Рисуем ограниченное окно вокруг
-- выбранной строки, чтобы фокус j/k всегда оставался видимым, без опоры
-- на автоскролл хоста.
local SURF_WINDOW = 4
local STRIP_WINDOW = 5

local SECTION_IDS = { "skins", "jobs", "refine" }
local SECTION_LABELS = { skins = "Скины", jobs = "Задачи", refine = "Уточнение" }

local STATE_CHIP = {
  candidate = { "кандидат", "tertiary" },
  approved = { "одобрен", "primary" },
  rejected = { "отклонён", "error" },
}

local PLAIN = "surface_variant/0"
local PICKED = "primary/0.14"

local PREVIEW_H = 262            -- высота сцены выбранного (сценография)
local STRIP_THUMB_W, STRIP_THUMB_H = 150, 84
local ROW_THUMB_W, ROW_THUMB_H = 96, 54

-- ───────────────────────────────────────────────────────────── состояние

local status = noctalia.state.get(KEY_STATUS) or {}
local sectionIdx = 1            -- skins | jobs | refine
local view = "variants"         -- в секции Skins: variants | wallpapers
local selContent = nil          -- content_id выбранных обоев
local focusVariant = nil        -- variant_id в фокусе витрины
local selSurfaceId = "scene"    -- surface_id выбранного компонента; "scene" — сцена
local focusWall = 1             -- индекс в списке обоев
local focusJob = 1              -- индекс в списке задач
-- browse | detail | apply | count | notes | refine (выбор рамки уточнения)
local mode = "browse"
local searchMode = false        -- режим ввода поиска (только в browse)
local refineScope = "whole"     -- whole | surface (выбор в refine-модале)

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

local search = ""
local notesBuf = ""             -- черновик заметок уточнения
local countN = 1                -- подтверждаемый размер серии (1..4)
local countFamily = nil         -- семейство серии (фиксируется при открытии)
local countQuote = nil          -- оценка стоимости, замороженная при открытии g
local refineQuote = nil         -- оценка стоимости, замороженная при открытии r
local refineSurfaceId = nil     -- surface_id цели уточнения (nil для whole)

local render                    -- вперёд: обработчики зовут её раньше определения

-- ──────────────────────────────────────────────────────────── служебное

local function snap()
  return type(status.snapshot) == "table" and status.snapshot or nil
end

local function busy()
  return status.requestBusy == true or status.syncBusy == true
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

-- ────────────────────────────────────────────────── noctalia-данные снимка

local function noctaliaTop()
  local s = snap()
  local n = s and s.noctalia
  return type(n) == "table" and n or nil
end

-- Каталог выбранных обоев: snapshot.items[i].noctalia, иначе верхний
-- noctalia, если он про те же обои. Отсутствие — честный nil.
local function catalogFor(contentId)
  for _, it in ipairs(items()) do
    if itemId(it) == contentId then
      local n = it.noctalia
      if type(n) == "table" and type(n.variants) == "table" then
        return n
      end
    end
  end
  local top = noctaliaTop()
  if type(top) == "table" and type(top.variants) == "table"
    and (top.content_id == nil or top.content_id == contentId) then
    return top
  end
  return nil
end

-- Доступность семейства: только явные supported_families нативного хоста.
-- Нет снимка/списка — недоступно всё, никаких догадок.
local function familyAvailable(family)
  local top = noctaliaTop()
  if top == nil then
    return false, "бэкенд без поддержки noctalia"
  end
  if type(top.supported_families) == "table" then
    for _, f in ipairs(top.supported_families) do
      if f == family then
        return true, ""
      end
    end
  end
  -- Явная строка families[] даёт причину — покажем её, не выдумывая.
  if type(top.families) == "table" then
    for _, rec in ipairs(top.families) do
      if type(rec) == "table" and rec.id == family then
        if rec.available == true then
          return true, ""
        end
        local r = type(rec.reason) == "string" and rec.reason or ""
        return false, r ~= "" and r or "семейство недоступно нативно"
      end
    end
  end
  return false, "семейство недоступно нативно"
end

-- Нормализация варианта в одну форму независимо от источника строки
-- (каталог CatalogApply или строка snapshot.variants от GenerationQueue).
local function normVariant(v, contentId)
  if type(v) ~= "table" then
    return nil
  end
  local id = tostring(v.variant_id or v.id or "")
  if id == "" then
    return nil
  end
  local family = tostring(v.family or "")
  local avail = v.available
  local reason = ""
  if avail == nil then
    avail, reason = familyAvailable(family)
  else
    avail = avail == true
    if not avail then
      reason = tostring(v.unavailable_reason or v.readiness or "")
      if reason == "" then
        _, reason = familyAvailable(family)
      end
    end
  end
  return {
    id = id,
    content_id = contentId,
    name = tostring(v.name or "") ~= "" and tostring(v.name) or id,
    family = family,
    topology = tostring(v.topology or ""),
    density = tostring(v.density or ""),
    colors = type(v.colors) == "table" and v.colors or nil,
    preview = type(v.preview_path) == "string" and v.preview_path ~= ""
      and v.preview_path or nil,
    state = tostring(v.state or "candidate"),
    builtin = v.builtin == true,
    source = tostring(v.source or ""),
    installed = v.installed == true,
    parent = v.parent_variant_id and tostring(v.parent_variant_id) or nil,
    batch = v.batch_id and tostring(v.batch_id) or nil,
    revision = tonumber(v.revision),
    mapped = v.mapped == true,
    available = avail,
    reason = reason,
    active = v.active == true,
    created_at = tonumber(v.created_at),
    presentation = type(v.presentation) == "table" and v.presentation or nil,
    raw = v,
  }
end

-- Варианты выбранных обоев: сначала каталог, иначе строки снимка с
-- target="noctalia". Порядок устойчивый: по времени, потом по имени.
local function variantsFor(contentId)
  local out = {}
  local cat = catalogFor(contentId)
  if cat ~= nil then
    for _, v in ipairs(cat.variants) do
      local n = normVariant(v, contentId)
      if n ~= nil then
        table.insert(out, n)
      end
    end
  else
    local s = snap()
    for _, v in ipairs((s and s.variants) or {}) do
      if type(v) == "table" and v.target == "noctalia"
        and tostring(v.content_id or "") == tostring(contentId) then
        local n = normVariant(v, contentId)
        if n ~= nil then
          table.insert(out, n)
        end
      end
    end
  end
  table.sort(out, function(a, b)
    local ta, tb = a.created_at or 0, b.created_at or 0
    if ta ~= tb then
      return ta < tb
    end
    return a.name < b.name
  end)
  return out
end

-- Активный вариант обоев — только живое нативное чтение, спроецированное
-- бэкендом (контракт F): active_variant_id == нативный рецепт ЭТИХ обоев;
-- mapped_variant_id — сохранённое одобрение, применённым оно не является.
-- active_known == false — статус неизвестен, показываем это честно.
local function activeInfo(contentId)
  local cat = catalogFor(contentId)
  local known = true
  local err = nil
  local mapped = nil
  if cat ~= nil then
    mapped = cat.mapped_variant_id ~= nil
      and tostring(cat.mapped_variant_id) or nil
    if cat.active_known == false then
      known = false
      err = type(cat.active_error) == "string" and cat.active_error
        and cat.active_error ~= "" and cat.active_error or nil
    elseif type(cat.active_variant_id) == "string"
      and cat.active_variant_id ~= "" then
      return { known = true, variant_id = tostring(cat.active_variant_id),
        mapped = mapped }
    end
  end
  -- Фолбэк: per-variant флаг active от бэкенда.
  for _, v in ipairs(variantsFor(contentId)) do
    if v.active then
      return { known = true, variant_id = v.id, mapped = mapped }
    end
  end
  if not known then
    return { known = false, error = err, mapped = mapped }
  end
  return { known = true, variant_id = nil, mapped = mapped }
end

-- ─────────────────────────────────────── презентация пакета и смета

-- presentation_view варианта: полный пакет «сцена + компоненты» или legacy
-- с честной оговоркой. Отсутствие/битая форма — не падение, а причина.
local function presOf(v)
  if type(v) ~= "table" then
    return nil
  end
  local p = v.presentation
  if type(p) ~= "table" then
    return nil
  end
  return p
end

-- Живая смета генерации: только явные поля каталога; отсутствие — честный
-- nil, без догадок о числе вызовов.
local function quoteOf(contentId)
  local cat = catalogFor(contentId)
  if cat == nil then
    return nil
  end
  local per = tonumber(cat.image_calls_per_variant)
  return {
    digest = type(cat.inventory_digest) == "string"
      and cat.inventory_digest or nil,
    perVariant = per,
    available = cat.generation_available == true,
    reason = tostring(cat.generation_unavailable_reason or ""),
    catalog = type(cat.surface_catalog) == "table"
      and cat.surface_catalog or nil,
  }
end

-- Строки витрины: индекс 1 — сцена, затем компоненты манифеста. Для
-- не-legacy берём ТОЛЬКО фактические пути презентации: отсутствующее
-- превью не подменяется сценой. Legacy явно остаётся одним кадром.
local function surfaceRows(v)
  local out = {}
  if v == nil then
    return out
  end
  local pres = presOf(v)
  if pres ~= nil and pres.legacy ~= true then
    local sc = pres.scene
    table.insert(out, {
      surface_id = "scene",
      name = "Сцена",
      kind = "scene",
      preview_path = type(sc) == "table"
        and type(sc.preview_path) == "string" and sc.preview_path ~= ""
        and sc.preview_path or nil,
      runtime_support = type(sc) == "table"
        and tostring(sc.runtime_support or "") or "",
      scene = true,
    })
    for _, s in ipairs(type(pres.surfaces) == "table" and pres.surfaces or {}) do
      if type(s) == "table" then
        table.insert(out, {
          surface_id = tostring(s.surface_id or ""),
          name = tostring(s.name or ""),
          kind = tostring(s.kind or ""),
          preview_path = type(s.preview_path) == "string"
            and s.preview_path ~= "" and s.preview_path or nil,
          runtime_support = tostring(s.runtime_support or "concept-only"),
          scene = false,
        })
      end
    end
  else
    -- Legacy (или презентации нет): единственный фактический кадр варианта.
    table.insert(out, {
      surface_id = "scene",
      name = "Сцена",
      kind = "scene",
      preview_path = v.preview,
      runtime_support = "",
      scene = true,
      legacy = true,
    })
  end
  return out
end

local function surfaceRowById(rows, id)
  for _, r in ipairs(rows) do
    if r.surface_id == id then
      return r
    end
  end
  return nil
end

local function selectedSurfaceRow(v)
  local rows = surfaceRows(v)
  return surfaceRowById(rows, selSurfaceId) or rows[1]
end

-- Службы/ярлыки/провайдеры — без изображений. Источник: презентация
-- выбранного варианта, иначе живой каталог поверхностей.
local function nonvisualRows(contentId, v)
  local pres = presOf(v)
  if pres ~= nil and type(pres.nonvisual) == "table" then
    return pres.nonvisual
  end
  local q = quoteOf(contentId)
  if q ~= nil and q.catalog ~= nil
    and type(q.catalog.nonvisual) == "table" then
    return q.catalog.nonvisual
  end
  return {}
end

-- Честное состояние покрытия/runtime пакета.
local function coverageInfo(v)
  local pres = presOf(v)
  if pres == nil or pres.legacy == true then
    return {
      state = "legacy",
      label = "устаревший каталог: одиночный legacy-предпросмотр",
      reason = pres ~= nil and tostring(pres.unavailable_reason or "") or "",
    }
  end
  local reason = tostring(pres.unavailable_reason or "")
  if pres.complete == true then
    if pres.apply_ready == true then
      return { state = "full", label = "полное покрытие", reason = reason }
    end
    return { state = "partial",
      label = "полное покрытие · не готово к полному применению",
      reason = reason }
  end
  return { state = "incomplete", label = "неполное покрытие", reason = reason }
end

local function runtimeLabel(support)
  return RUNTIME_LABEL[support] or (support ~= "" and support or "LLM-концепт")
end

local function kindLabel(kind)
  return KIND_LABEL[kind] or (kind ~= "" and kind or "компонент")
end

-- ──────────────────────────────────────────────────────────── задачи

-- Noctalia-задачи: сначала сводка снимка (партии), иначе — строки задач
-- с payload.target == "noctalia". Никаких выдуманных процентов хода.
local function noctaliaJobs()
  local out = {}
  local s = snap()
  local top = noctaliaTop()
  if s ~= nil then
    for _, job in ipairs((type(s.jobs) == "table" and s.jobs) or {}) do
      local p = type(job) == "table" and type(job.payload) == "table"
        and job.payload or nil
      if type(job) == "table" and (p ~= nil and p.target == "noctalia"
        or job.target == "noctalia") then
        table.insert(out, {
          id = job.id and tostring(job.id) or nil,
          batch = (p and p.batch_id or job.batch_id) and
            tostring(p and p.batch_id or job.batch_id) or nil,
          state = tostring(job.state or ""),
          stage = tostring(job.stage or ""),
          error = type(job.error) == "string" and job.error or "",
          content_id = job.content_id and tostring(job.content_id) or nil,
          ordinal = p and tonumber(p.ordinal) or nil,
          count = p and tonumber(p.count) or nil,
          family = p and tostring(p.family or "") or "",
        })
      end
    end
  end
  if #out == 0 and type(top) == "table" and type(top.jobs) == "table"
    and type(top.jobs.active) == "table" then
    for _, j in ipairs(top.jobs.active) do
      if type(j) == "table" then
        table.insert(out, {
          id = nil,
          batch = j.batch_id and tostring(j.batch_id) or nil,
          state = tostring(j.state or ""),
          stage = tostring(j.stage or ""),
          error = "",
          content_id = j.content_id and tostring(j.content_id) or nil,
          count = tonumber(j.count),
        })
      end
    end
  end
  return out
end

local function jobsSummaryLine()
  local top = noctaliaTop()
  local j = type(top) == "table" and top.jobs or nil
  if type(j) ~= "table" then
    return "сводка задач недоступна"
  end
  local function num(k)
    return tonumber(j[k]) or 0
  end
  local parts = {}
  if num("queued") > 0 then
    table.insert(parts, "в очереди " .. num("queued"))
  end
  if num("running") > 0 then
    table.insert(parts, "идёт " .. num("running"))
  end
  if num("failed") > 0 then
    table.insert(parts, "ошибки " .. num("failed"))
  end
  if num("cancelled") > 0 then
    table.insert(parts, "отменено " .. num("cancelled"))
  end
  return #parts > 0 and table.concat(parts, " · ") or "задач нет"
end

-- ─────────────────────────────────────────────────── фокус и доступность

local function galleryRows()
  if selContent == nil then
    return {}
  end
  local q = search ~= "" and string.lower(search) or nil
  local out = {}
  for _, v in ipairs(variantsFor(selContent)) do
    if q == nil or string.find(string.lower(v.name), q, 1, true)
      or string.find(string.lower(v.family), q, 1, true) then
      table.insert(out, v)
    end
  end
  return out
end

local function wallRows()
  local q = search ~= "" and string.lower(search) or nil
  local out = {}
  for _, it in ipairs(items()) do
    if q == nil or string.find(string.lower(itemTitle(it)), q, 1, true)
      or string.find(string.lower(itemId(it)), q, 1, true) then
      table.insert(out, it)
    end
  end
  return out
end

local function clampFocus()
  if view == "wallpapers" then
    local n = #wallRows()
    focusWall = math.max(1, math.min(focusWall, math.max(1, n)))
    return
  end
  local rows = galleryRows()
  if #rows == 0 then
    focusVariant = nil
    selSurfaceId = "scene"
    return
  end
  local found
  for i, v in ipairs(rows) do
    if v.id == focusVariant then
      found = i
    end
  end
  local i = found or 1
  i = math.max(1, math.min(i, #rows))
  focusVariant = rows[i].id
  -- surface_id сохраняется, пока существует у выбранного варианта;
  -- иначе фокус сбрасывается на сцену.
  if surfaceRowById(surfaceRows(rows[i]), selSurfaceId) == nil then
    selSurfaceId = "scene"
  end
end

local function focusedVariant()
  for _, v in ipairs(galleryRows()) do
    if v.id == focusVariant then
      return v
    end
  end
  return nil
end

local function focusedJob()
  local jobs = noctaliaJobs()
  if #jobs == 0 then
    return nil
  end
  focusJob = math.max(1, math.min(focusJob, #jobs))
  return jobs[focusJob]
end

-- Выбранные обои: явный выбор, иначе первые из каталога.
local function effectiveContent()
  if selContent ~= nil and itemById(selContent) ~= nil then
    return selContent
  end
  local list = items()
  return list[1] ~= nil and itemId(list[1]) or nil
end

-- Окно видимых строк вокруг выбранного: фокус j/k всегда в окне.
local function windowRange(n, sel, size)
  if n <= size then
    return 1, n
  end
  local half = math.floor(size / 2)
  local start = sel - half
  if start < 1 then
    start = 1
  end
  local stop = start + size - 1
  if stop > n then
    stop = n
    start = stop - size + 1
  end
  return start, stop
end

-- h/l — вариант в ленте; surface_id переносится, если существует.
local function moveVariantSel(d)
  local rows = galleryRows()
  local n = #rows
  if n == 0 then
    return
  end
  local cur = 1
  for i, v in ipairs(rows) do
    if v.id == focusVariant then
      cur = i
    end
  end
  local nxt = ((cur - 1 + d) % n) + 1
  focusVariant = rows[nxt].id
  if surfaceRowById(surfaceRows(rows[nxt]), selSurfaceId) == nil then
    selSurfaceId = "scene"
  end
  render()
end

-- j/k — сцена/компоненты выбранного варианта.
local function moveSurfaceSel(d)
  local rows = surfaceRows(focusedVariant())
  local n = #rows
  if n == 0 then
    return
  end
  local cur = 1
  for i, r in ipairs(rows) do
    if r.surface_id == selSurfaceId then
      cur = i
    end
  end
  local nxt = ((cur - 1 + d) % n) + 1
  selSurfaceId = rows[nxt].surface_id
  render()
end

-- Горизонталь (dh) — вариант, вертикаль (dv) — сцена/компонент.
local function moveFocus(dh, dv)
  if view == "wallpapers" then
    local n = #wallRows()
    if n > 0 then
      focusWall = ((focusWall - 1 + dv) % n) + 1
    end
    render()
    return
  end
  if dh ~= nil and dh ~= 0 then
    moveVariantSel(dh)
  end
  if dv ~= nil and dv ~= 0 then
    moveSurfaceSel(dv)
  end
end

-- ─────────────────────────────────────────────── текстовый ввод вручную

local function charCount(str)
  local n = utf8.len(str)
  return (n == nil or n < 0) and 0 or n
end

local function byteOffset(str, cp)
  if cp <= 0 then
    return 1
  end
  local len = charCount(str)
  if cp >= len then
    return #str + 1
  end
  return utf8.offset(str, cp + 1)
end

local function insertAt(str, cp, ch)
  local b = byteOffset(str, cp)
  return string.sub(str, 1, b - 1) .. ch .. string.sub(str, b), cp + 1
end

local function deleteBefore(str, cp)
  if cp <= 0 then
    return str, cp
  end
  local b = byteOffset(str, cp)
  local pb = byteOffset(str, cp - 1)
  return string.sub(str, 1, pb - 1) .. string.sub(str, b), cp - 1
end

-- Клавиатура: латиница приходит одним аккордом в верхнем регистре,
-- кириллица — именами кейсимов (таблица снята xkbcli, ср. tmus).
local TEXT_CHARS = {
  A = "a", B = "b", C = "c", D = "d", E = "e", F = "f", G = "g", H = "h",
  I = "i", J = "j", K = "k", L = "l", M = "m", N = "n", O = "o", P = "p",
  Q = "q", R = "r", S = "s", T = "t", U = "u", V = "v", W = "w", X = "x",
  Y = "y", Z = "z",
  ["0"] = "0", ["1"] = "1", ["2"] = "2", ["3"] = "3", ["4"] = "4",
  ["5"] = "5", ["6"] = "6", ["7"] = "7", ["8"] = "8", ["9"] = "9",
  minus = "-", comma = ",", apostrophe = "'", period = ".",
  Cyrillic_a = "а", Cyrillic_be = "б", Cyrillic_ve = "в", Cyrillic_ghe = "г",
  Cyrillic_de = "д", Cyrillic_ie = "е", Cyrillic_io = "ё", Cyrillic_zhe = "ж",
  Cyrillic_ze = "з", Cyrillic_i = "и", Cyrillic_shorti = "й",
  Cyrillic_ka = "к", Cyrillic_el = "л", Cyrillic_em = "м", Cyrillic_en = "н",
  Cyrillic_o = "о", Cyrillic_pe = "п", Cyrillic_er = "р", Cyrillic_es = "с",
  Cyrillic_te = "т", Cyrillic_u = "у", Cyrillic_ef = "ф", Cyrillic_ha = "х",
  Cyrillic_tse = "ц", Cyrillic_che = "ч", Cyrillic_sha = "ш",
  Cyrillic_shcha = "щ", Cyrillic_hardsign = "ъ", Cyrillic_yeru = "ы",
  Cyrillic_softsign = "ь", Cyrillic_e = "э", Cyrillic_yu = "ю",
  Cyrillic_ya = "я", Cyrillic_A = "А", Cyrillic_BE = "Б", Cyrillic_VE = "В",
  Cyrillic_GHE = "Г", Cyrillic_DE = "Д", Cyrillic_IE = "Е", Cyrillic_IO = "Ё",
  Cyrillic_ZHE = "Ж", Cyrillic_ZE = "З", Cyrillic_I = "И",
  Cyrillic_SHORTI = "Й", Cyrillic_KA = "К", Cyrillic_EL = "Л",
  Cyrillic_EM = "М", Cyrillic_EN = "Н", Cyrillic_O = "О", Cyrillic_PE = "П",
  Cyrillic_ER = "Р", Cyrillic_ES = "С", Cyrillic_TE = "Т", Cyrillic_U = "У",
  Cyrillic_EF = "Ф", Cyrillic_HA = "Х", Cyrillic_TSE = "Ц",
  Cyrillic_CHE = "Ч", Cyrillic_SHA = "Ш", Cyrillic_SHCHA = "Щ",
  Cyrillic_HARDSIGN = "Ъ", Cyrillic_YERU = "Ы", Cyrillic_SOFTSIGN = "Ь",
  Cyrillic_E = "Э", Cyrillic_YU = "Ю", Cyrillic_YA = "Я",
}

local function textEdit(chord)
  -- Возвращает true, если хорда — часть текстового ввода.
  if chord == "Return" or chord == "Escape" or chord == "Tab"
    or chord == "Up" or chord == "Down" then
    return false
  end
  if chord == "Space" then
    return true, " "
  end
  if chord == "BackSpace" or chord == "Backspace" then
    return true, nil, "backspace"
  end
  local ch = TEXT_CHARS[chord]
  if ch then
    return true, ch
  end
  return false
end

-- ────────────────────────────────────────────────────── действия (заявки)

-- Применение возможно только для одобренного, доступного нативно и ещё
-- не активного варианта. Всё остальное — честно отключено.
local function canApply(v)
  if v == nil then
    return false, "вариант не выбран"
  end
  local ai = activeInfo(v.content_id)
  if not ai.known then
    return false, "статус применения неизвестен: "
      .. (ai.error or "нативный статус нечитаем")
  end
  if v.active or v.id == ai.variant_id then
    return false, "уже применён"
  end
  if v.state ~= "approved" and v.state ~= "candidate" then
    return false, "отклонённый вариант нельзя выбрать"
  end
  if not v.available then
    return false, v.reason ~= "" and v.reason or "недоступно нативно"
  end
  return true, ""
end


-- Полная применимость: полный пакет требует актуального готового каталога;
-- legacy остаётся recipe-only через прежний cap gate.
local function applyState(v)
  if v == nil then
    return { ok = false, why = "вариант не выбран", full = false,
      legacy = false }
  end
  local pres = presOf(v)
  if pres == nil or pres.legacy == true then
    local ok, why = canApply(v)
    return { ok = ok, why = why, full = false, legacy = true }
  end
  if pres.complete ~= true then
    local r = tostring(pres.unavailable_reason or "")
    return { ok = false, full = false, legacy = false,
      why = "полное покрытие не сформировано"
        .. (r ~= "" and (": " .. r) or "") }
  end
  if pres.coverage_current ~= true then
    return { ok = false, full = false, legacy = false,
      why = "каталог изменился: покрытие устарело" }
  end
  if pres.apply_ready ~= true then
    local r = tostring(pres.unavailable_reason or "")
    return { ok = false, full = false, legacy = false,
      why = r ~= "" and r or "есть компоненты без runtime-поддержки: "
        .. "полное применение запрещено" }
  end
  local ok, why = canApply(v)
  return { ok = ok, why = why, full = true, legacy = false }
end

local function doApply()
  local v = focusedVariant()
  local st = applyState(v)
  if st.ok then
    send({ action = v.state == "candidate" and "approve" or "apply",
      variant_id = v.id })
  end
  mode = "browse"
  render()
end

-- Уточнение компонента — только для актуального полного пакета и реального
-- компонента (не сцены).
local function refineSurfaceEligible(v)
  local pres = presOf(v)
  if pres == nil or pres.legacy == true then
    return false, "legacy: доступно только уточнение всего пакета"
  end
  if pres.complete ~= true then
    return false, "покрытие не сформировано: только уточнение всего пакета"
  end
  if pres.coverage_current ~= true then
    return false, "каталог изменился: только уточнение всего пакета"
  end
  local row = selectedSurfaceRow(v)
  if row == nil or row.scene then
    return false, "выбрана сцена: уточнение компонента недоступно"
  end
  return true, ""
end

local function doGenerateBatch()
  local contentId = effectiveContent()
  local q = countQuote
  if contentId == nil or q == nil or q.digest == nil
    or q.perVariant == nil or not q.available then
    mode = "browse"
    render()
    return
  end
  local family = countFamily or "stagecraft"
  if not familyAvailable(family) then
    mode = "browse"
    render()
    return
  end
  send({ action = "generate", target = "noctalia", content_id = contentId,
    count = countN, family = family, refinement_scope = "whole",
    inventory_digest = q.digest, image_calls = q.perVariant, notes = "" })
  mode = "browse"
  render()
end

local function doRefine()
  local v = focusedVariant()
  if v == nil then
    mode = "browse"
    render()
    return
  end
  if not familyAvailable(v.family) then
    mode = "browse"
    render()
    return
  end
  local q = refineQuote
  if q == nil or q.digest == nil or q.perVariant == nil then
    mode = "browse"
    render()
    return
  end
  local surfaceId = nil
  local calls = q.perVariant
  if refineScope == "surface" then
    local ok = refineSurfaceEligible(v)
    if not ok then
      mode = "browse"
      render()
      return
    end
    surfaceId = refineSurfaceId
    calls = 2
  end
  send({ action = "generate", target = "noctalia",
    content_id = v.content_id or effectiveContent(),
    count = 1, family = v.family, parent_variant_id = v.id,
    refinement_scope = refineScope, surface_id = surfaceId,
    inventory_digest = q.digest, image_calls = calls, notes = notesBuf })
  notesBuf = ""
  refineScope = "whole"
  refineSurfaceId = nil
  mode = "browse"
  render()
end

local function cancelFocusedJob()
  local job = focusedJob()
  if job == nil then
    return
  end
  if job.state ~= "queued" and job.state ~= "running" then
    return
  end
  if job.id ~= nil then
    -- Отмена одного ребёнка каскадом останавливает старших младших
    -- (контракт GenerationQueue: child отказывается стартовать, пока
    -- младший ординал той же партии не терминальный).
    send({ action = "cancel", job_id = job.id })
  elseif job.batch ~= nil then
    send({ action = "cancel", batch_id = job.batch })
  end
end

-- ──────────────────────────────────────────────────── удержание клавиш

-- Автоповтор навигации и правки текста: press → задержка → серия,
-- release/закрытие панели гасит (см. tmus onFrameTick/onKey).
local held = nil
local heldMs = 0
local heldFired = 0
local MAX_HOLD_MS = 6000
local NAV_DELAY_MS, NAV_REPEAT_MS = 350, 90
local EDIT_DELAY_MS, EDIT_REPEAT_MS = 300, 60

local function startHold(chord, action)
  if panel.setNeedsFrameTick == nil then
    return
  end
  held = { chord = chord, action = action, mode = mode, search = searchMode,
    section = sectionIdx, view = view }
  heldMs = 0
  heldFired = 0
  panel.setNeedsFrameTick(true)
end

local function endHold()
  held = nil
  if panel.setNeedsFrameTick ~= nil then
    panel.setNeedsFrameTick(false)
  end
end

local function holdOwnsMode()
  if held == nil or held.mode ~= mode or held.search ~= searchMode
    or held.section ~= sectionIdx or held.view ~= view then
    return false
  end
  if held.action == "backspace" then
    return searchMode or mode == "notes"
  end
  return mode == "browse" and not searchMode
end

local function runHeld()
  if not holdOwnsMode() then
    endHold()
    return false
  end
  local a = held.action
  if a == "down" then
    if sectionIdx == 3 then
      moveVariantSel(1)
    else
      moveFocus(0, 1)
    end
  elseif a == "up" then
    if sectionIdx == 3 then
      moveVariantSel(-1)
    else
      moveFocus(0, -1)
    end
  elseif a == "left" then
    moveFocus(-1, 0)
  elseif a == "right" then
    moveFocus(1, 0)
  elseif a == "job-down" then
    local n = #noctaliaJobs()
    if n > 0 then
      focusJob = focusJob % n + 1
      render()
    end
  elseif a == "job-up" then
    local n = #noctaliaJobs()
    if n > 0 then
      focusJob = (focusJob - 2) % n + 1
      render()
    end
  elseif a == "backspace" then
    if searchMode then
      search = deleteBefore(search, charCount(search))
      render()
      return true
    end
    if mode == "notes" then
      notesBuf = deleteBefore(notesBuf, charCount(notesBuf))
      render()
      return true
    end
    return false
  end
  return true
end

function onFrameTick(deltaMs)
  if not held then
    return
  end
  heldMs = heldMs + (deltaMs or 0)
  if heldMs >= MAX_HOLD_MS then
    endHold()
    return
  end
  local editing = held.action == "backspace"
  local delay = editing and EDIT_DELAY_MS or NAV_DELAY_MS
  local interval = editing and EDIT_REPEAT_MS or NAV_REPEAT_MS
  if heldMs < delay then
    return
  end
  local due = math.floor((heldMs - delay) / interval) + 1
  local owed = due - heldFired
  if owed <= 0 then
    return
  end
  heldFired = due
  for _ = 1, owed do
    if not runHeld() then
      endHold()
      break
    end
  end
end

-- ───────────────────────────────────────────────────────── отрисовка

local function chipLabel(text, pair)
  return ui.label({ key = "chip", text = "· " .. text,
    fontSize = 11, color = pair and pair[2] or "on_surface_variant" })
end

local function stateChip(state)
  local pair = STATE_CHIP[state]
  if pair == nil then
    return chipLabel(state, nil)
  end
  return chipLabel(pair[1], pair)
end

local function thumb(path, key, w, h, radius)
  if path ~= nil then
    local props = { key = key, path = path, height = h,
      radius = radius == nil and 6 or radius, fit = "contain" }
    if w ~= nil and w > 0 then
      props.width = w
    end
    return ui.image(props)
  end
  return ui.column({ key = key, width = w, height = h,
    align = "center", justify = "center", fill = PLAIN, radius = 6 }, {
    ui.glyph({ name = "image", size = 18, color = "on_surface_variant" }),
  })
end

-- ──────────────────────────────────────────────────────────────────────

-- Открытие модалов с заморозкой сметы: подтверждение всегда показывает и
-- отправляет числа из снимка, а не пересчитывает скрытно.
local function openCount()
  local v = focusedVariant()
  countFamily = (v ~= nil and v.family or nil) or countFamily or "stagecraft"
  countN = 1
  countQuote = quoteOf(effectiveContent())
  mode = "count"
  render()
end

local function openRefine()
  local v = focusedVariant()
  if v == nil then
    return
  end
  refineQuote = quoteOf(v.content_id or effectiveContent())
  refineScope = "whole"
  refineSurfaceId = nil
  notesBuf = ""
  mode = "refine"
  render()
end

-- Концепт: ряд явных действий под витриной. Кнопки повторяют те же
-- клавишные подтверждения — ни одна не действует без модала/деталей.
local function actionRow()
  local v = focusedVariant()
  local st = applyState(v)
  local q = quoteOf(effectiveContent())
  local genOk = q ~= nil and q.digest ~= nil and q.perVariant ~= nil
    and q.available
    and familyAvailable(countFamily or (v ~= nil and v.family or nil)
      or "stagecraft")
  local applyLabel = "Применить"
  if st.legacy then
    applyLabel = v ~= nil and v.state == "candidate"
      and "Одобрить (recipe-only)" or "Применить (recipe-only)"
  elseif v ~= nil and v.state == "candidate" then
    applyLabel = "Одобрить"
  end
  local applyTooltip = st.ok
    and (st.full and "применить полный пакет (a)"
      or "применить как recipe-only скин (a)")
    or ("недоступно: " .. st.why .. " (a)")
  local genTooltip = genOk
    and ("серия 1..4 пакетов — " .. (q.perVariant + 0) .. " вызов(а) на "
      .. "пакет, подтверждение в модале (g)")
    or ("серия недоступна: " .. ((q ~= nil and q.reason ~= "" and q.reason)
      or "нет живой сметы вызовов — обновите снимок") .. " (g)")
  local kids = {
    ui.button({ key = "details", text = "Детали", glyph = "search",
      controlSize = "sm", variant = "ghost", enabled = v ~= nil,
      tooltip = "крупно выбранный кадр с подписями (Enter)",
      onClick = function()
        if focusedVariant() ~= nil then
          mode = "detail"
          render()
        end
      end }),
    ui.button({ key = "apply", text = applyLabel, glyph = "check",
      controlSize = "sm", variant = "primary", enabled = st.ok,
      tooltip = applyTooltip,
      onClick = function()
        if applyState(focusedVariant()).ok then
          mode = "apply"
          render()
        end
      end }),
    ui.button({ key = "generate", text = "Серия пакетов…", glyph = "copy",
      controlSize = "sm", variant = "ghost", enabled = genOk,
      tooltip = genTooltip,
      onClick = openCount }),
    ui.button({ key = "refine", text = "Уточнить…", glyph = "brain",
      controlSize = "sm", variant = "ghost",
      enabled = v ~= nil and q ~= nil
        and q.digest ~= nil and q.perVariant ~= nil
        and familyAvailable(v.family),
      tooltip = "уточнение всего пакета или компонента; ничего не "
        .. "применяется и не тратится без подтверждения (r)",
      onClick = openRefine }),
  }
  return ui.row({ key = "actions", gap = 8, align = "center" }, kids)
end

-- Крупная сцена выбранного варианта + честная метка покрытия.
local function sceneBlock(v, rows)
  local scene = rows[1]
  local cov = coverageInfo(v)
  local ai = activeInfo(v.content_id)
  local col = cov.state == "full" and "primary"
    or cov.state == "legacy" and "on_surface_variant" or "tertiary"
  local head = {
    ui.label({ key = "fam", text = FAMR[v.family] or v.family, fontSize = 14,
      fontFamily = skin.FONT, fontWeight = "semibold" }),
    chipLabel(cov.label, { "", col }),
    ui.spacer({ flexGrow = 1 }),
    ui.label({ key = "ru", text = "runtime unverified", fontSize = 10,
      color = "on_surface_variant" }),
  }
  if v.mapped and (not ai.known or ai.variant_id ~= v.id) then
    table.insert(head, ui.label({ key = "mapped",
      text = "сохранён к применению", fontSize = 11, color = "primary" }))
  end
  local info = {
    ui.label({ key = "nm", text = v.name, fontSize = 14,
      fontFamily = skin.FONT, fontWeight = "semibold" }),
    stateChip(v.state),
  }
  if cov.reason ~= "" then
    table.insert(info, ui.label({ key = "cr", text = cov.reason, fontSize = 10,
      color = "on_surface_variant", maxLines = 2 }))
  end
  return ui.column({ key = "scene", gap = 4, fill = PLAIN, radius = 8,
    shape = skin.stagecraft() and "chamfer" or nil,
    border = skin.stagecraft() and skin.tokens().colors.outline or nil,
    borderWidth = skin.stagecraft() and 1 or nil,
    padding = 8 }, {
    ui.row({ key = "head", gap = 8, align = "center" }, head),
    thumb(scene ~= nil and scene.preview_path or nil, "scene-img", 0,
      PREVIEW_H, 6),
    ui.row({ key = "info", gap = 10, align = "center" }, info),
  })
end

-- Лента вариантов: h/l, ограниченное окно вокруг фокуса.
local function variantStrip(rows)
  if #rows == 0 then
    return ui.label({ key = "strip-empty", text = search ~= ""
      and "ничего не нашлось" or "вариантов скина для этих обоев ещё нет",
      color = "on_surface_variant" })
  end
  local sel = 1
  for i, v in ipairs(rows) do
    if v.id == focusVariant then
      sel = i
    end
  end
  local a, b = windowRange(#rows, sel, STRIP_WINDOW)
  local kids = {}
  if a > 1 then
    table.insert(kids, ui.label({ key = "s-prev", text = "‹ " .. (a - 1),
      fontSize = 11, color = "on_surface_variant" }))
  end
  for i = a, b do
    local v = rows[i]
    local cov = coverageInfo(v)
    table.insert(kids, ui.column({ key = "s" .. i, gap = 2,
      width = STRIP_THUMB_W + 8,
      fill = v.id == focusVariant and PICKED or PLAIN,
      shape = skin.stagecraft() and "chamfer" or nil,
      border = (skin.stagecraft() and v.id == focusVariant)
        and skin.tokens().colors.accent or "outline/0",
      borderWidth = (skin.stagecraft() and v.id == focusVariant) and 1 or 0,
      radius = 6,
      padding = 4,
      onClick = function()
        focusVariant = v.id
        if surfaceRowById(surfaceRows(v), selSurfaceId) == nil then
          selSurfaceId = "scene"
        end
        render()
      end }, {
      thumb(v.preview, "img", STRIP_THUMB_W, STRIP_THUMB_H, 4),
      ui.label({ key = "nm", text = v.name, fontSize = 10, maxLines = 1,
        color = v.available and "on_surface" or "error" }),
      ui.label({ key = "cov",
        text = cov.state == "full" and "полное"
          or cov.state == "legacy" and "legacy" or cov.state,
        fontSize = 9, color = "on_surface_variant" }),
    }))
  end
  if b < #rows then
    table.insert(kids, ui.label({ key = "s-next", text = "+" .. (#rows - b)
      .. " ›", fontSize = 11, color = "on_surface_variant" }))
  end
  return ui.row({ key = "strip", gap = 6, align = "center" }, kids)
end

-- Список поверхности: сцена + компоненты, окно вокруг выбранного.
local function surfaceList(v)
  local rows = surfaceRows(v)
  if #rows == 0 then
    return ui.label({ key = "sl-empty", text = "компоненты недоступны",
      color = "on_surface_variant" })
  end
  local sel = 1
  for i, r in ipairs(rows) do
    if r.surface_id == selSurfaceId then
      sel = i
    end
  end
  local a, b = windowRange(#rows, sel, SURF_WINDOW)
  local kids = {}
  if a > 1 then
    table.insert(kids, ui.label({ key = "l-prev", text = "… ещё " .. (a - 1),
      fontSize = 10, color = "on_surface_variant" }))
  end
  for i = a, b do
    local r = rows[i]
    local rl = r.scene and "концепт-макет" or runtimeLabel(r.runtime_support)
    table.insert(kids, ui.row({ key = "l" .. i, gap = 8, align = "center",
      fill = r.surface_id == selSurfaceId and PICKED or PLAIN, radius = 6,
      shape = skin.stagecraft() and "chamfer" or nil,
      border = (skin.stagecraft() and r.surface_id == selSurfaceId)
        and skin.tokens().colors.accent or "outline/0",
      borderWidth = (skin.stagecraft() and r.surface_id == selSurfaceId)
        and 1 or 0,
      padding = 4,
      onClick = function()
        selSurfaceId = r.surface_id
        render()
      end }, {
      ui.column({ key = "txt", gap = 0, flexGrow = 1 }, {
        ui.label({ key = "nm", text = r.scene and "Сцена"
          or (r.name ~= "" and r.name or r.surface_id), fontSize = 12,
          maxLines = 1 }),
        ui.label({ key = "meta", text = kindLabel(r.kind) .. " · "
          .. r.surface_id, fontSize = 10, color = "on_surface_variant",
          maxLines = 1 }),
      }),
      ui.label({ key = "rt", text = rl, fontSize = 10,
        color = (not r.scene and r.runtime_support == "implemented")
          and "primary" or "on_surface_variant" }),
    }))
  end
  if b < #rows then
    table.insert(kids, ui.label({ key = "l-next", text = "… ещё "
      .. (#rows - b), fontSize = 10, color = "on_surface_variant" }))
  end
  return ui.scroll({ key = "surfaces", gap = 2, flexGrow = 1 }, kids)
end

-- Превью выбранного элемента — только фактический путь, без подмен.
local function selectedPreview(v)
  local row = selectedSurfaceRow(v)
  if row == nil then
    return ui.label({ key = "sp-none", text = "компонент не выбран",
      color = "on_surface_variant" })
  end
  local rl = row.scene and "концепт-макет" or runtimeLabel(row.runtime_support)
  return ui.column({ key = "sp", gap = 4, width = 420 }, {
    ui.label({ key = "t", text = row.scene and "Сцена"
      or (row.name ~= "" and row.name or row.surface_id), fontSize = 13,
      fontFamily = skin.FONT, fontWeight = "semibold", maxLines = 1 }),
    ui.label({ key = "k", text = kindLabel(row.kind) .. " · "
      .. row.surface_id .. " · " .. rl, fontSize = 10,
      color = "on_surface_variant", maxLines = 1 }),
    thumb(row.preview_path, "sp-img", 410, 200, 6),
    ui.label({ key = "hint", text = "Enter — крупно с подписями",
      fontSize = 10, color = "on_surface_variant" }),
  })
end

-- Невизуальные службы/ярлыки — текстом, без изображений.
local function nonvisualBlock(contentId, v)
  local rows = nonvisualRows(contentId, v)
  if #rows == 0 then
    return nil
  end
  local parts = {}
  for i, n in ipairs(rows) do
    if i > 12 then
      table.insert(parts, "… ещё " .. (#rows - 12))
      break
    end
    local nm = tostring((n and (n.name or n.id)) or "")
    local k = tostring((n and n.kind) or "")
    table.insert(parts, nm .. " (" .. (NONVISUAL_LABEL[k] or k) .. ")")
  end
  return ui.label({ key = "nonvisual", text = "Без изображений (" .. #rows
    .. "): " .. table.concat(parts, " · "), fontSize = 10,
    color = "on_surface_variant", maxLines = 3 })
end

-- Витрина всего скина: сцена крупно, лента вариантов, поверхность и
-- невизуальные службы. Ничего не додумывает при неполном покрытии.
local function wholeSkinBody(rows)
  local v = focusedVariant()
  if v == nil then
    return ui.column({ key = "none", gap = 4, align = "center",
      justify = "center", flexGrow = 1, fill = PLAIN, radius = 8,
      padding = 8 }, {
      ui.label({ text = search ~= "" and "ничего не нашлось"
        or "вариантов скина для этих обоев ещё нет",
        color = "on_surface_variant" }),
    })
  end
  local contentId = v.content_id or effectiveContent()
  local kids = {
    sceneBlock(v, surfaceRows(v)),
    variantStrip(rows),
    ui.row({ key = "mid", gap = 8, flexGrow = 1 },
      { surfaceList(v), selectedPreview(v) }),
  }
  local nv = nonvisualBlock(contentId, v)
  if nv ~= nil then
    table.insert(kids, nv)
  end
  return ui.column({ key = "whole", gap = 6, flexGrow = 1 }, kids)
end

-- Превью обоев из записи каталога: только явные поля записи.
local function capturePreview(it)
  local c = it and it.capture
  if type(c) == "table" then
    return c.preview_path or c.path or c.thumb or nil
  end
  return it and (it.preview_path or it.preview or it.thumbnail) or nil
end

-- Список обоев: identity + активный/кандидаты.
local function wallpaperListBody()
  local rows = wallRows()
  local kids = {}
  for i, it in ipairs(rows) do
    local id = itemId(it)
    local vs = variantsFor(id)
    local ai = activeInfo(id)
    local candidates = 0
    for _, v in ipairs(vs) do
      if v.state == "candidate" then
        candidates += 1
      end
    end
    table.insert(kids, ui.row({ key = "w" .. i, gap = 8, align = "center",
      fill = i == focusWall and PICKED or PLAIN, radius = 6, padding = 4,
      shape = skin.stagecraft() and "chamfer" or nil,
      border = (skin.stagecraft() and i == focusWall)
        and skin.tokens().colors.accent or "outline/0",
      borderWidth = (skin.stagecraft() and i == focusWall) and 1 or 0,
      onClick = function()
        focusWall = i
        render()
      end }, {
      thumb(capturePreview(it), "img", ROW_THUMB_W, ROW_THUMB_H, 4),
      ui.column({ key = "txt", gap = 0, flexGrow = 1 }, {
        ui.label({ key = "nm", text = itemTitle(it), fontSize = 12,
          maxLines = 1 }),
        ui.label({ key = "id", text = string.sub(id, 1, 12) .. "… · вариантов "
          .. #vs, fontSize = 10, color = "on_surface_variant" }),
      }),
      not ai.known
        and ui.label({ key = "act", text = "статус неизвестен", fontSize = 11,
          color = "error" })
        or ai.variant_id ~= nil
          and ui.label({ key = "act", text = "активен", fontSize = 11,
            color = "secondary" })
          or ui.label({ key = "act", text = "без скина", fontSize = 11,
            color = "on_surface_variant" }),
      candidates > 0 and ui.label({ key = "cand",
        text = "кандидаты " .. candidates, fontSize = 11,
        color = "tertiary" }) or nil,
    }))
  end
  if #kids == 0 then
    table.insert(kids, ui.label({ key = "empty",
      text = search ~= "" and "ничего не нашлось" or "каталог обоев пуст",
      color = "on_surface_variant" }))
  end
  return ui.scroll({ key = "walls", gap = 2, flexGrow = 1 }, kids)
end

-- ───────────────────────────────────────────────── секции и подвалы

local function sectionTabs()
  local row = {}
  for i, id in ipairs(SECTION_IDS) do
    local chosen = sectionIdx == i
    local b = {
      key = "sec-" .. id,
      text = SECTION_LABELS[id],
      glyph = id == "skins" and "palette"
        or id == "jobs" and "history" or "copy",
      controlSize = "sm",
      variant = chosen and "primary" or "ghost",
      onClick = function()
        sectionIdx = i
        mode = "browse"
        render()
      end,
    }
    if skin.stagecraft() then
      -- Активный Stagecraft: выбранная секция — только флаг selected поверх
      -- variant primary/ghost. Рамку кнопке не задаём: border-only props
      -- строят локальную палитру с прозрачным текстом (reconciler строит
      -- CustomPalette по любому цвету) — читаемость даёт нативная палитра.
      b.selected = chosen
      b.radius = 6
    end
    table.insert(row, ui.button(b))
  end
  return ui.row({ key = "sections", gap = 6, align = "center" }, row)
end

-- Детали: крупный выбранный кадр (сцена или компонент) с подписями.
local function detailBody()
  local v = focusedVariant()
  if v == nil then
    return ui.label({ key = "nodetail", text = "вариант не выбран",
      color = "on_surface_variant" })
  end
  local row = selectedSurfaceRow(v)
  local contentId = v.content_id or effectiveContent()
  local it = itemById(contentId)
  local activeId = activeInfo(contentId)
  local cov = coverageInfo(v)
  local lines = {}
  local function line(key, text, color)
    table.insert(lines, ui.label({ key = key, text = text, fontSize = 11,
      color = color or "on_surface_variant", maxLines = 2 }))
  end
  line("kind", row ~= nil
    and (kindLabel(row.kind) .. " · " .. row.surface_id)
    or "компонент не выбран")
  if row ~= nil and not row.scene then
    line("rt", "runtime: " .. runtimeLabel(row.runtime_support)
      .. (row.runtime_support == "concept-only"
        and " — изображение концепт-макета" or ""))
  else
    line("rt", "сцена — синтетический концепт-макет пакета")
  end
  line("cov", "покрытие: " .. cov.label
    .. (cov.reason ~= "" and (": " .. cov.reason) or ""))
  line("ru", "runtime unverified")
  line("wall", "обои: " .. (it ~= nil and itemTitle(it) or contentId)
    .. " (" .. string.sub(contentId, 1, 12) .. "…)")
  line("vid", "вариант " .. v.id .. " · " .. (FAMR[v.family] or v.family))
  if v.parent ~= nil then
    line("parent", "уточнение варианта " .. v.parent)
  end
  if not activeId.known then
    line("active", "статус применения неизвестен: "
      .. (activeId.error or "нативный статус нечитаем"))
  elseif activeId.variant_id == v.id then
    line("active", "этот вариант сейчас применён")
  elseif activeId.variant_id ~= nil then
    line("active", "применён другой вариант: " .. activeId.variant_id)
  else
    line("active", "скин для этих обоев не применён")
  end
  if not v.available then
    line("unavail", "недоступно: " .. (v.reason ~= "" and v.reason
      or "нативное применение запрещено"), "error")
  end
  local st = applyState(v)
  local btnText = "Применить"
  if st.legacy then
    btnText = v.state == "candidate" and "Одобрить (recipe-only)"
      or "Применить (recipe-only)"
  elseif v.state == "candidate" then
    btnText = "Одобрить"
  end
  return ui.scroll({ key = "detail", gap = 6, flexGrow = 1 }, {
    ui.label({ key = "nm", text = v.name, fontSize = 16,
      fontFamily = skin.FONT, fontWeight = "semibold" }),
    thumb(row ~= nil and row.preview_path or nil, "detail-img", 0,
      PREVIEW_H, 8),
    ui.column({ key = "meta", gap = 2 }, lines),
    ui.button({ key = "approve", text = btnText, glyph = "check",
      controlSize = "sm", variant = "outline", enabled = st.ok,
      tooltip = st.ok and "подтверждение (a)" or ("недоступно: " .. st.why),
      onClick = function()
        if applyState(v).ok then
          mode = "apply"
          render()
        end
      end }),
  })
end

-- Секция уточнения: родословная выбранного варианта.
local function refineBody()
  local v = focusedVariant()
  if v == nil then
    return ui.label({ key = "norefine", text = "вариант не выбран — "
      .. "нажмите Enter в галерее или выберите вариант",
      color = "on_surface_variant" })
  end
  local all = variantsFor(v.content_id or effectiveContent())
  local byId = {}
  for _, x in ipairs(all) do
    byId[x.id] = x
  end
  -- Родители: поднимаемся по parent_variant_id.
  local ancestors = {}
  local cur = v
  local guard = 0
  while cur ~= nil and cur.parent ~= nil and guard < 16 do
    guard += 1
    cur = byId[cur.parent]
    if cur == nil then
      break
    end
    table.insert(ancestors, cur)
  end
  -- Дети: варианты, чей parent — этот.
  local children = {}
  for _, x in ipairs(all) do
    if x.parent == v.id then
      table.insert(children, x)
    end
  end
  local rows = {
    ui.label({ key = "head", text = "Уточнение: " .. v.name, fontSize = 14,
      fontFamily = skin.FONT, fontWeight = "semibold" }),
    ui.label({ key = "hint", text = "r — выбор рамки уточнения: весь пакет "
      .. "или выбранный компонент (ничего не применяется само)",
      fontSize = 11, color = "on_surface_variant" }),
  }
  if not v.available then
    table.insert(rows, ui.label({ key = "unavail",
      text = "уточнение недоступно: " .. (v.reason ~= "" and v.reason
        or "семейство недоступно нативно"), fontSize = 11, color = "error" }))
  end
  if #ancestors > 0 then
    table.insert(rows, ui.label({ key = "anc-h", text = "Родители:",
      fontSize = 12 }))
    for i, a in ipairs(ancestors) do
      table.insert(rows, ui.label({ key = "anc" .. i,
        text = "← " .. a.name .. " (" .. a.id .. ", " .. a.state .. ")",
        fontSize = 11, color = "on_surface_variant" }))
    end
  end
  table.insert(rows, ui.label({ key = "cur-h", text = "Выбран:",
    fontSize = 12 }))
  table.insert(rows, ui.label({ key = "cur", text = v.name .. " (" .. v.id
    .. ", " .. v.state .. (v.revision ~= nil
    and ", rev " .. v.revision or "") .. ")", fontSize = 11 }))
  table.insert(rows, ui.label({ key = "child-h",
    text = "Итерации (" .. #children .. "):", fontSize = 12 }))
  for i, c in ipairs(children) do
    table.insert(rows, ui.label({ key = "ch" .. i,
      text = "→ " .. c.name .. " (" .. c.id .. ", " .. c.state
      .. (c.revision ~= nil and ", rev " .. c.revision or "") .. ")",
      fontSize = 11, color = "on_surface_variant" }))
  end
  if notesBuf ~= "" then
    table.insert(rows, ui.label({ key = "draft",
      text = "черновик заметок: " .. notesBuf, fontSize = 11,
      color = "tertiary", maxLines = 2 }))
  end
  return ui.scroll({ key = "refine", gap = 3, flexGrow = 1 }, rows)
end

-- Секция задач: сводка + строки. Отмена — c на активной задаче.
local function jobsBody()
  local jobs = noctaliaJobs()
  local rows = {
    ui.label({ key = "summary", text = jobsSummaryLine(), fontSize = 11,
      color = "on_surface_variant" }),
  }
  for i, job in ipairs(jobs) do
    local statePair = ({
      queued = { "в очереди", "on_surface_variant" },
      running = { "идёт", "primary" },
      succeeded = { "готово", "primary" },
      failed = { "ошибка", "error" },
      cancelled = { "отменено", "on_surface_variant" },
      interrupted = { "прервано", "error" },
    })[job.state] or { job.state ~= "" and job.state or "неизвестно",
      "on_surface_variant" }
    local it = job.content_id ~= nil and itemById(job.content_id) or nil
    local title = it ~= nil and itemTitle(it)
      or (job.content_id ~= nil and string.sub(job.content_id, 1, 12) .. "…"
        or "обои не указаны")
    local kids = {
      ui.label({ key = "t", text = title, fontSize = 12, maxLines = 1,
        flexGrow = 1 }),
      ui.label({ key = "st", text = statePair[1], fontSize = 11,
        color = statePair[2] }),
    }
    if job.count ~= nil and job.count > 1 then
      table.insert(kids, ui.label({ key = "cnt", text = "серия "
        .. tostring(job.count), fontSize = 11, color = "on_surface_variant" }))
    end
    if job.stage ~= "" and job.state == "running" then
      table.insert(kids, ui.label({ key = "stage", text = job.stage,
        fontSize = 11, color = "on_surface_variant" }))
    end
    local active = job.state == "queued" or job.state == "running"
    if active then
      table.insert(kids, ui.label({ key = "cancel-hint", text = "c — отмена",
        fontSize = 10, color = "on_surface_variant" }))
    end
    table.insert(rows, ui.column({ key = "j" .. i, gap = 1,
      fill = i == focusJob and PICKED or PLAIN, radius = 6, padding = 4,
      shape = skin.stagecraft() and "chamfer" or nil,
      border = (skin.stagecraft() and i == focusJob)
        and skin.tokens().colors.accent or "outline/0",
      borderWidth = (skin.stagecraft() and i == focusJob) and 1 or 0,
      onClick = function()
        focusJob = i
        render()
      end }, {
      ui.row({ key = "line", gap = 8, align = "center" }, kids),
      job.error ~= "" and ui.label({ key = "err", text = job.error,
        fontSize = 10, color = "error", maxLines = 2 }) or nil,
    }))
  end
  if #jobs == 0 then
    table.insert(rows, ui.label({ key = "empty",
      text = "noctalia-задач нет", color = "on_surface_variant" }))
  end
  return ui.scroll({ key = "jobs", gap = 3, flexGrow = 1 }, rows)
end

-- ───────────────────────────────────────────────── модалы и подвалы

-- Подтверждение применения: полный пакет или явный recipe-only legacy.
local function applyModal()
  local v = focusedVariant()
  local st = applyState(v)
  local candidate = v ~= nil and v.state == "candidate"
  local title
  if not st.ok then
    title = "Применение недоступно"
  elseif st.full then
    title = candidate and "Одобрить полный пакет?"
      or "Применить полный пакет?"
  else
    title = candidate and "Одобрить recipe-only?"
      or "Применить recipe-only?"
  end
  local what = st.ok
    and (v.name .. " · " .. (FAMR[v.family] or v.family)
      .. (st.legacy and " · recipe-only: без компонентов пакета"
        or " · полный пакет"))
    or ("нельзя: " .. st.why)
  local hint = st.ok
    and (candidate
      and "Enter — одобрить и сохранить выбор · Esc — отмена. При включённом auto-apply выбор будет применён автоматически."
      or "Enter — применить · Esc — отмена")
    or "Esc — вернуться"
  return ui.column({ key = "apply-modal", gap = 4, fill = "surface/0.5",
    radius = 8, padding = 8 }, {
    ui.label({ key = "t", text = title, fontSize = 13,
      fontWeight = "semibold" }),
    ui.label({ key = "what", text = what, fontSize = 11,
      color = st.ok and "on_surface" or "error", maxLines = 2 }),
    ui.label({ key = "hint", text = hint, fontSize = 10,
      color = "on_surface_variant", maxLines = 2 }),
  })
end

-- Подтверждение серии: размер 1..4 цифрами, выбор семейства j/k, точная
-- смета вызовов из замороженной живой оценки.
local function countModal()
  local v = focusedVariant()
  local contentId = effectiveContent()
  local it = itemById(contentId)
  local fam = countFamily or (v ~= nil and v.family or nil) or "stagecraft"
  local famOk = familyAvailable(fam)
  local q = countQuote
  local per = q ~= nil and q.perVariant or nil
  local total = per ~= nil and (countN * per) or nil
  local nonvisual = nonvisualRows(contentId, v)
  local callsText
  if per == nil then
    callsText = "смета вызовов недоступна — обновите снимок"
  else
    callsText = "1 сцена + " .. math.max(0, per - 1) .. " компонент(ов) · "
      .. "всего " .. total .. " generate_image вызов(ов): "
      .. countN .. " × " .. per
  end
  return ui.column({ key = "count-modal", gap = 4, fill = "surface/0.5",
    radius = 8, padding = 8 }, {
    ui.label({ key = "t", text = "Сгенерировать серию пакетов?", fontSize = 13,
      fontWeight = "semibold" }),
    ui.label({ key = "what", text = (it ~= nil and itemTitle(it) or contentId)
      .. " · семейство " .. (FAMR[fam] or fam) .. " · размер " .. countN,
      fontSize = 11, maxLines = 2,
      color = famOk and "on_surface" or "error" }),
    ui.label({ key = "calls", text = callsText, fontSize = 11,
      color = per ~= nil and "on_surface" or "error", maxLines = 2 }),
    ui.label({ key = "nonvisual", text = "невизуальных записей без "
      .. "изображений: " .. #nonvisual
      .. " (изображения для них не генерируются)", fontSize = 10,
      color = "on_surface_variant", maxLines = 2 }),
    ui.label({ key = "paid", text = "каждый вызов — явный платный вызов "
      .. "модели; отмена партии останавливает расход, готовые кандидаты "
      .. "сохраняются", fontSize = 10, color = "on_surface_variant",
      maxLines = 2 }),
    ui.label({ key = "hint", text = (famOk and per ~= nil)
      and "1–4 — размер · j/k — семейство · Enter — запустить · Esc — отмена"
      or "генерация недоступна (семейство/смета) · Esc — выход",
      fontSize = 10,
      color = (famOk and per ~= nil) and "on_surface_variant" or "error" }),
  })
end

-- Заметки уточнения: ручной ввод вместо ui.input (захваченные буквы до
-- родного поля не доходят). Показывает точное число вызовов.
local function notesModal()
  local v = focusedVariant()
  local q = refineQuote
  local calls = refineScope == "surface" and 2
    or (q ~= nil and q.perVariant or nil)
  local target
  if refineScope == "surface" then
    local row = selectedSurfaceRow(v)
    local nm = row ~= nil
      and (row.name ~= "" and row.name or row.surface_id) or "?"
    target = "компонент «" .. nm .. "»"
  else
    target = "весь пакет (сцена + компоненты)"
  end
  return ui.column({ key = "notes-modal", gap = 4, fill = "surface/0.5",
    radius = 8, padding = 8 }, {
    ui.label({ key = "t", text = "Заметки уточнения", fontSize = 13,
      fontWeight = "semibold" }),
    ui.label({ key = "what", text = "итерация «"
      .. (v ~= nil and v.name or "?") .. "» · " .. target, fontSize = 11,
      color = "on_surface_variant", maxLines = 2 }),
    ui.label({ key = "buf", text = "заметки: " .. (notesBuf ~= ""
      and notesBuf or "—"), fontSize = 12, maxLines = 3,
      fontFamily = skin.MONO }),
    ui.label({ key = "calls", text = calls ~= nil
      and ("ровно " .. calls .. " generate_image вызов(ов) — "
        .. "подтверждение Enter")
      or "смета вызовов недоступна", fontSize = 11,
      color = calls ~= nil and "on_surface" or "error" }),
    ui.label({ key = "hint", text = "текст вводится клавишами · "
      .. "Enter — запустить уточнение (платный вызов) · Esc — отмена",
      fontSize = 10, color = "on_surface_variant" }),
  })
end

-- Выбор рамки уточнения: весь пакет или выбранный компонент (j/k + Enter).
local function refineModal()
  local v = focusedVariant()
  local selRow = selectedSurfaceRow(v)
  local selOk, selWhy = refineSurfaceEligible(v)
  local q = refineQuote
  local wholeCalls = q ~= nil and q.perVariant or nil
  local qOk = q ~= nil and q.digest ~= nil and wholeCalls ~= nil
  local famOk = v ~= nil and familyAvailable(v.family)
  local wholeOk = qOk and famOk
  local surfaceOk = selOk and qOk and famOk
  local function opt(key, active, title, sub, enabled)
    return ui.row({ key = key, gap = 8, align = "center",
      fill = active and PICKED or PLAIN, radius = 6, padding = 4,
      shape = skin.stagecraft() and "chamfer" or nil,
      border = (skin.stagecraft() and active)
        and skin.tokens().colors.accent or "outline/0",
      borderWidth = (skin.stagecraft() and active) and 1 or 0 }, {
      ui.label({ key = "mark", text = active and "●" or "○", fontSize = 12,
        color = enabled and "primary" or "on_surface_variant" }),
      ui.column({ key = "txt", gap = 0, flexGrow = 1 }, {
        ui.label({ key = "t", text = title, fontSize = 12,
          color = enabled and "on_surface" or "on_surface_variant" }),
        ui.label({ key = "s", text = sub, fontSize = 10,
          color = "on_surface_variant", maxLines = 2 }),
      }),
    })
  end
  local selName = selRow ~= nil
    and (selRow.scene and "сцена"
      or (selRow.name ~= "" and selRow.name or selRow.surface_id))
    or "компонент"
  local selSub
  if surfaceOk then
    selSub = selName .. " — 2 вызова (компонент + сцена)"
  elseif not famOk then
    selSub = "недоступно: семейство не реализовано нативно"
  elseif not qOk then
    selSub = "недоступно: нет сметы вызовов"
  else
    selSub = "недоступно: " .. selWhy
  end
  local kids = {
    ui.label({ key = "t", text = "Уточнение", fontSize = 13,
      fontWeight = "semibold" }),
    ui.label({ key = "what", text = v ~= nil
      and ("вариант «" .. v.name .. "»") or "вариант не выбран",
      fontSize = 11, color = "on_surface_variant", maxLines = 1 }),
    opt("scope-whole", refineScope == "whole", "Весь пакет",
      wholeOk and ("пересобрать сцену и все компоненты — " .. wholeCalls
        .. " вызов(ов)") or "недоступно (семейство/смета)", wholeOk),
    opt("scope-surf", refineScope == "surface", "Выбранный компонент",
      selSub, surfaceOk),
    ui.label({ key = "hint", text = "j/k — выбор · Enter — к вводу заметок · "
      .. "Esc — отмена", fontSize = 10, color = "on_surface_variant" }),
  }
  return ui.column({ key = "refine-modal", gap = 4, fill = "surface/0.5",
    radius = 8, padding = 8 }, kids)
end

-- Строка поиска: витрина запроса, ввод ведёт onKey.
local function searchRow()
  return ui.row({ key = "searchrow", gap = 6, align = "center" }, {
    ui.glyph({ name = "search", size = 14, color = searchMode
      and "primary" or "on_surface_variant" }),
    ui.label({ key = "q", text = search ~= "" and search
      or (searchMode and "…" or "поиск по вариантам и обоям (/)"),
      fontSize = 12, color = searchMode and "primary"
      or "on_surface_variant", flexGrow = 1, maxLines = 1 }),
  })
end

local function footerHint()
  local text
  if mode == "browse" then
    text = "h/l — вариант · j/k — сцена/компонент · Tab — обои/варианты · "
      .. "Ctrl+j/k — секции · / — поиск · Enter — детали · a — применить · "
      .. "g — серия · r — уточнить · Esc — закрыть"
  elseif mode == "detail" then
    text = "a — применить · r — уточнить · g — серия · Esc — назад"
  elseif mode == "refine" then
    text = "j/k — весь пакет/компонент · Enter — к заметкам · Esc — отмена"
  elseif mode == "notes" then
    text = "ввод заметок · Enter — запустить · Esc — отмена"
  elseif mode == "count" then
    text = "1–4 — размер · j/k — семейство · Enter — запустить · Esc — отмена"
  elseif mode == "apply" then
    text = "Enter — подтвердить выбор · Esc — отмена"
  end
  return ui.label({ key = "foot", text = text, fontSize = 10,
    color = "on_surface_variant", maxLines = 1 })
end

local function header()
  local contentId = effectiveContent()
  local it = contentId ~= nil and itemById(contentId) or nil
  local top = noctaliaTop()
  local err = ""
  for _, candidate in ipairs({ status.lastError, status.syncError }) do
    if candidate ~= "" then
      err = candidate
      break
    end
  end
  local kids = {
    ui.glyph({ name = "palette", size = 20, color = "primary" }),
    ui.label({ text = "Скины Noctalia", fontSize = 15,
      fontFamily = skin.FONT, fontWeight = "semibold" }),
    ui.spacer({ flexGrow = 1 }),
    ui.label({ key = "wall", text = it ~= nil and itemTitle(it)
      or "обои не выбраны", fontSize = 12, maxLines = 1 }),
  }
  if top == nil then
    table.insert(kids, ui.label({ key = "cap",
      text = "noctalia недоступна", fontSize = 11, color = "error" }))
  elseif top.available == false then
    table.insert(kids, ui.label({ key = "cap",
      text = "нативное применение недоступно"
        .. (type(top.reason) == "string" and top.reason ~= ""
          and (": " .. top.reason) or ""),
      fontSize = 11, color = "error" }))
  else
    local ai = contentId ~= nil and activeInfo(contentId) or nil
    if ai ~= nil and not ai.known then
      table.insert(kids, ui.label({ key = "cap",
        text = "статус применения неизвестен", fontSize = 11,
        color = "error" }))
    end
  end
  table.insert(kids, ui.button({
    key = "refresh", glyph = "refresh", controlSize = "sm", variant = "ghost",
    tooltip = "свежий снимок", enabled = not busy(),
    onClick = function() poke("refresh") end,
  }))
  table.insert(kids, ui.button({
    key = "close", glyph = "close", controlSize = "sm", variant = "ghost",
    onClick = function() panel.close() end,
  }))
  return ui.row({ key = "head", gap = 8, align = "center" }, kids), err
end

render = function()
  if held ~= nil and not holdOwnsMode() then
    endHold()
  end
  local head, err = header()
  local s = snap()
  local la = s and s.last_action or nil
  local feedback = nil
  if type(la) == "table" and type(la.message) == "string" and la.message ~= "" then
    feedback = ui.label({ key = "action", text = (la.ok == true and "Готово: "
      or "Ошибка: ") .. la.message, fontSize = 11, maxLines = 2,
      color = la.ok == true and "primary" or "error" })
  end

  clampFocus()

  local body
  if sectionIdx == 2 then
    body = jobsBody()
  elseif sectionIdx == 3 then
    body = refineBody()
  elseif mode == "detail" then
    body = detailBody()
  elseif view == "wallpapers" then
    body = wallpaperListBody()
  else
    body = wholeSkinBody(galleryRows())
  end

  local children = { sectionTabs(), searchRow(), body }
  -- Ряд действий — только в витрине скинов, поверх фактов доступности.
  if sectionIdx == 1 and mode == "browse" and view == "variants" then
    table.insert(children, actionRow())
  end
  table.insert(children, footerHint())
  if feedback ~= nil then
    table.insert(children, feedback)
  end
  if err ~= "" then
    table.insert(children, ui.label({ key = "err", text = err, fontSize = 11,
      color = "error", maxLines = 2 }))
  end
  if mode == "apply" then
    table.insert(children, applyModal())
  elseif mode == "count" then
    table.insert(children, countModal())
  elseif mode == "refine" then
    table.insert(children, refineModal())
  elseif mode == "notes" then
    table.insert(children, notesModal())
  end

  local root = ui.column({ key = "root", flexGrow = 1, gap = 8,
    paddingH = 6, paddingV = 6 }, { head, table.unpack(children) })
  panel.render(skin.frame(root, "skins-frame"))
end

-- ──────────────────────────────────────────────────── клавиатура

local LAYOUT = {
  H = "h", J = "j", K = "k", L = "l", A = "a", G = "g", R = "r", C = "c",
  Cyrillic_er = "h", Cyrillic_ER = "h",
  Cyrillic_o = "j", Cyrillic_O = "j",
  Cyrillic_el = "k", Cyrillic_EL = "k",
  Cyrillic_de = "l", Cyrillic_DE = "l",
  Cyrillic_ef = "a", Cyrillic_EF = "a",
  Cyrillic_pe = "g", Cyrillic_PE = "g",
  Cyrillic_ka = "r", Cyrillic_KA = "r",
  Cyrillic_es = "c", Cyrillic_ES = "c",
}

local function cycleSection(d)
  sectionIdx = ((sectionIdx - 1 + d) % #SECTION_IDS) + 1
  mode = "browse"
  searchMode = false
  render()
end

function onKey(chord, pressed)
  if not pressed then
    if held and held.chord == chord then
      endHold()
    end
    return
  end
  if held and held.chord == chord then
    return
  end

  local key = LAYOUT[chord] or chord
  local lc = string.lower(chord)


  -- Заметки: текстовый режим владеет буквами.
  if mode == "notes" then
    if chord == "Escape" then
      mode = "browse"
      notesBuf = ""
    elseif chord == "Return" then
      if focusedVariant() ~= nil then
        doRefine()
      else
        mode = "browse"
        notesBuf = ""
        render()
      end
      return
    elseif chord == "BackSpace" or chord == "Backspace" then
      notesBuf = deleteBefore(notesBuf, charCount(notesBuf))
      startHold(chord, "backspace")
    elseif chord == "Space" then
      notesBuf = notesBuf .. " "
    else
      local isText, ch, edit = textEdit(chord)
      if isText and ch then
        if charCount(notesBuf) < 200 then
          notesBuf = notesBuf .. ch
        end
      end
    end
    render()
    return
  end

  -- Выбор рамки уточнения: j/k — весь пакет или компонент, Enter — заметки.
  if mode == "refine" then
    if chord == "Escape" then
      mode = "browse"
      refineScope = "whole"
      refineSurfaceId = nil
      render()
      return
    end
    local v = focusedVariant()
    local selOk = refineSurfaceEligible(v)
    local famOk = v ~= nil and familyAvailable(v.family)
    if chord == "Down" or key == "j" or chord == "Up" or key == "k" then
      if refineScope == "whole" then
        if selOk and famOk then
          refineScope = "surface"
          local row = selectedSurfaceRow(v)
          refineSurfaceId = row ~= nil and row.surface_id or nil
        end
      else
        refineScope = "whole"
        refineSurfaceId = nil
      end
    elseif chord == "Return" then
      if refineScope == "surface" and not (selOk and famOk) then
        refineScope = "whole"
        refineSurfaceId = nil
      end
      if famOk and refineQuote ~= nil and refineQuote.digest ~= nil
        and refineQuote.perVariant ~= nil then
        mode = "notes"
      else
        mode = "browse"
      end
    end
    render()
    return
  end

  -- Подтверждение серии: цифры — размер, j/k — семейство, Enter — запуск.
  if mode == "count" then
    if chord == "Escape" then
      mode = "browse"
    elseif chord == "1" or chord == "2" or chord == "3" or chord == "4" then
      countN = tonumber(chord)
    elseif chord == "Down" or key == "j" then
      local cur = 1
      for i, f in ipairs(FAMILY_IDS) do
        if f == countFamily then
          cur = i
        end
      end
      countFamily = FAMILY_IDS[(cur % #FAMILY_IDS) + 1]
    elseif chord == "Up" or key == "k" then
      local cur = 1
      for i, f in ipairs(FAMILY_IDS) do
        if f == countFamily then
          cur = i
        end
      end
      countFamily = FAMILY_IDS[((cur - 2) % #FAMILY_IDS) + 1]
    elseif chord == "Return" then
      doGenerateBatch()
      return
    end
    render()
    return
  end

  -- Подтверждение применения: Enter применяет, Esc отменяет.
  if mode == "apply" then
    if chord == "Escape" then
      mode = "browse"
    elseif chord == "Return" then
      doApply()
      return
    end
    render()
    return
  end

  -- Детали: только возврат и действия-подтверждения.
  if mode == "detail" then
    if chord == "Escape" then
      mode = "browse"
    elseif key == "a" then
      local v = focusedVariant()
      if applyState(v).ok then
        mode = "apply"
      end
    elseif key == "g" then
      openCount()
      return
    elseif key == "r" then
      openRefine()
      return
    end
    render()
    return
  end

  -- Режим поиска: буквы в запрос, Esc — выход из режима.
  if searchMode then
    if chord == "Escape" then
      searchMode = false
    elseif chord == "Return" then
      searchMode = false
    elseif chord == "BackSpace" or chord == "Backspace" then
      search = deleteBefore(search, charCount(search))
      startHold(chord, "backspace")
    elseif chord == "Space" then
      search = search .. " "
    else
      local isText, ch = textEdit(chord)
      if isText and ch and charCount(search) < 64 then
        search = search .. ch
      end
    end
    clampFocus()
    render()
    return
  end
  -- Ctrl+j/k переключают секции только в browse, не внутри ввода или модалов.
  if chord == "ctrl+j" or chord == "ctrl+Cyrillic_o" then
    cycleSection(1)
    return
  end
  if chord == "ctrl+k" or chord == "ctrl+Cyrillic_el" then
    cycleSection(-1)
    return
  end

  -- Обычный режим (browse), секции Skins/Jobs/Refine.
  if chord == "Escape" then
    if view == "wallpapers" then
      view = "variants"
      render()
      return
    end
    if search ~= "" then
      search = ""
      clampFocus()
      render()
      return
    end
    panel.close()
    return
  end

  if lc == "slash" or chord == "slash" or chord == "period" then
    -- Слэш-позиция: в RU-раскладке физическая клавиша даёт «.».
    searchMode = true
    render()
    return
  end

  if chord == "Tab" then
    view = view == "variants" and "wallpapers" or "variants"
    clampFocus()
    render()
    return
  end

  if sectionIdx == 2 then
    -- Задачи: фокус и отмена.
    local n = #noctaliaJobs()
    if chord == "Down" or key == "j" then
      if n > 0 then
        focusJob = focusJob % n + 1
      end
      startHold(chord, "job-down")
    elseif chord == "Up" or key == "k" then
      if n > 0 then
        focusJob = (focusJob - 2) % n + 1
      end
      startHold(chord, "job-up")
    elseif key == "c" then
      cancelFocusedJob()
    elseif chord == "Return" then
      sectionIdx = 1
      render()
      return
    end
    render()
    return
  end

  if sectionIdx == 3 then
    -- Уточнение: навигация по вариантам, r открывает выбор рамки.
    if chord == "Down" or key == "j" then
      moveVariantSel(1)
      startHold(chord, "down")
      return
    elseif chord == "Up" or key == "k" then
      moveVariantSel(-1)
      startHold(chord, "up")
      return
    elseif key == "r" then
      openRefine()
      return
    elseif chord == "Return" then
      sectionIdx = 1
      mode = "detail"
      render()
      return
    end
    render()
    return
  end

  -- Секция Skins.
  if view == "wallpapers" then
    if chord == "Down" or key == "j" then
      moveFocus(0, 1)
      startHold(chord, "down")
    elseif chord == "Up" or key == "k" then
      moveFocus(0, -1)
      startHold(chord, "up")
    elseif chord == "Return" then
      local rows = wallRows()
      local pick = rows[focusWall]
      if pick ~= nil then
        selContent = itemId(pick)
        view = "variants"
        focusVariant = nil
        clampFocus()
      end
      render()
    else
      render()
    end
    return
  end

  if chord == "Down" or key == "j" then
    moveFocus(0, 1)
    startHold(chord, "down")
  elseif chord == "Up" or key == "k" then
    moveFocus(0, -1)
    startHold(chord, "up")
  elseif chord == "Right" or key == "l" then
    moveFocus(1, 0)
    startHold(chord, "right")
  elseif chord == "Left" or key == "h" then
    moveFocus(-1, 0)
    startHold(chord, "left")
  elseif chord == "Return" then
    if focusedVariant() ~= nil then
      mode = "detail"
    end
    render()
  elseif key == "a" then
    local v = focusedVariant()
    if applyState(v).ok then
      mode = "apply"
    end
    render()
  elseif key == "g" then
    openCount()
    return
  elseif key == "r" then
    openRefine()
    return
  else
    render()
  end
end

-- ──────────────────────────────────────────────────── жизненный цикл

function onOpen(_context)
  status = noctalia.state.get(KEY_STATUS) or {}
  selContent = effectiveContent()
  clampFocus()
  render()
end

function onClose()
  -- Черновики живут в панельных переменных; снимки и очередь — у службы.
  endHold()
  mode = "browse"
  searchMode = false
  sectionIdx = 1
  refineScope = "whole"
  refineSurfaceId = nil
  countQuote = nil
  refineQuote = nil
end

noctalia.state.watch(KEY_STATUS, function(value)
  status = value or {}
  if selContent == nil then
    selContent = effectiveContent()
  end
  clampFocus()
  render()
end)

render()
