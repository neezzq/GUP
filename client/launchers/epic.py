from pathlib import Path

from .base import Game, Launcher, game_directory, local_directory, read_json


BRANCHES = ("Win64", "Win32")


class Epic(Launcher):
    key = "epic"
    login_hosts = ('www.epicgames.com', 'accounts.epicgames.com')
    display_names = ("epic games launcher",)
    publishers = ("Epic Games, Inc.", "Epic Games Inc.", "Epic Games, Inc")
    suffixes = (
        "Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe",
        "Launcher/Portal/Binaries/Win32/EpicGamesLauncher.exe",
        "Portal/Binaries/Win64/EpicGamesLauncher.exe",
        "Portal/Binaries/Win32/EpicGamesLauncher.exe",
        "EpicGamesLauncher.exe",
    )
    paths = tuple(
        (env, f"Epic Games/Launcher/Portal/Binaries/{branch}/EpicGamesLauncher.exe")
        for env in ("ProgramFiles(x86)", "ProgramFiles")
        for branch in BRANCHES
    )
    helper_paths = ("EpicWebHelper.exe",)

    def discover_games(self):
        directory = local_directory("ProgramData", "Epic/EpicGamesLauncher/Data/Manifests")
        if not directory:
            return
        seen = set()
        for path in directory.glob("*.item"):
            item = read_json(path)
            if not isinstance(item, dict) or item.get("bIsIncompleteInstall"):
                continue
            identifier, name = item.get("AppName"), item.get("DisplayName")
            executable = item.get("LaunchExecutable")
            target = game_directory(item.get("InstallLocation"))
            if not (isinstance(identifier, str) and identifier and identifier not in seen
                    and isinstance(name, str) and name and isinstance(executable, str)
                    and executable and target):
                continue
            exe = (target / Path(executable)).resolve()
            if exe.is_relative_to(target) and exe.is_file():
                seen.add(identifier)
                yield Game(identifier, name, target, exe)
