--!nonstrict
-- Служба: переводит текущие обои Wallpaper Studio в профиль шейдера и вешает
-- его на разрешённые классы окон через HyprWindowShade.
--
-- Источник состояния. Канал `noctalia.state` у Noctalia изолирован по id
-- плагина — чужой ключ прочитать нельзя в принципе. Поэтому берётся
-- персистентный playback.json плагина q/wallpaper-studio: `enabled` и
-- `current[выход]` — запись рендерера, из которой активным считается только
-- source_id записи со status == "playing". Это односторонний мост чтения: сам
-- файл не пишется ни при каком исходе, а декодируется только при первом
-- обращении и когда сменился его mtime — остальные тики читают лишь
-- метаданные файла. Контент Workshop не сканируется: project.json читается
-- один раз на новый id, и решение кэшируется.
--
-- Дедупликация — требование корректности, а не экономия: classshader
-- применяется ко всем окнам класса, поэтому повторный dispatch того же
-- профиля заново прогонял бы по одному и тому же пути все окна класса, хотя
-- нужный эффект на них уже установлен.

local STATUS_PLUGIN = "q/wallpaper-studio"
local STATUS_FILE = "playback.json"
local WORKSHOP_APP = "431960"

-- Редкий тик оправдан: смена обоев — событие на глазах человека, а не на
-- кадрах, и между сменами сервис не делает ничего, кроме дешёвой проверки
-- mtime двух файлов.
local TICK_MS = 2000

local CLEAR = "clear"

-- Оба значения уходят внутрь одной shell-строки `hyprctl dispatch '…'`:
-- классы и имена файлов приходят из редактируемого profiles.json, поэтому
-- пропускаем только безопасные токены, а не экранируем.
local SAFE_TOKEN = "^[A-Za-z0-9_.%-]+$"

local WorkshopRoots = {
    "/home/q/.local/share/Steam/steamapps/workshop/content/" .. WORKSHOP_APP,
    "~/.local/share/Steam/steamapps/workshop/content/" .. WORKSHOP_APP,
    "~/.steam/steam/steamapps/workshop/content/" .. WORKSHOP_APP,
    "~/.steam/root/steamapps/workshop/content/" .. WORKSHOP_APP,
}

local cfg = {
    classes = {},       -- string[], безопасные токены
    classSet = {},      -- class -> true, для отсечения дублей
    shaderDir = nil,    -- string
    profiles = {},      -- имя профиля -> имя файла
    byId = {},          -- wallpaper id -> имя профиля
    byTag = {},         -- нормализованный тег -> имя профиля
    keywords = {},      -- { { keyword, length, profile } } в детерминированном порядке
    ok = false,
}

-- applied[class] = путь шейдера или "clear" — что последний раз было
-- отправлено этому классу. Никогда не сбрасывается: сравнение с ним и есть
-- дедупликация, а исчезнувший из profiles.json класс через него же и
-- очищается.
local applied = {}

-- resolution[id] = имя профиля или false (= clear). Ложь отличает
-- «посмотрели и решили не вешать» от «ещё не смотрели», чтобы project.json
-- читался ровно один раз на id.
local resolution = {}

local configMtime = nil

-- Кэширование playback.json. statusMtime — mtime файла, для которого получен
-- currentCache; nil означает «файла не было». currentCache хранит таблицу
-- current либо false (= «декодировали: состояния нет»), чтобы отсутствие
-- файла, выключенное состояние и отсутствие current не заставляли читать файл
-- заново на каждом тике.
local statusMtime = nil
local currentCache = false

-- ────────────────────────────────────────────────────────── конфигурация

local function safeToken(value)
    if type(value) ~= "string" then return nil end
    return string.match(value, SAFE_TOKEN)
end

