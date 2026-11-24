from pathlib import Path

from .base import Launcher, local_directory, read_json


class Riot(Launcher):
    key = "riot"
    login_hosts = ('auth.riotgames.com', 'authenticate.riotgames.com')
    display_names = ("riot client",)
    publishers = ("Riot Games, Inc.", "Riot Games, Inc", "Riot Games Inc.")
    suffixes = ("RiotClientServices.exe",)
    paths = (
        ("SystemDrive", "/Riot Games/Riot Client/RiotClientServices.exe"),
        ("ProgramFiles", "Riot Games/Riot Client/RiotClientServices.exe"),
    )
    helper_paths = ("UX/RiotClientUx.exe", "UX/RiotClientUxRender.exe")

    def discover_paths(self):
        file = local_directory("ProgramData", "Riot Games/RiotClientInstalls.json")
        local = read_json(file) if file else {}
        if isinstance(local, dict):
            for key in ("rc_live", "rc_default"):
                if isinstance((value := local.get(key)), str) and value:
                    yield Path(value)
        yield from super().discover_paths()

    def discover_games(self):
        excluded = {"riot vanguard", "riot client", "riot games"}
        return (game for game in self.registry_games(self.publishers) if game.name.casefold() not in excluded)
