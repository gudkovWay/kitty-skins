--!nonstrict
-- Иконка Wallpaper Studio в баре. Ничего не считает и не запускает: читает
-- studio.status, который пишет служба. Экземпляров столько, сколько баров.
--
-- Панель открывается ТОЛЬКО кликом: никаких автоматических togglePanel из
-- службы, уведомления всплывают сами, а панели открывают руками.
--
--   левая клавиша   открыть/закрыть панель
--   средняя         попросить службу свежий снимок
--   правая          то же, на случай когда средняя занята мышью
--
-- Цвет глифа:
--   идёт задача         loader  primary            работа, не мешать
--   нужен захват        image   tertiary           ждёт кадры от человека
--   есть кандидаты      image   secondary          лежит неразобранное решение
--   тишина              image   on_surface_variant обычный фон

local KEY = "studio.status"
local PANEL = "q/wallpaper-studio:studio"
local SERVICE = "q/wallpaper-studio:watch"

local function poke(event)
  noctalia.runAsync("noctalia msg plugin " .. SERVICE .. " all " .. event)
end

local function paint(glyph, color)
  barWidget.setGlyph(glyph)
  barWidget.setColor(color)
  barWidget.setGlyphColor(color)
end

local function counts(snap)
  local c = type(snap) == "table" and snap.counts or nil
  local function num(name)
    return tonumber(type(c) == "table" and c[name]) or 0
  end
  return { queued = num("queued"), running = num("running"),
    candidates = num("candidates"), needs_capture = num("needs_capture") }
end

local function refresh(s)
  s = s or {}
  local c = counts(s.snapshot)

  if c.running > 0 or c.queued > 0 or s.requestBusy or s.syncBusy then
    paint("loader", "primary")
  elseif c.needs_capture > 0 then
    paint("image", "tertiary")
  elseif c.candidates > 0 then
    paint("image", "secondary")
  else
    paint("image", "on_surface_variant")
  end

  -- Счётчик кандидатов — единственное число, достойное бара: оно зовёт
  -- кликнуть. Служебные счётчики очереди живут в подсказке.
  if c.candidates > 0 then
    barWidget.setText(tostring(c.candidates))
  else
    barWidget.setText("")
  end

  local rows = {}
  if type(s.snapshot) ~= "table" then
    table.insert(rows, { key = "state", value = "снимка ещё нет — служба просит" })
  else
    table.insert(rows, { key = "очередь", value = tostring(c.queued) })
    table.insert(rows, { key = "идёт", value = tostring(c.running) })
    table.insert(rows, { key = "кандидаты", value = tostring(c.candidates) })
    if c.needs_capture > 0 then
      table.insert(rows, { key = "нужен захват", value = tostring(c.needs_capture) })
    end
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
