-- GRTelemetry -- publishes object addresses for the telemetry bridge.
--
-- This mod deliberately does almost nothing. It runs a few times a second and
-- writes a small text file naming the player's craft, its root component, the
-- UWorld, and the transform values it read through Unreal's reflection system.
--
-- The bridge outside the game then reads the transform directly out of process
-- memory at a thousand hertz. Doing the fast sampling here instead would put
-- file I/O on the game thread every frame; doing it outside costs the game
-- nothing. The published VALUES exist so the bridge can find the right memory
-- offsets by matching against a known answer, rather than relying on constants
-- that a game patch would silently invalidate.
--
-- It also resolves a list of gameplay properties (weapons, shields, boost) to
-- their byte offsets within the pawn, by name, through Unreal's reflection
-- system. Offsets move whenever the game is patched, so resolving them by name
-- at runtime is the difference between surviving an update and silently
-- reading garbage. The resolution walk is cached per pawn class -- it only
-- reruns when the class changes, not every tick.
--
-- Two UE 5.8 quirks worth knowing, both found the hard way: the table returned
-- by K2_GetActorRotation enumerates its keys as Roll, Yaw, pitch (lowercase p),
-- and the vector accessors return nil often enough that every read needs a
-- pcall guard. Hence the defensive style below.

local UEHelpers = require("UEHelpers")

-- Rewritten to an absolute path by the installer. The game's working directory
-- is not reliably the folder UE4SS lives in, so a relative path would leave the
-- bridge hunting for this file.
local OUTPUT_PATH = "@@OUTPUT_PATH@@"
local INTERVAL_MS = 200

local lastCraft = ""

local function try(fn)
    local ok, value = pcall(fn)
    if ok then return value end
    return nil
end

-- Gameplay properties the bridge reads straight out of memory. Names are exact
-- Blueprint variable names -- note the ones with spaces and question marks,
-- which are legal in Blueprint and appear verbatim in the reflection data.
local WANTED = {
    "PrimaryFireMagazineStatus",   -- rounds left: the shot counter
    "PrimaryFireMagazineSize",
    "PrimaryFire_pressed",
    "PrimaryFire_Success",
    "ShootLeft",                   -- alternating barrel
    "TotalHeatPrimary",
    "MaxHeatPrimary",
    "isOverheatedPrimary",
    "AvailableMissiles",
    "MaxAvailableMissiles",
    "MissileNotificationActive",   -- incoming missile warning
    "Health",
    "ShieldHealthCurrent",
    "ShieldHealthMax",
    "BoostAxis",
    "EngineBoosterIsActive",
    "EngineBoostTimePercentage",
    "LandingGearActive",
    "isLanding",
    "CurrentVelocity",             -- the game's own speed scalar
    "CurrentMaxVelocity",          -- denominator for the synthetic RPM
    "Force F Primary Fire",        -- unverified: may be a tuning constant
    "Force F Secondary Fire",
}

local wantedSet = {}
for _, name in ipairs(WANTED) do wantedSet[name] = true end

-- Cached "field=" block, rebuilt only when the pawn class changes.
local fieldsBlock = ""
local fieldsForClass = ""