-- Нормализация регистра для языков, которые реально встречаются в profiles.json
-- и в workshop-метаданных: ASCII плюс русская кириллица. Идём по кодам
-- Unicode, а не по байтам: string.lower понижает только ASCII, поэтому «СНЕГ»
-- и «снег» остались бы разными ключами и правило по русскому тегу молчало бы.
-- CJK регистра не имеет и проходит через функцию без изменений — как и любой
-- другой кодпойнт вне двух перечисленных диапазонов.
local function normalizeText(value)
    if type(value) ~= "string" then return value end
    local ok, normalized = pcall(function()
        local out = {}
        for _, code in utf8.codes(value) do
            if code >= 0x41 and code <= 0x5A then        -- A-Z
                code = code + 0x20
            elseif code >= 0x410 and code <= 0x42F then  -- А-Я
                code = code + 0x20
            elseif code == 0x401 then                    -- Ё
                code = 0x451
            end
            out[#out + 1] = utf8.char(code)
        end
        return table.concat(out)
    end)
    -- Невалидный UTF-8 (utf8.codes на нём падает) не должен ронять загрузку
    -- конфига: понижение по ASCII — то же поведение, что было до правки.
    return ok and normalized or string.lower(value)
end

-- Имя профиля валидно, только если оно объявлено в profiles и его файл
-- существует: разрыв между JSON и каталогом шейдеров ловится здесь, а не
-- молчанием в hyprctl. Функция чистая, чтобы валидировать ещё не
-- установленный конфиг.
local function profilePathIn(conf, name)
    if type(name) ~= "string" then return nil end
    local file = conf.profiles[name]
    if not file or not conf.shaderDir then return nil end
    local path = conf.shaderDir .. "/" .. file
    return noctalia.fileExists(path) and path or nil
end

local function profilePath(name)
    return profilePathIn(cfg, name)
end

-- nil-возврат = причина, по которой конфиг не принят.
local function loadConfig()
    local dir = noctalia.pluginDir()
    local raw = dir and noctalia.readFile(dir .. "/profiles.json") or nil
    local decoded = raw and noctalia.json.decode(raw) or nil
    if type(decoded) ~= "table" then
        return "profiles.json не читается или не JSON"
    end

    local next_ = {
        classes = {}, classSet = {},
        profiles = {}, byId = {}, byTag = {}, keywords = {},
    }

    if type(decoded.shader_dir) == "string" and decoded.shader_dir ~= "" then
        next_.shaderDir = noctalia.expandPath(decoded.shader_dir)
    end
    if not next_.shaderDir then
        return "в profiles.json нет shader_dir"
    end

    if type(decoded.profiles) == "table" then
        for name, file in pairs(decoded.profiles) do
            local n, f = safeToken(name), safeToken(file)
            if n and f then next_.profiles[n] = f end
        end
    end

    if type(decoded.classes) == "table" then
        for _, class in ipairs(decoded.classes) do
            local c = safeToken(class)
            if c and not next_.classSet[c] then
                table.insert(next_.classes, c)
                next_.classSet[c] = true
            end
        end
    end
    if #next_.classes == 0 then
        return "в profiles.json нет ни одного допустимого класса"
    end

    -- Ссылка обязана указывать на существующий профиль: битое значение не
    -- превращается в dispatch мимо диска.
    local function target(value)
        return profilePathIn(next_, value) and value or nil
    end

    if type(decoded.by_id) == "table" then
        for id, name in pairs(decoded.by_id) do
            if type(id) == "string" and id ~= "" then
                next_.byId[id] = target(name)
            end
        end
    end

    if type(decoded.by_tag) == "table" then
        for tag, name in pairs(decoded.by_tag) do
            if type(tag) == "string" and tag ~= "" then
                next_.byTag[normalizeText(tag)] = target(name)
            end
        end
    end

    if type(decoded.by_keyword) == "table" then
        for keyword, name in pairs(decoded.by_keyword) do
            local kw = type(keyword) == "string" and normalizeText(keyword) or nil
            if kw and kw ~= "" and target(name) then
                -- Длина считается один раз при загрузке и лежит рядом с
                -- ключом: компаратор сортировки вызывается O(n log n) раз, и
                -- utf8.len внутри него пересчитывал бы одно и то же.
                -- Единственный безопасный запасной вариант — байты: utf8.len
                -- возвращает nil на невалидном вводе.
                table.insert(next_.keywords, {
                    keyword = kw,
                    length = utf8.len(kw) or #kw,
                    profile = name,
                })
            end
        end
        -- Порядок обхода pairs не определён, а решение обязано быть
        -- детерминированным: длинное ключевое слово специфичнее и выигрывает,
        -- при равной длине — лексикографический порядок.
        table.sort(next_.keywords, function(a, b)
            if a.length ~= b.length then return a.length > b.length end
            return a.keyword < b.keyword
        end)
    end

    cfg = next_
    cfg.ok = true
    resolution = {} -- метаданные те же, а правила сменились: кэш больше не правда
    return nil
end

-- ────────────────────────────────────────────────────────── состояние обоев

local function statusFile()
    -- Свой data-каталог даёт корень хранилища плагинов; он уважает
    -- NOCTALIA_STATE_HOME, в отличие от захардкоженного ~/.local/state.
    local mine = noctalia.pluginDataDir()
    local root = mine and string.match(mine, "^(.+)/plugins/data/[^/]+/[^/]+$")
    if root then
        return root .. "/plugins/data/" .. STATUS_PLUGIN .. "/" .. STATUS_FILE
    end
    return noctalia.expandPath(
        "~/.local/state/noctalia/plugins/data/" .. STATUS_PLUGIN .. "/" .. STATUS_FILE)
end

-- Чтение и декодирование playback.json — только когда сменился mtime файла.
-- Дешёвая проверка метаданных на каждом тике нужна, потому что хост не
-- сообщает о записи в чужой персистентный файл.
-- nil = «состояния нет» (Studio выключен, playback ещё не включался или файл
-- нечитаем): это не ошибка, а законный повод для clear. Выключенное состояние
-- тоже даёт nil: воспроизведение по-настоящему остановлено, а устаревшие
-- записи current не должны навешивать шейдеры.
local function readCurrent()
    local path = statusFile()
    local info = noctalia.fileInfo(path)
    local mtime = info and info.mtime or nil

    if mtime == statusMtime then
        return currentCache or nil
    end

    if not mtime then
        -- Файла нет: «пусто» — законный результат, и он кэшируется, чтобы
        -- не пробовать читать файл на каждом тике.
        statusMtime = nil
        currentCache = false
        return nil
    end

    local raw = noctalia.readFile(path)
    if raw == nil then
        -- Файл есть по метаданным, но прочитать его не удалось (например,
        -- права). Кэш на этот mtime не ставим: как только чтение заработает,
        -- состояние подхватится, а не залипнет в «пусто».
        statusMtime = nil
        currentCache = false
        return nil
    end

    local decoded = noctalia.json.decode(raw)
    if type(decoded) ~= "table" or decoded.enabled ~= true
        or type(decoded.current) ~= "table" then
        statusMtime = mtime
        currentCache = false
        return nil
    end

    statusMtime = mtime
    currentCache = decoded.current
    return currentCache
end

local function activeOutputs()
    local active = {}
    local list = noctalia.outputs()
    if type(list) == "table" then
        for _, output in ipairs(list) do
            if type(output) == "table" and type(output.name) == "string" then
                active[output.name] = true
            end
        end
    end
    return active
end

-- Один source_id на все классы: только когда активные выходы согласны.
-- current[выход] — запись рендерера: годится лишь status == "playing" и
-- непустой строковый record.source_id.
local function desiredId()
    local current = readCurrent()
    if not current then return nil end

    local active = activeOutputs()
    local trustAll = next(active) == nil -- список выходов недоступен: берём всё

    local first, conflict = nil, false
    for output, record in pairs(current) do
        if type(record) ~= "table" then continue end
        if record.status ~= "playing" then continue end
        local id = record.source_id
        if type(id) ~= "string" or id == "" then continue end
        if not (trustAll or active[output]) then continue end

        if first == nil then
            first = id
        elseif first ~= id then
            conflict = true
        end
    end

    if conflict or first == nil then return nil end
    return first
end

-- ────────────────────────────────────────────────────────── разрешение id

local function workshopDir()
    for _, root in ipairs(WorkshopRoots) do
        local dir = noctalia.expandPath(root)
        if dir and noctalia.fileExists(dir) then return dir end
    end
    return nil
end

-- project.json читается только здесь и только для id без готовного решения.
local function resolveId(id)
    if not cfg.ok or type(id) ~= "string" then return nil end

    local direct = cfg.byId[id]
    if direct and profilePath(direct) then return direct end

    local dir = workshopDir()
    local meta = nil
    if dir then
        local raw = noctalia.readFile(dir .. "/" .. id .. "/project.json")
        meta = raw and noctalia.json.decode(raw) or nil
    end

    if type(meta) == "table" then
        -- Специфичность: сначала ключевые слова из заголовка/описания,
        -- и только потом грубые workshop-теги.
        local text = normalizeText(tostring(meta.title or "") .. " " .. tostring(meta.description or ""))
        for _, entry in ipairs(cfg.keywords) do
            if string.find(text, entry.keyword, 1, true) and profilePath(entry.profile) then
                return entry.profile
            end
        end

        if type(meta.tags) == "table" then
            -- Теги идут в порядке самого workshop-файла: первый знакомый выигрывает.
            for _, tag in ipairs(meta.tags) do
                if type(tag) == "string" then
                    local hit = cfg.byTag[normalizeText(tag)]
                    if hit and profilePath(hit) then return hit end
                end
            end
        end
    end

    return nil
end

local function resolveCached(id)
    if type(id) ~= "string" then return nil end
    local cached = resolution[id]
    if cached == nil then
        cached = resolveId(id) or false
        resolution[id] = cached
    end
    return cached or nil
end

-- ────────────────────────────────────────────────────────── применение

-- Вызов уходит через shell, а не через return-код: HyprWindowShade применяет
-- шейдер и только потом отвечает `hl.dispatch: expected a dispatcher` с
-- ненулевым кодом. Это штатный ответ, а не ошибка, поэтому его вывод глушится,
-- а результат команды принудительно делается успешным — иначе каждый dispatch
-- оставлял бы в логе мусор и выглядел бы как упавшая асинхронная работа.
local function dispatch(class, target)
    noctalia.runAsync(string.format(
        "hyprctl dispatch 'hl.plugin.HyprWindowShade.classshader(\"%s\", \"%s\")' >/dev/null 2>&1 || true",
        class, target))
end

local function apply(name)
    if not cfg.ok then return end

    local target = CLEAR
    if name then
        local path = profilePath(name)
        if path then target = path end
    end

    local want = {}
    for _, class in ipairs(cfg.classes) do
        want[class] = target
    end
    -- Класс, выпавший из profiles.json, обязан получить clear один раз —
    -- иначе на нём навсегда останется чужой профиль.
    for class in pairs(applied) do
        if want[class] == nil then want[class] = CLEAR end
    end

    for class, path in pairs(want) do
        if applied[class] ~= path then
            dispatch(class, path)
            applied[class] = path
        end
    end
end

-- ────────────────────────────────────────────────────────── цикл

local function refresh()
    apply(resolveCached(desiredId()))
end

noctalia.setUpdateInterval(TICK_MS)

function update()
    -- profiles.json редактируется вручную, хост за ним не следит: дешёвая
    -- проверка mtime раз в тик дешевле, чем перечитывать файл целиком.
    local dir = noctalia.pluginDir()
    local info = dir and noctalia.fileInfo(dir .. "/profiles.json") or nil
    if info and info.mtime ~= configMtime then
        configMtime = info.mtime
        local err = loadConfig()
        if err then
            -- Конфиг остаётся прежним, рабочим: правка с опечаткой не должна
            -- снимать шейдеры с работающего стола.
            noctalia.log("w-engine-effects: " .. err)
        end
    end
    refresh()
end

local initialErr = loadConfig()
if initialErr then
    noctalia.log("w-engine-effects: " .. initialErr .. " — служба простаивает")
else
    local info = noctalia.pluginDir()
    info = info and noctalia.fileInfo(info .. "/profiles.json") or nil
    configMtime = info and info.mtime or nil
    refresh() -- применяем сразу, не дожидаясь первого тика
end
