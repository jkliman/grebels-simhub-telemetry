function Register()
    return "48 8B 05 ?? ?? ?? ?? 48 8B 14 D0 4A 8D 3C C2"
end

function OnMatchFound(MatchAddress)
    return MatchAddress + 0x7 + DerefToInt32(MatchAddress + 0x3)
end