local function resolveFields(pawn)
    local class = try(function() return pawn:GetClass() end)
    if not class then return end

    local className = try(function() return class:GetFName():ToString() end) or "?"
    if className == fieldsForClass then return end

    local found = {}
    local ok = pcall(function()
        class:ForEachProperty(function(prop)
            local name = prop:GetFName():ToString()
            if wantedSet[name] then
                local kind = prop:GetClass():GetFName():ToString()
                found[#found + 1] = string.format("field=%d,%s,%s\n",
                    prop:GetOffset_Internal(), kind, name)
            end
        end)
    end)
    if not ok then return end

    table.sort(found)
    fieldsBlock = table.concat(found)
    fieldsForClass = className
    print(string.format("[GRTelemetry] resolved %d/%d fields on %s\n",
        #found, #WANTED, className))
end


local function outputPath()
    if OUTPUT_PATH:sub(1, 2) == "@@" then
        return "gr_target.txt"          -- unsubstituted: fall back to the CWD
    end
    return OUTPUT_PATH
end

local function readVector(v)
    if v == nil then return nil end
    local x = try(function() return v.X end)
    local y = try(function() return v.Y end)
    local z = try(function() return v.Z end)
    if x == nil or y == nil or z == nil then return nil end
    return { x, y, z }
end

local function readRotator(v)
    if v == nil then return nil end
    local p = try(function() return v.Pitch end)
    local y = try(function() return v.Yaw end)
    local r = try(function() return v.Roll end)
    if p == nil then p = try(function() return v.pitch end) end
    if p == nil or y == nil or r == nil then return nil end
    return { p, y, r }
end

local function writeTriple(file, key, values)
    if values then
        file:write(string.format("%s=%.6f,%.6f,%.6f\n", key,
            values[1], values[2], values[3]))
    else
        file:write(key .. "=nil\n")
    end
end

local function publish()
    local controller = UEHelpers.GetPlayerController()
    if not controller or not controller:IsValid() then return end

    local pawn = controller.Pawn
    if not pawn or not pawn:IsValid() then return end

    local root = pawn.RootComponent
    if not root or not root:IsValid() then return end

    local world = try(function() return UEHelpers.GetWorld() end)
    local worldAddress = 0
    if world and world:IsValid() then
        worldAddress = world:GetAddress()
    end

    -- The sim clock. The bridge finds UWorld::TimeSeconds by looking for a
    -- double matching this value that then advances in real time.
    local timeSeconds = -1.0
    local statics = try(function() return UEHelpers.GetGameplayStatics() end)
    if statics and world then
        local value = try(function() return statics:GetTimeSeconds(world) end)
        if value ~= nil then timeSeconds = value end
    end

    resolveFields(pawn)

    local name = pawn:GetFullName()
    if name ~= lastCraft then
        print(string.format("[GRTelemetry] craft: %s (world %X)\n", name, worldAddress))
        lastCraft = name
    end

    local file = io.open(outputPath(), "w")
    if not file then return end

    file:write("pawn=" .. name .. "\n")
    file:write(string.format("pawn_addr=%X\n", pawn:GetAddress()))
    file:write(string.format("root_addr=%X\n", root:GetAddress()))
    file:write(string.format("world_addr=%X\n", worldAddress))
    -- Landmarks for the offset hunt: knowing which object a pointer route
    -- passes through is what tells us whether the route is meaningful or a
    -- coincidence that will break the next time the craft respawns.
    file:write(string.format("controller_addr=%X\n", controller:GetAddress()))
    local acknowledged = try(function() return controller.AcknowledgedPawn end)
    if acknowledged and acknowledged:IsValid() then
        file:write(string.format("acknowledged_addr=%X\n", acknowledged:GetAddress()))
    end
    local player = try(function() return controller.Player end)
    if player and player:IsValid() then
        file:write(string.format("localplayer_addr=%X\n", player:GetAddress()))
    end
    local camera = try(function() return controller.PlayerCameraManager end)
    if camera and camera:IsValid() then
        file:write(string.format("camera_addr=%X\n", camera:GetAddress()))
    end
    local state = try(function() return controller.PlayerState end)
    if state and state:IsValid() then
        file:write(string.format("playerstate_addr=%X\n", state:GetAddress()))
    end
    local instance = try(function() return UEHelpers.GetGameInstance() end)
    if instance and instance:IsValid() then
        file:write(string.format("gameinstance_addr=%X\n", instance:GetAddress()))
    end
    file:write(string.format("time_seconds=%.6f\n", timeSeconds))
    writeTriple(file, "loc", readVector(try(function() return pawn:K2_GetActorLocation() end)))
    writeTriple(file, "rot", readRotator(try(function() return pawn:K2_GetActorRotation() end)))
    writeTriple(file, "vel", readVector(try(function() return pawn:GetVelocity() end)))
    file:write(fieldsBlock)
    file:close()
end

LoopAsync(INTERVAL_MS, function()
    ExecuteInGameThread(function()
        local ok, err = pcall(publish)
        if not ok then
            print("[GRTelemetry] error: " .. tostring(err) .. "\n")
        end
    end)
    return false
end)

print("[GRTelemetry] loaded\n")
