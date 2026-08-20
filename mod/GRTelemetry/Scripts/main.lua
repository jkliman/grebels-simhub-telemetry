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
    file:write(string.format("time_seconds=%.6f\n", timeSeconds))
    writeTriple(file, "loc", readVector(try(function() return pawn:K2_GetActorLocation() end)))
    writeTriple(file, "rot", readRotator(try(function() return pawn:K2_GetActorRotation() end)))
    writeTriple(file, "vel", readVector(try(function() return pawn:GetVelocity() end)))
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
