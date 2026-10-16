--!nonstrict
-- Иконка «Скины» в баре: выбор Noctalia-скина для обоев. Ничего не считает
-- и не запускает — читает studio.status, который пишет общая служба
-- q/wallpaper-studio:watch. Экземпляров столько, сколько баров.
--
--   левая клавиша   открыть/закрыть панель выбора скинов
--   средняя/правая  попросить службу свежий снимок
--
-- Цвет глифа:
--   идёт noctalia-задача   loader  primary            генерация, не мешать
--   активный скин          palette secondary          применён и живёт
--   есть кандидаты         palette tertiary            ждут решения
--   тишина                 palette on_surface_variant  обычный фон
--
-- Никаких выдуманных процентов и «нативных» ярлыков: только факты снимка.

local KEY = "studio.status"
local PANEL = "q/wallpaper-studio:skins"
local SERVICE = "q/wallpaper-studio:watch"

local function poke(event)
  noctalia.runAsync("noctalia msg plugin " .. SERVICE .. " all " .. event)
end

local function paint(glyph, color)
  barWidget.setGlyph(glyph)
  barWidget.setColor(color)
  barWidget.setGlyphColor(color)
end

local function num(v)
  return tonumber(v) or 0
end

-- Сколько noctalia-вариантов всего/кандидатов в снимке. Считаем только
-- явные строки target="noctalia"; тихий снимок без noctalia — не ноль.
local function noctaliaTally(snap)
  local total, candidates, active = 0, 0, 0
  local variants = type(snap) == "table" and snap.variants or nil
  if type(variants) == "table" then
    for _, v in ipairs(variants) do
      if v ~= nil and type(v) == "table" and v.target == "noctalia" then
        total += 1
        if v.state == "candidate" then
          candidates += 1
        end
      end
    end
  end
  local top = type(snap) == "table" and snap.noctalia or nil
  if type(top) == "table" then
    -- «Активен» — только живое нативное чтение; сохранённые одобрения
    -- (mappings/mapped_variant_id) активностью не считаются.
    if top.active_known == false then
      active = 0                  -- статус неизвестен — не рисуем уверенность
    elseif type(top.active_variant_id) == "string"
      and top.active_variant_id ~= "" then
      active = 1
    else
      local variants = type(snap) == "table" and snap.variants or nil
      if type(variants) == "table" then
        for _, v in ipairs(variants) do
          if type(v) == "table" and v.target == "noctalia" and v.active == true then
            active += 1
          end
        end
      end
    end
  end
  return { total = total, candidates = candidates, active = active }
end

-- Сводка noctalia-задач: сначала явная сводка снимка, иначе честный прочерк.
local function jobsSummary(snap)
  local top = type(snap) == "table" and snap.noctalia or nil
  local j = type(top) == "table" and top.jobs or nil
  if type(j) ~= "table" then
    return "задач: нет данных"
  end
  local parts = {}
  if num(j.queued) > 0 then
    table.insert(parts, "в очереди " .. num(j.queued))
  end
  if num(j.running) > 0 then
    table.insert(parts, "идёт " .. num(j.running))
  end
  if num(j.failed) > 0 then
    table.insert(parts, "ошибки " .. num(j.failed))
  end
  if #parts == 0 then
    return "задач нет"
  end
  return table.concat(parts, " · ")
end

local function refresh(s)
  s = s or {}
  local snap = type(s.snapshot) == "table" and s.snapshot or nil
  local t = noctaliaTally(snap)
  local jobs = jobsSummary(snap)

  local busyNow = s.requestBusy == true or s.syncBusy == true
  if not busyNow and type(snap) == "table" then
    local j = type(snap.noctalia) == "table" and snap.noctalia.jobs or nil
    if type(j) == "table" and (num(j.running) > 0 or num(j.queued) > 0) then
      busyNow = true
    end
  end

  if busyNow then
    paint("loader", "primary")
  elseif t.active > 0 then
    paint("palette", "secondary")
  elseif t.candidates > 0 then
    paint("palette", "tertiary")
  else
    paint("palette", "on_surface_variant")
  end

  -- Цифра в баре — только неразобранное решение: кандидаты, а не служебные
  -- счётчики очереди (те живут в подсказке).
  barWidget.setText(t.candidates > 0 and tostring(t.candidates) or "")

  local rows = {}
  if type(snap) ~= "table" then
    table.insert(rows, { key = "state", value = "снимка ещё нет — служба просит" })
  else
    local activeKnown = true
    local topW = type(snap) == "table" and snap.noctalia or nil
    if type(topW) == "table" and topW.active_known == false then
      activeKnown = false
    end
    table.insert(rows, { key = "активно", value = not activeKnown
      and "статус применения неизвестен"
      or t.active > 0 and "скин применён" or "скин не применён" })
    table.insert(rows, { key = "кандидаты", value = tostring(t.candidates) })
    table.insert(rows, { key = "задачи", value = jobs })
    if (s.lastError or "") ~= "" then
      table.insert(rows, { key = "ошибка", value = s.lastError })
    end
  end
  barWidget.setTooltip(rows)
end

-- Панель — только по явному клику человека.
function onClick()
  noctalia.togglePanel(PANEL)
end

function onMiddleClick()
  poke("refresh")
end

function onRightClick()
  poke("refresh")
end

noctalia.state.watch(KEY, refresh)
refresh(noctalia.state.get(KEY))
